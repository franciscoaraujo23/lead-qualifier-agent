<!--
DRAFT — portfolio case study for the Lead Qualification Agent.
Audience: a technical reviewer or technical buyer evaluating the engineering.
Written in English to match README.md and architecture.md; say the word and it moves to PT-PT.
Screenshot slots are marked [[SCREENSHOT: ...]] — captured once, from the live system.
Everything stated here is live-verified; nothing is aspirational. Keep it that way.
-->

# Lead Qualification Agent — case study

**Give it a company domain. Get back an enriched profile, a scored lead, a drafted
outreach message, and a routing decision — with every step traceable in SQL.**

The interesting part isn't the LLM call. It's the orchestration discipline around
it: idempotency, layered retries, schema-validated model output, dead-lettering,
structured logs, and per-call cost tracking with a spend ceiling — all inspectable,
all proven to work against a live system rather than asserted.

[[SCREENSHOT: one `curl … /qualify` → the JSON response (score, tier, reasoning, draft, usage.cost_usd)]]

---

## What it is

A public webhook takes `{"domain": "acme.com"}`. n8n orchestrates; a FastAPI
service does the domain work; Postgres holds the shared state. One request in,
one enriched decision out — no polling.

```
POST /webhook/lead                 →  n8n            (orchestration: auth, idempotency, retry, routing, dead-letter)
      → POST /qualify              →  FastAPI        (enrichment → LLM → schema validation → cost accounting)
            → Claude Haiku 4.5     →  OpenRouter     (scored, with the real per-call cost returned)
      → Postgres                                     (idempotency keys, dead letters, structured logs, token cost)
```

The boundary is deliberate: **n8n never touches enrichment, prompts, or scoring.**
It deduplicates, calls one HTTP endpoint, retries transport failures, routes on a
`tier` field, and dead-letters on exhaustion. FastAPI owns everything
domain-specific and stays a pure function of its input — which is what makes the
core reusable outside this workflow.

[[SCREENSHOT: the n8n canvas — the full "Lead Qualifier - Main" workflow]]

---

## The engineering worth noticing

- **Race-safe idempotency in one statement.** Key acquisition is a single
  `INSERT … ON CONFLICT DO UPDATE … WHERE status='failed' RETURNING …`. Two
  concurrent submissions of the same key cannot both proceed; a prior *failed*
  run is retryable, a *completed* one is served from cache and never re-routed —
  so a resubmission never re-fires a side effect or re-spends on the LLM.

- **Two retry layers, on purpose different.** n8n retries *transport* failures
  (network / 5xx) with exponential backoff. FastAPI runs a separate *schema-repair*
  loop that re-prompts the model with the validation error. A network retry
  re-sends an identical request; a repair retry changes the prompt. Merging them
  would either waste spend re-sending a bad prompt or leak domain logic into n8n.
  The daily cost-ceiling `503` is deliberately excluded from transport retry — a
  budget decision won't resolve itself in four seconds.

- **The model's output is validated, not trusted.** Responses are parsed against a
  Pydantic schema; invalid output is repaired within a bound or surfaced as a typed
  `422` that n8n dead-letters — malformed data never reaches the caller.

- **Cost is measured, not estimated.** Every LLM attempt is booked the moment it
  returns — including repaired and failed ones — so the daily ceiling can't be
  blinded by a model looping on garbage. The provider interface has two real
  implementations (Anthropic direct, and Claude Haiku 4.5 via OpenRouter); the
  OpenRouter path reads back the **actual** USD charge per call, so cost-per-lead
  is a measured number, not a table guess.

- **A queryable audit trail.** Dead letters, structured logs, and token usage all
  live in Postgres, correlated by one `trace_id` (= the idempotency key, also sent
  as `x-trace-id`). "Show me everything that happened for domain X" is a SQL query.

- **Abuse ceiling.** The webhook requires a shared-secret header, enforced by n8n
  *before* the workflow starts (no execution, no spend on a bad token), and a daily
  cost ceiling bounds worst-case spend even if auth were bypassed.

The decisions that departed from the first design — and *why* — are written up in
[architecture.md §9 and §10](../architecture.md), including the two that only
surfaced by running the thing on a real n8n instead of reasoning about it.

---

## Evidence it works (live-verified)

Not a local mock run — a public n8n instance calling a deployed FastAPI service,
scoring with a real model, backed by real Postgres.

| Situation | Status | Verified |
|---|---|---|
| Bad / missing auth token | `403` (before any work) | ✅ live |
| Missing / malformed domain | `400` | ✅ live |
| Duplicate key while first run is in flight | `409` in 0.6s, no second pipeline started | ✅ live |
| API error / retries exhausted | typed `422`/`502`/`503` + `dead_letter_id` | ✅ live |
| Qualified | `200` with validated payload (~8s) | ✅ live |
| Same key replayed after success | stored `200` in **0.65s** | ✅ live |

The cache-replay row is the one to dwell on: the same domain submitted twice
returned a fresh `200` (~8s) then the **stored** `200` (0.65s), and afterwards
`token_usage` held **exactly one row** for that key — the replay re-ran nothing and
spent nothing. A real qualification of `stripe.com` cost **$0.0024** (1202 in / 166
out tokens, Claude Haiku 4.5).

[[SCREENSHOT: the "seeing the machinery" SQL — idempotency_keys + token_usage for one key, showing 1 usage row for a replayed lead]]

- **Test suite:** 52 passing, offline, no keys, no network (deterministic mock
  provider + in-memory repository), so a reviewer verifies the whole core in one
  step. The idempotency SQL is tested by reading the statements *out of the workflow
  JSON*, so they can't drift from what the nodes actually run.
- **`/stats`** reports running spend, cost per delivered lead, per-model breakdown,
  schema-repair overhead, failure rate, and latency.

[[SCREENSHOT: GET /stats output]]

---

## Judgment calls (what was deliberately *not* built)

- **Synchronous, not async.** One LLM call fits inside the webhook budget; a
  `202 + poll` pattern would be over-engineering for the demo. The escape hatch is
  documented for when volume needs it.
- **Two providers, not a plugin zoo.** Enough to prove the abstraction carries a
  second vendor with a different transport and cost story — no more.
- **Free enrichment only** (DNS, RDAP, scrape, tech fingerprint), best-effort per
  source, so anyone can run the whole thing with no keys but the model provider's.

---

## Reuse

The FastAPI service is the reusable unit: runnable standalone, importable as a
library, and reusable by a separate internal pipeline with different config
injected — never forked. The public repo keeps the generic core; business-specific
prompts, paid sources, and rules stay in a private config layer that consumes the
same package.

---

## Stack

Python · FastAPI · Pydantic · Postgres (Neon in production) · n8n · Docker Compose ·
Claude Haiku 4.5 via OpenRouter · deployed on Coolify.

- **Run it yourself:** `docker compose up` — Postgres + API + n8n, schema
  auto-created, mock provider by default (no keys). See the [README](../README.md).
- **Live demo:** _[[demo URL — add once the API has a proper domain]]_

<!-- TODO before publishing:
  - capture the 4 screenshots above
  - add the live demo URL (after HTTPS/custom domain)
  - Francisco's voice pass on the intro and the "judgment calls" section
  - decide PT-PT vs EN
-->
