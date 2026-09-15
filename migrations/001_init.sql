-- Phase 0 schema. One Postgres instance backs idempotency, dead-letter,
-- structured logs, and cost tracking (architecture.md §4).

BEGIN;

-- §4.1 Idempotency. Acquisition is a single atomic upsert:
--   INSERT ... ON CONFLICT (key) DO NOTHING RETURNING status
-- A returned row means this caller won and owns the work; no row means a
-- concurrent caller holds the key and we branch on its status.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key         TEXT PRIMARY KEY,
    domain      TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed')),
    result      JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_idempotency_status ON idempotency_keys (status);

-- §4.4 Dead-letter queue as a queryable table (not a message queue), so
-- failures are inspectable with plain SQL and replayable by the eval harness.
CREATE TABLE IF NOT EXISTS dead_letters (
    id               BIGSERIAL PRIMARY KEY,
    domain           TEXT NOT NULL,
    idempotency_key  TEXT,
    failure_stage    TEXT NOT NULL
                       CHECK (failure_stage IN
                         ('enrichment', 'llm', 'schema', 'network', 'cost_ceiling', 'unknown')),
    last_error       TEXT,
    attempt_count    INT NOT NULL DEFAULT 1,
    payload_snapshot JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dead_letters_stage ON dead_letters (failure_stage);
CREATE INDEX IF NOT EXISTS idx_dead_letters_created ON dead_letters (created_at DESC);

-- §4.5 Structured logs persisted (not just stdout) so "show everything that
-- happened for domain X" is a single SQL query. trace_id = idempotency key.
CREATE TABLE IF NOT EXISTS structured_logs (
    id         BIGSERIAL PRIMARY KEY,
    trace_id   TEXT NOT NULL,
    domain     TEXT,
    stage      TEXT NOT NULL,
    level      TEXT NOT NULL DEFAULT 'info',
    detail     JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_logs_trace ON structured_logs (trace_id);

-- §4.6 Token/cost tracking. Also feeds the daily cost-ceiling gate (§4.7):
--   SELECT SUM(cost_usd) FROM token_usage WHERE created_at > now() - interval '1 day'
CREATE TABLE IF NOT EXISTS token_usage (
    id              BIGSERIAL PRIMARY KEY,
    idempotency_key TEXT,
    domain          TEXT,
    model           TEXT NOT NULL,
    input_tokens    INT NOT NULL DEFAULT 0,
    output_tokens   INT NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_token_usage_created ON token_usage (created_at DESC);

COMMIT;
