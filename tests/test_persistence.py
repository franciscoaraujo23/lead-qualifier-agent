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
