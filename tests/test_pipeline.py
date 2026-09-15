"""Pipeline tests — offline. Enrichment is monkeypatched so we test the LLM
repair loop, cost accounting, and the cost ceiling in isolation.
"""

import pytest

import lead_qualifier.pipeline as pipe
from lead_qualifier.errors import CostCeilingExceeded, EnrichmentEmptyError, SchemaValidationError
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
