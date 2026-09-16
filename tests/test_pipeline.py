"""Pipeline tests — offline. Enrichment is monkeypatched so we test the LLM
repair loop, cost accounting, and the cost ceiling in isolation.
"""

import asyncio

import pytest

import lead_qualifier.pipeline as pipe
from lead_qualifier.errors import (
    CostCeilingExceeded,
    EnrichmentEmptyError,
    LLMCallError,
    SchemaValidationError,
)
from lead_qualifier.llm.base import Completion, RawUsage
from lead_qualifier.persistence import InMemoryRepository
from lead_qualifier.schemas import CompanyProfile, EnrichmentSource, Tier


@pytest.fixture
def profile():
    return CompanyProfile(
        domain="acme.com",
        resolved=True,
        technologies=["nginx"],
        sources_ok=list(EnrichmentSource),
    )


@pytest.fixture(autouse=True)
def _patch_enrich(monkeypatch, profile):
    async def fake_enrich(domain):
        return profile
    monkeypatch.setattr(pipe, "enrich_domain", fake_enrich)


class ScriptedProvider:
    """Returns queued completions in order; lets us script invalid-then-valid."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = 0

    async def complete(self, *, system, user, max_tokens=1024):
        self.calls += 1
        text = self._texts.pop(0)
        return Completion(text=text, usage=RawUsage("mock", 10, 20))


_VALID = '{"score": 82, "confidence": 0.9, "reasoning": "good fit", "draft_message": "hi"}'


async def test_happy_path_scores_and_routes():
    provider = ScriptedProvider([_VALID])
    repo = InMemoryRepository()
    resp = await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t1")
    assert resp.score.score == 82
    assert resp.tier == Tier.HOT
    assert resp.trace_id == "t1"
    assert provider.calls == 1


async def test_repair_loop_recovers_from_invalid_then_valid():
    provider = ScriptedProvider(["not json at all", _VALID])
    repo = InMemoryRepository()
    resp = await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t2")
    assert resp.score.score == 82
    assert provider.calls == 2  # one repair attempt used


async def test_repair_loop_exhausts_and_raises(monkeypatch):
    # max_retries default = 2 -> 3 total attempts, all invalid.
    provider = ScriptedProvider(["bad", "still bad", "nope"])
    repo = InMemoryRepository()
    with pytest.raises(SchemaValidationError):
        await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t3")
    assert provider.calls == 3


async def test_json_fences_are_stripped():
    provider = ScriptedProvider(["```json\n" + _VALID + "\n```"])
    repo = InMemoryRepository()
    resp = await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t4")
    assert resp.score.score == 82


async def test_cost_is_recorded():
    provider = ScriptedProvider([_VALID])
    repo = InMemoryRepository()
    await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t5")
    # mock model is free, so cost stays 0 but a usage row exists.
    assert await repo.daily_cost_usd() == 0.0


async def test_cost_ceiling_blocks_before_work(monkeypatch):
    provider = ScriptedProvider([_VALID])

    class FullRepo(InMemoryRepository):
        async def daily_cost_usd(self):
            return 999.0

    with pytest.raises(CostCeilingExceeded):
        await pipe.qualify("acme.com", provider=provider, repo=FullRepo(), trace_id="t6")
    assert provider.calls == 0  # gated before any LLM call


async def test_enrichment_empty_propagates(monkeypatch):
    async def fake_empty(domain):
        raise EnrichmentEmptyError("no data")
    monkeypatch.setattr(pipe, "enrich_domain", fake_empty)
    provider = ScriptedProvider([_VALID])
    with pytest.raises(EnrichmentEmptyError):
        await pipe.qualify("x.com", provider=provider, repo=InMemoryRepository(), trace_id="t7")


async def test_llm_call_over_budget_fails_fast_without_repair_attempts(monkeypatch):
    """§4.8: the n8n node timeout is computed from this per-call budget. A call
    that outlives it must fail as a typed error, not run on while n8n gives up
    and re-sends."""
    monkeypatch.setattr(pipe.settings, "llm_timeout_s", 0.05)

    class HangingProvider:
        calls = 0

        async def complete(self, *, system, user, max_tokens=1024):
            HangingProvider.calls += 1
            await asyncio.sleep(10)

    with pytest.raises(LLMCallError, match="budget"):
        await pipe.qualify(
            "acme.com", provider=HangingProvider(), repo=InMemoryRepository(), trace_id="t8"
        )
    assert HangingProvider.calls == 1, "a timeout is not a schema problem; no repair loop"


class PricedProvider(ScriptedProvider):
    """ScriptedProvider on a billed model. The mock model costs $0, which is
    exactly how an accounting leak stays invisible."""

    async def complete(self, *, system, user, max_tokens=1024):
        self.calls += 1
        return Completion(
            text=self._texts.pop(0), usage=RawUsage("claude-haiku-4-5", 1_000_000, 0)
        )


async def test_repaired_request_is_charged_for_every_attempt():
    # 2 attempts x 1M input tokens x $1/1M = $2.00, not the $1.00 of the last call.
    provider = PricedProvider(["not json", _VALID])
    repo = InMemoryRepository()
    resp = await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t9")

    assert await repo.daily_cost_usd() == 2.0
    assert resp.usage.cost_usd == 2.0
    assert resp.usage.input_tokens == 2_000_000
    completed = next(e for e in repo.logs if e["stage"] == "request_completed")
    assert completed["detail"]["llm_attempts"] == 2


class ReportedCostProvider(ScriptedProvider):
    """A provider that returns its own authoritative charge (OpenRouter does).
    Its model id is deliberately absent from COST_TABLE, so the table would book
    $0 — the reported figure is the only way the cost is not silently lost."""

    async def complete(self, *, system, user, max_tokens=1024):
        self.calls += 1
        return Completion(
            text=self._texts.pop(0),
            usage=RawUsage("anthropic/claude-haiku-4.5", 1_000_000, 500, cost_usd=0.0075),
        )


async def test_provider_reported_cost_is_booked_verbatim_not_the_table():
    provider = ReportedCostProvider([_VALID])
    repo = InMemoryRepository()
    resp = await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t11")

    # Table lookup for this unknown model id is $0; the reported charge is what counts.
    assert resp.usage.cost_usd == 0.0075
    assert await repo.daily_cost_usd() == 0.0075


async def test_exhausted_repair_loop_still_books_its_spend():
    """Three billed calls that end in SchemaValidationError must reach the daily
    ceiling's view, or the ceiling is blind in the case it exists for."""
    provider = PricedProvider(["bad", "still bad", "nope"])
    repo = InMemoryRepository()
    with pytest.raises(SchemaValidationError):
        await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t10")
    assert await repo.daily_cost_usd() == 3.0


async def test_failed_request_leaves_a_trace():
    provider = PricedProvider(["bad", "still bad", "nope"])
    repo = InMemoryRepository()
    with pytest.raises(SchemaValidationError):
        await pipe.qualify("acme.com", provider=provider, repo=repo, trace_id="t11")

    failed = [e for e in repo.logs if e["stage"] == "request_failed"]
    assert len(failed) == 1
    assert failed[0]["trace_id"] == "t11"
    assert failed[0]["level"] == "error"
    assert failed[0]["detail"] == {"failure_stage": "schema", "error": "SchemaValidationError"}
