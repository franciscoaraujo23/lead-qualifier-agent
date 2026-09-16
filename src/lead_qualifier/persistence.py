"""Persistence boundary.

A small Repository protocol so the pipeline never talks to a database directly.
Per §4.1, idempotency and dead-letter are owned by n8n (SQL nodes), so the
Python service's repository covers only what FastAPI writes: token cost (§4.6)
and structured logs (§4.5). The in-memory implementation keeps the app and tests
running with zero infra; the Postgres one is used in the compose stack.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .config import settings
from .schemas import ModelSpend, StatsSummary, TokenUsage


def _summary(
    *,
    total_cost: float,
    cost_24h: float,
    llm_calls: int,
    by_model: list[ModelSpend],
    completed: int,
    failed: int,
    avg_latency: float | None,
) -> StatsSummary:
    """Assemble the derived figures the same way for both repositories, so the
    two implementations can never disagree on how cost-per-lead or failure-rate
    is computed — only on how the raw counts were gathered."""
    ceiling = settings.daily_cost_ceiling_usd
    total_reqs = completed + failed
    return StatsSummary(
        total_cost_usd=round(total_cost, 6),
        cost_usd_24h=round(cost_24h, 6),
        leads=completed,
        llm_calls=llm_calls,
        cost_per_lead_usd=round(total_cost / completed, 6) if completed else 0.0,
        by_model=by_model,
        daily_ceiling_usd=ceiling,
        ceiling_used_pct=round(cost_24h / ceiling * 100, 2) if ceiling else 0.0,
        requests_completed=completed,
        requests_failed=failed,
        failure_rate=round(failed / total_reqs, 4) if total_reqs else 0.0,
        avg_latency_ms=int(avg_latency) if avg_latency is not None else None,
    )


class Repository(Protocol):
    async def daily_cost_usd(self) -> float:
        """Total token cost over the last 24h — feeds the cost ceiling (§4.7)."""
        ...

    async def record_usage(
        self, *, trace_id: str, domain: str, usage: TokenUsage
    ) -> None: ...

    async def log(
        self,
        *,
        trace_id: str,
        domain: str | None,
        stage: str,
        level: str = "info",
        detail: dict[str, Any] | None = None,
    ) -> None: ...

    async def stats(self) -> StatsSummary:
        """Aggregate spend, cost-per-lead and reliability for /stats (§4.6)."""
        ...


class InMemoryRepository(Repository):
    def __init__(self) -> None:
        self._usage: list[tuple[datetime, TokenUsage]] = []
        self.logs: list[dict[str, Any]] = []

    async def daily_cost_usd(self) -> float:
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        return round(sum(u.cost_usd for ts, u in self._usage if ts >= cutoff), 6)

    async def record_usage(
        self, *, trace_id: str, domain: str, usage: TokenUsage
    ) -> None:
        self._usage.append((datetime.now(timezone.utc), usage))

    async def log(
        self,
        *,
        trace_id: str,
        domain: str | None,
        stage: str,
        level: str = "info",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.logs.append(
            {"trace_id": trace_id, "domain": domain, "stage": stage, "level": level, "detail": detail}
        )

    async def stats(self) -> StatsSummary:
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        agg: dict[str, list] = {}  # model -> [calls, in, out, cost]
        for _, u in self._usage:
            row = agg.setdefault(u.model, [0, 0, 0, 0.0])
            row[0] += 1
            row[1] += u.input_tokens
            row[2] += u.output_tokens
            row[3] += u.cost_usd
        by_model = [
            ModelSpend(
                model=m, llm_calls=r[0], input_tokens=r[1], output_tokens=r[2], cost_usd=round(r[3], 6)
            )
            for m, r in sorted(agg.items())
        ]
        completed = [e for e in self.logs if e["stage"] == "request_completed"]
        failed = sum(1 for e in self.logs if e["stage"] == "request_failed")
        latencies = [
            e["detail"]["latency_ms"]
            for e in completed
            if e.get("detail") and "latency_ms" in e["detail"]
        ]
        return _summary(
            total_cost=sum(u.cost_usd for _, u in self._usage),
            cost_24h=sum(u.cost_usd for ts, u in self._usage if ts >= cutoff),
            llm_calls=len(self._usage),
            by_model=by_model,
            completed=len(completed),
            failed=failed,
            avg_latency=(sum(latencies) / len(latencies)) if latencies else None,
        )


class PostgresRepository(Repository):
    """Backed by the tables in migrations/001_init.sql. Uses an async connection
    pool opened lazily on first use.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool = None

    async def _get_pool(self):
        if self._pool is None:
            from psycopg_pool import AsyncConnectionPool

            self._pool = AsyncConnectionPool(self._dsn, open=False, min_size=1, max_size=5)
            await self._pool.open()
        return self._pool

    async def daily_cost_usd(self) -> float:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM token_usage "
                "WHERE created_at > now() - interval '1 day'"
            )
            row = await cur.fetchone()
            return float(row[0])

    async def record_usage(
        self, *, trace_id: str, domain: str, usage: TokenUsage
    ) -> None:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO token_usage "
                "(idempotency_key, domain, model, input_tokens, output_tokens, cost_usd) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (trace_id, domain, usage.model, usage.input_tokens, usage.output_tokens, usage.cost_usd),
            )

    async def log(
        self,
        *,
        trace_id: str,
        domain: str | None,
        stage: str,
        level: str = "info",
        detail: dict[str, Any] | None = None,
    ) -> None:
        from psycopg.types.json import Json

        pool = await self._get_pool()
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO structured_logs (trace_id, domain, stage, level, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (trace_id, domain, stage, level, Json(detail) if detail is not None else None),
            )

    async def stats(self) -> StatsSummary:
        pool = await self._get_pool()
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*), "
                "COALESCE(SUM(cost_usd) FILTER (WHERE created_at > now() - interval '1 day'), 0) "
                "FROM token_usage"
            )
            total_cost, llm_calls, cost_24h = await cur.fetchone()

            cur = await conn.execute(
                "SELECT model, COUNT(*), COALESCE(SUM(input_tokens), 0), "
                "COALESCE(SUM(output_tokens), 0), COALESCE(SUM(cost_usd), 0) "
                "FROM token_usage GROUP BY model ORDER BY model"
            )
            by_model = [
                ModelSpend(
                    model=m,
                    llm_calls=calls,
                    input_tokens=int(tin),
                    output_tokens=int(tout),
                    cost_usd=round(float(cost), 6),
                )
                for m, calls, tin, tout, cost in await cur.fetchall()
            ]

            cur = await conn.execute(
                "SELECT COUNT(*) FILTER (WHERE stage = 'request_completed'), "
                "COUNT(*) FILTER (WHERE stage = 'request_failed'), "
                "AVG((detail->>'latency_ms')::float) FILTER (WHERE stage = 'request_completed') "
                "FROM structured_logs"
            )
            completed, failed, avg_latency = await cur.fetchone()

        return _summary(
            total_cost=float(total_cost),
            cost_24h=float(cost_24h),
            llm_calls=int(llm_calls),
            by_model=by_model,
            completed=int(completed),
            failed=int(failed),
            avg_latency=float(avg_latency) if avg_latency is not None else None,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()


def get_repository() -> Repository:
    """Select persistence from config. Defaults to in-memory so the app and tests
    run offline; set LQ_PERSISTENCE=postgres for the compose stack.
    """
    if settings.persistence == "postgres":
        return PostgresRepository(settings.database_url)
    return InMemoryRepository()
