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
from .schemas import TokenUsage


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
