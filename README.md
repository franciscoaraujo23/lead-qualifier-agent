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
- **Phase 3B (n8n orchestration)** — pending: the workflow (webhook, atomic
  idempotency, transport retry, score routing, dead-letter) is authored via the
  n8n tooling.

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
tests/                  contract tests
```

## Configuration

Copy `.env.example` to `.env`. All variables are prefixed `LQ_`. Defaults run the
mock provider against a local Postgres; set `LQ_LLM_PROVIDER=anthropic` and
`LQ_ANTHROPIC_API_KEY` to use a real model.
