"""Typed errors that map to HTTP responses at the FastAPI boundary and to
`failure_stage` values in the dead-letter table.
"""

from __future__ import annotations


class PipelineError(Exception):
    """Base. `failure_stage` matches the dead_letters.failure_stage column."""

    failure_stage: str = "unknown"
    http_status: int = 500


class EnrichmentEmptyError(PipelineError):
    """Zero enrichment sources produced usable data (e.g. domain does not
    resolve). The only hard-failure case for enrichment (§4.9)."""

    failure_stage = "enrichment"
    http_status = 422


class SchemaValidationError(PipelineError):
    """LLM output failed schema validation after the bounded repair loop (§4.2)."""

    failure_stage = "schema"
    http_status = 422


class LLMCallError(PipelineError):
    """The LLM provider call itself failed (transport, auth, rate limit, or the
    per-call time budget ran out)."""

    failure_stage = "llm"
    http_status = 502


class LLMRefusalError(PipelineError):
    """The model declined the request. Re-sending the same prompt gets the same
    answer, so this is a 422 that n8n does not retry, not a retryable 502, and it
    skips the schema-repair loop, which would only re-ask twice more for nothing.
    """

    failure_stage = "llm"
    http_status = 422


class CostCeilingExceeded(PipelineError):
    """Daily spend ceiling hit before the LLM call (§4.7)."""

    failure_stage = "cost_ceiling"
    http_status = 503
