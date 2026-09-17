# Lead Qualification Agent

Give it a company **domain** and nothing else. It enriches from public sources,
scores the lead with an LLM, drafts an outreach message, validates that output
against a schema, and routes the result by score — with idempotency, retries,
dead-lettering, structured logs, and token-cost tracking around it.

The design and the reasoning behind every decision live in
[architecture.md](architecture.md). This README covers running it.

## Status

- **Phase 0 (contracts)** — done: payload schemas, provider-agnostic LLM
  interface, config/thresholds, Postgres migration.
- **Phase 1 (enrichment)** — done: DNS, RDAP (modern WHOIS), site scrape, tech
  fingerprint, best-effort per source.
- **Phase 2 (scoring)** — done: `/qualify` endpoint, LLM behind the interface,
  schema validation + bounded repair loop, token-cost accounting + ceiling.
- **Phase 3A (persistence)** — done: `PostgresRepository` (cost + structured
  logs), compose stack (Postgres + API + n8n).
- **Phase 3B (n8n orchestration)** — done: the workflow (webhook, atomic
  idempotency, transport retry with backoff, score routing, dead-letter) plus a
  safety-net error workflow, in [`n8n/`](n8n).
- **Phase 4 (observability)** — done: structured logs correlated by `trace_id`,
  and a `/stats` endpoint (running spend, cost per lead, per-model breakdown,
  repair overhead, failure rate, latency).

See the full build plan in [architecture.md §7](architecture.md).

## Run the tests (no keys, no network)

```bash
pip install -e ".[dev]"
pytest
```

Everything runs offline via a deterministic mock LLM provider and an in-memory
repository, so a reviewer can verify the whole core on a clean machine in one
step. The one Postgres integration test skips unless a database is reachable.

## Run the full stack

```bash
docker compose up
```

Brings up Postgres (schema auto-created), the API on `:8000`, and n8n on `:5678`.
Try it:

```bash
curl -X POST localhost:8000/qualify -H 'content-type: application/json' \
  -d '{"domain": "stripe.com"}'
```

Then see the running economics and reliability:

```bash
curl localhost:8000/stats
# { "total_cost_usd": ..., "cost_per_lead_usd": ..., "leads": ...,
#   "llm_calls": ..., "by_model": [...], "ceiling_used_pct": ...,
#   "requests_completed": ..., "requests_failed": ..., "failure_rate": ...,
#   "avg_latency_ms": ... }
```

## Layout

```
src/lead_qualifier/     reusable core (importable as a package)
  schemas.py            payload contracts (single source of truth)
  config.py             thresholds, cost table, safety limits
  errors.py             typed errors -> HTTP status + dead-letter stage
  enrichment.py         enrichment interface (stubbed in Phase 0)
  llm/                  provider-agnostic LLM interface + mock provider
migrations/001_init.sql Postgres: idempotency, dead_letters, logs, token_usage
n8n/                    orchestration workflows (importable JSON)
tests/                  contract tests
```

## The n8n workflow

`docker compose up` starts n8n empty — workflows carry credential references, so
they are imported rather than baked into the image. The JSON is mounted at
`/workflows` inside the container:

```bash
# 1. Import both workflows.
docker compose exec n8n n8n import:workflow --separate --input=/workflows
```

Then, in the UI at `localhost:5678`, create two credentials and attach them:

| Credential | Type | Attach to |
|---|---|---|
| `Lead Qualifier Postgres` | Postgres — host `postgres`, db `lead_qualifier`, user/password `lq`/`lq` | the five Postgres nodes (four in the main workflow, one in the error handler) |
| `Lead Qualifier Webhook Token` | Header Auth — name `X-Auth-Token`, value anything you choose | the `POST /lead` webhook node and the `Call /qualify` node |

Finally, set "Lead Qualifier - Error Handler" as the error workflow in the main
workflow's settings, and activate it.

The workflow deliberately reads **no environment variables** — its tunables live
in a `Config` node at the head of the canvas, and its two secrets are the
credentials above. [architecture.md §9.6](architecture.md) explains why.

Then drive it through the orchestration layer rather than the API directly:

```bash
curl -X POST localhost:5678/webhook/lead   -H 'content-type: application/json'   -H 'x-auth-token: <the value you set in the Header Auth credential>'   -d '{"domain": "stripe.com"}'
```

### What each exit path returns

| Situation | Status | Body | Verified |
|---|---|---|---|
| Bad or missing `X-Auth-Token` | `403` | rejected by the webhook node itself, before the workflow starts — no execution record, no DB write, no spend | ✅ live |
| Missing/malformed domain | `400` | `invalid_domain` | ✅ live |
| Same key while the first run is working | `409` | `duplicate_in_flight` | ✅ live |
| API unreachable or typed error, retries exhausted | the API's own status (`422`/`502`/`503`) | failure `stage`, `attempts`, and the `dead_letter_id` to look up | ✅ live |
| Qualified | `200` | the API's validated `QualifyResponse` | ✅ live |
| Same key replayed after success | `200` | the **stored** result — routing is skipped, so no duplicate hot-path side effect | ✅ live |

All six rows were exercised against a live n8n instance backed by real Postgres
(Neon), calling a deployed FastAPI service scoring with a real model (Claude
Haiku 4.5 via OpenRouter). Two are worth calling out. The concurrency row: a
second submission arriving 4s into a 25s run was answered `409` in 0.6s, having
started no second pipeline. The cache-replay row: the same key submitted twice
returned a fresh `200` in ~8s the first time and the **stored** `200` in 0.65s
the second, and `token_usage` held exactly **one** row for the key afterwards —
the replay re-ran nothing and spent nothing. Tier routing was exercised in the
same run (a `cold` result took the cold branch to the no-op it documents).

The SQL behind all of this has its own tests —
[`tests/test_idempotency_sql.py`](tests/test_idempotency_sql.py) reads the
statements **out of the workflow JSON** so they cannot drift from what the nodes
run, and checks the race directly: two simultaneous callers, exactly one winner,
and the loser demonstrably blocked on the row lock rather than slipping past it.

### Seeing the machinery

Every lead's journey is keyed by the idempotency key, which n8n also sends as
the `x-trace-id` header — so one identifier joins the n8n execution log to the
API's own rows:

```sql
SELECT stage, detail, created_at FROM structured_logs WHERE trace_id = 'stripe.com:2026-09-15' ORDER BY id;
SELECT key, status, updated_at FROM idempotency_keys WHERE key = 'stripe.com:2026-09-15';
SELECT failure_stage, attempt_count, last_error FROM dead_letters ORDER BY created_at DESC LIMIT 10;
SELECT model, sum(cost_usd) AS spend, count(*) AS leads FROM token_usage GROUP BY model;
```

Seven implementation choices in that workflow are worth reading the rationale
for — the hand-built retry loop, the readable idempotency key, routing on `tier`
rather than on a number, and the two things only running it on a real n8n
revealed. They are written up in [architecture.md §9](architecture.md).

## Configuration

Copy `.env.example` to `.env`. All variables are prefixed `LQ_`. Defaults run the
mock provider against a local Postgres. For a real model, pick a provider:

- `LQ_LLM_PROVIDER=anthropic` with `LQ_ANTHROPIC_API_KEY` — Anthropic direct.
- `LQ_LLM_PROVIDER=openrouter` with `LQ_OPENROUTER_API_KEY` — the same Claude
  Haiku 4.5 routed through OpenRouter, which also reports the real per-call cost.
  Model id is namespaced (`anthropic/claude-haiku-4.5`); see
  [architecture.md §10](architecture.md) for why there are two providers and how
  cost accounting differs between them.
