"""Payload contracts for the lead qualification pipeline.

These schemas are the single source of truth for every boundary in the system:
the webhook input, the enrichment output, the LLM output, and the endpoint
response. They are locked in Phase 0 before any logic is implemented so that
n8n, FastAPI, the eval harness, and the internal pipeline all agree on shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EnrichmentSource(str, Enum):
    DNS = "dns"
    WHOIS = "whois"
    SCRAPE = "scrape"
    TECH_FINGERPRINT = "tech_fingerprint"


class Tier(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


# --- Enrichment output -------------------------------------------------------


class DnsInfo(BaseModel):
    a_records: list[str] = Field(default_factory=list)
    mx_records: list[str] = Field(default_factory=list)
    ns_records: list[str] = Field(default_factory=list)


class WhoisInfo(BaseModel):
    registrar: str | None = None
    created_on: datetime | None = None
    expires_on: datetime | None = None
    country: str | None = None
    # WHOIS is flaky in the free tier (rate limits, redacted TLDs); redacted is
    # a normal, expected state, not a failure.
    redacted: bool = False


class SiteInfo(BaseModel):
    title: str | None = None
    description: str | None = None
    text_excerpt: str | None = None
    status_code: int | None = None


class CompanyProfile(BaseModel):
    """Assembled from best-effort enrichment. A source that fails is recorded in
    `sources_failed` and its section is left null, rather than sinking the whole
    request. `confidence`-relevant completeness is derived from `sources_ok`.
    """

    domain: str
    resolved: bool = Field(
        description="False when the domain does not resolve at all (hard failure upstream)."
    )
    dns: DnsInfo | None = None
    whois: WhoisInfo | None = None
    site: SiteInfo | None = None
    technologies: list[str] = Field(default_factory=list)
    sources_ok: list[EnrichmentSource] = Field(default_factory=list)
    sources_failed: list[EnrichmentSource] = Field(default_factory=list)
    enriched_at: datetime = Field(default_factory=_utcnow)

    @property
    def completeness(self) -> float:
        """Fraction of enrichment sources that produced usable data (0.0-1.0).

        Fed to the LLM so a thin profile yields a low-confidence score instead of
        a hallucinated one.
        """
        total = len(EnrichmentSource)
        return round(len(self.sources_ok) / total, 3) if total else 0.0


# --- LLM output contract -----------------------------------------------------


class LeadScore(BaseModel):
    """Exact shape the LLM must return. Validated in FastAPI; invalid output is
    repaired (bounded re-prompt) or surfaced as a typed 422 — it never reaches
    n8n as malformed data.
    """

    score: int = Field(ge=0, le=100, description="Fit/intent score, 0-100.")
    confidence: float = Field(
        ge=0.0, le=1.0, description="Model's certainty given profile completeness."
    )
    reasoning: str = Field(min_length=1, description="Short rationale for the score.")
    draft_message: str = Field(
        min_length=1, description="Drafted outreach message for this lead."
    )

    @field_validator("reasoning", "draft_message")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


# --- Request / response boundary --------------------------------------------


class QualifyRequest(BaseModel):
    domain: str = Field(min_length=3, description="Bare domain, e.g. 'acme.com'.")

    @field_validator("domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        v = v.strip().lower()
        for prefix in ("http://", "https://"):
            if v.startswith(prefix):
                v = v[len(prefix):]
        v = v.split("/")[0]
        if "." not in v:
            raise ValueError("not a valid domain")
        return v


class TokenUsage(BaseModel):
    model: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)


class QualifyResponse(BaseModel):
    """The enriched decision returned to n8n. `tier` is computed server-side for
    convenience/observability; n8n still owns the authoritative routing Switch.
    """

    domain: str
    trace_id: str
    profile: CompanyProfile
    score: LeadScore
    tier: Tier
    usage: TokenUsage
    latency_ms: int = Field(ge=0)
    qualified_at: datetime = Field(default_factory=_utcnow)


# --- Observability (§4.6 cost, §4.5 latency/failure-rate) --------------------


class ModelSpend(BaseModel):
    """Per-model slice of spend. `llm_calls` counts every billed attempt,
    including schema-repair retries, so calls > leads reveals repair overhead."""

    model: str
    llm_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)


class StatsSummary(BaseModel):
    """What `/stats` (§4.6) reports: running spend and the two numbers a technical
    reviewer actually checks — cost per delivered lead and how the LLM behaved.
    """

    # Cost (§4.6)
    total_cost_usd: float = Field(ge=0.0)
    cost_usd_24h: float = Field(ge=0.0)
    leads: int = Field(ge=0)  # requests that produced a validated result
    llm_calls: int = Field(ge=0)  # includes repair retries; llm_calls/leads = overhead
    # Cost per delivered lead, wasted spend on failed requests included on purpose:
    # it answers "what did each result actually cost us", not "cost of a clean run".
    cost_per_lead_usd: float = Field(ge=0.0)
    by_model: list[ModelSpend] = Field(default_factory=list)

    # Cost ceiling (§4.7)
    daily_ceiling_usd: float = Field(ge=0.0)
    ceiling_used_pct: float = Field(ge=0.0)

    # Reliability (§4.5)
    requests_completed: int = Field(ge=0)
    requests_failed: int = Field(ge=0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    avg_latency_ms: int | None = None
