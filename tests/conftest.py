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

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def _force_offline_provider(monkeypatch):
    """The suite must run with no keys and no network (the README's promise), even
    on a machine whose .env selects a real provider — otherwise creating a real
    .env silently turns every /qualify test into a paid API call. Force the mock
    everywhere a test might reach a provider; the provider-specific tests
    construct their own client directly and are unaffected."""
    from lead_qualifier import api
    from lead_qualifier.config import settings
    from lead_qualifier.llm.mock import MockProvider

    monkeypatch.setattr(settings, "llm_provider", "mock")
    monkeypatch.setattr(api, "_provider", MockProvider(model="mock"), raising=False)
