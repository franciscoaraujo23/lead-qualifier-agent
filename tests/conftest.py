"""Shared test setup.

On Windows, Python defaults to the Proactor event loop, which psycopg cannot use
in async mode — it raises `InterfaceError` on connect. Without this, the Postgres
integration test skips on every Windows machine with a message blaming the
database, when the cause is the event loop.

This runs at import time, not in a fixture: pytest-asyncio creates its loop from
the active policy during collection, which is before any fixture would run.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
