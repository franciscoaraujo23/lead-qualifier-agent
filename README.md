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
# 1. Create one Postgres credential in the n8n UI (localhost:5678), named
#    "Lead Qualifier Postgres", pointing at host `postgres`, db `lead_qualifier`,
#    user/password `lq`/`lq`.

# 2. Import both workflows.
docker compose exec n8n n8n import:workflow --separate --input=/workflows

# 3. In the UI: select that credential on the four Postgres nodes, set
#    "Lead Qualifier - Error Handler" as the error workflow on the main
#    workflow's settings, and activate the main workflow.
```

Then drive it through the orchestration layer rather than the API directly:

```bash
curl -X POST localhost:5678/webhook/lead   -H 'content-type: application/json'   -H 'x-auth-token: <LQ_WEBHOOK_AUTH_TOKEN, if set>'   -d '{"domain": "stripe.com"}'
```

### What each exit path returns

| Situation | Status | Body |
|---|---|---|
| Qualified | `200` | the API's validated `QualifyResponse` |
| Same key replayed after success | `200` | the **stored** result — routing is skipped, so no duplicate hot-path side effect |
| Same key while the first run is working | `409` | `duplicate_in_flight` |
| Bad or missing `X-Auth-Token` | `401` | `unauthorized` (before any DB write or LLM spend) |
| Missing/malformed domain | `400` | `invalid_domain` |
| API returned a typed error, or retries exhausted | the API's own status (`422`/`502`/`503`) | failure `stage`, `attempts`, and the `dead_letter_id` to look up |

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

Three implementation choices in that workflow are worth reading the rationale
for — the hand-built retry loop, the readable idempotency key, and routing on
`tier` rather than on a number. They are written up in
[architecture.md §9](architecture.md).

## Configuration

Copy `.env.example` to `.env`. All variables are prefixed `LQ_`. Defaults run the
mock provider against a local Postgres; set `LQ_LLM_PROVIDER=anthropic` and
`LQ_ANTHROPIC_API_KEY` to use a real model.
