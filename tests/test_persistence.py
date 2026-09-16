"""Persistence tests. In-memory logic runs everywhere; the Postgres integration
test skips cleanly when no database is reachable (e.g. outside the compose stack).
"""

import pytest

from lead_qualifier.config import settings
from lead_qualifier.persistence import InMemoryRepository
from lead_qualifier.schemas import TokenUsage


async def test_inmemory_records_usage_and_daily_cost():
    repo = InMemoryRepository()
    await repo.record_usage(
        trace_id="t1",
        domain="acme.com",
        usage=TokenUsage(model="claude-haiku-4-5", input_tokens=1000, output_tokens=500, cost_usd=0.0035),
    )
    assert await repo.daily_cost_usd() == 0.0035


async def test_inmemory_log_stores_events():
    repo = InMemoryRepository()
    await repo.log(trace_id="t1", domain="acme.com", stage="enrichment_started")
    await repo.log(
        trace_id="t1", domain="acme.com", stage="request_completed", detail={"score": 80}
    )
    assert [e["stage"] for e in repo.logs] == ["enrichment_started", "request_completed"]
    assert repo.logs[1]["detail"]["score"] == 80


async def test_inmemory_stats_derives_cost_per_lead_and_reliability():
    repo = InMemoryRepository()
    haiku = "claude-haiku-4-5"

    async def usage(cost, tin=100, tout=50):
        await repo.record_usage(
            trace_id="t", domain="acme.com",
            usage=TokenUsage(model=haiku, input_tokens=tin, output_tokens=tout, cost_usd=cost),
        )

    # Lead A: two billed attempts (schema repair), then completed.
    await usage(0.001)
    await usage(0.001)
    await repo.log(trace_id="a", domain="a.com", stage="request_completed", detail={"latency_ms": 100})
    # Lead B: one attempt, completed.
    await usage(0.0015)
    await repo.log(trace_id="b", domain="b.com", stage="request_completed", detail={"latency_ms": 200})
    # Lead C: one billed attempt, then failed (spend still counts).
    await usage(0.001)
    await repo.log(trace_id="c", domain="c.com", stage="request_failed", detail={"failure_stage": "schema"})

    s = await repo.stats()
    assert s.total_cost_usd == 0.0045
    assert s.llm_calls == 4
    assert s.leads == 2
    assert s.cost_per_lead_usd == 0.00225  # all spend / delivered leads, waste included
    assert s.requests_completed == 2 and s.requests_failed == 1
    assert s.failure_rate == round(1 / 3, 4)
    assert s.avg_latency_ms == 150
    assert [(m.model, m.llm_calls, m.cost_usd) for m in s.by_model] == [(haiku, 4, 0.0045)]
    assert s.daily_ceiling_usd == settings.daily_cost_ceiling_usd
    assert s.ceiling_used_pct == round(0.0045 / settings.daily_cost_ceiling_usd * 100, 2)


async def test_inmemory_stats_empty_is_all_zeros_not_a_crash():
    s = await InMemoryRepository().stats()
    assert s.total_cost_usd == 0.0
    assert s.cost_per_lead_usd == 0.0
    assert s.failure_rate == 0.0
    assert s.avg_latency_ms is None
    assert s.by_model == []


async def _postgres_unavailable_reason() -> str | None:
    """None when usable, else why — so a skip never misreports its own cause."""
    try:
        import psycopg  # noqa: F401
        import psycopg_pool  # noqa: F401  — PostgresRepository needs the pool too
    except ImportError as exc:
        return f"{exc.name} not installed (pip install -e '.[api]')"
    try:
        conn = await psycopg.AsyncConnection.connect(settings.database_url, connect_timeout=5)
        await conn.close()
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"


async def test_postgres_roundtrip_if_available():
    reason = await _postgres_unavailable_reason()
    if reason is not None:
        pytest.skip(f"Postgres not usable here — {reason}")

    from lead_qualifier.persistence import PostgresRepository

    repo = PostgresRepository(settings.database_url)
    before = await repo.daily_cost_usd()
    await repo.record_usage(
        trace_id="itest",
        domain="acme.com",
        usage=TokenUsage(model="mock", input_tokens=10, output_tokens=20, cost_usd=0.01),
    )
    await repo.log(trace_id="itest", domain="acme.com", stage="itest", detail={"ok": True})
    after = await repo.daily_cost_usd()
    await repo.close()
    assert round(after - before, 6) == 0.01
