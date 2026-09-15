"""lead_qualifier — reusable core for domain-only lead qualification.

Public surface is deliberately small so the eval harness and the internal
pipeline can depend on it without reaching into internals.
"""

from .enrichment import enrich_domain
from .pipeline import qualify
from .errors import (
    CostCeilingExceeded,
    EnrichmentEmptyError,
    LLMCallError,
    PipelineError,
    SchemaValidationError,
)
from .schemas import (
    CompanyProfile,
    EnrichmentSource,
    LeadScore,
    QualifyRequest,
    QualifyResponse,
    Tier,
    TokenUsage,
)

__all__ = [
    "enrich_domain",
    "qualify",
    "CompanyProfile",
    "EnrichmentSource",
    "LeadScore",
    "QualifyRequest",
    "QualifyResponse",
    "Tier",
    "TokenUsage",
    "PipelineError",
    "EnrichmentEmptyError",
    "SchemaValidationError",
    "LLMCallError",
    "CostCeilingExceeded",
]
