"""Phase 0 contract tests: lock payload shapes and the LLM interface before any
real logic exists. No network, no keys — runs anywhere.
"""

import pytest
from pydantic import ValidationError

from lead_qualifier import (
    CompanyProfile,
    EnrichmentSource,
    LeadScore,
    QualifyRequest,
    Tier,
)
from lead_qualifier.config import Thresholds, cost_usd
from lead_qualifier.llm import LLMProvider, MockProvider


def test_qualify_request_normalizes_domain():
    assert QualifyRequest(domain="  HTTPS://Acme.com/path ").domain == "acme.com"


def test_qualify_request_rejects_non_domain():
    with pytest.raises(ValidationError):
        QualifyRequest(domain="notadomain")


def test_lead_score_bounds_enforced():
    with pytest.raises(ValidationError):
        LeadScore(score=101, confidence=0.5, reasoning="x", draft_message="y")
    with pytest.raises(ValidationError):
        LeadScore(score=50, confidence=1.5, reasoning="x", draft_message="y")


def test_lead_score_rejects_blank_text():
    with pytest.raises(ValidationError):
        LeadScore(score=50, confidence=0.5, reasoning="   ", draft_message="y")


@pytest.mark.parametrize(
    "score,expected",
    [(100, Tier.HOT), (70, Tier.HOT), (69, Tier.WARM), (40, Tier.WARM), (39, Tier.COLD), (0, Tier.COLD)],
)
def test_threshold_bands(score, expected):
    assert Thresholds.tier_for(score) == expected


def test_profile_completeness():
    full = CompanyProfile(
        domain="acme.com",
        resolved=True,
        sources_ok=list(EnrichmentSource),
    )
    assert full.completeness == 1.0
    half = CompanyProfile(
        domain="acme.com",
        resolved=True,
        sources_ok=[EnrichmentSource.DNS, EnrichmentSource.SCRAPE],
    )
    assert half.completeness == 0.5


def test_cost_table_known_model():
    # 1M input @ $1 + 1M output @ $5 = $6
    assert cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) == 6.0


def test_cost_table_unknown_model_is_free_not_error():
    assert cost_usd("some-future-model", 1000, 1000) == 0.0


async def test_mock_provider_satisfies_protocol_and_returns_valid_json():
    import json

    provider = MockProvider()
    assert isinstance(provider, LLMProvider)
    completion = await provider.complete(system="s", user="acme.com profile")
    # Transport contract: raw text that parses into a schema-valid LeadScore.
    parsed = LeadScore.model_validate(json.loads(completion.text))
    assert 0 <= parsed.score <= 100
    assert completion.usage.model == "mock"
