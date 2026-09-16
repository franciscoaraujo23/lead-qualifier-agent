"""Qualification pipeline: enrichment -> LLM -> schema-validated result.

Owns the schema-repair loop (§4.2) and the token-cost accounting + ceiling
(§4.6/§4.7). Transport lives in the provider; validation lives here so the eval
harness can reuse it.
"""

from __future__ import annotations

import asyncio
import re
import time

from pydantic import ValidationError

from .config import Thresholds, cost_usd, settings
from .enrichment import enrich_domain
from .errors import CostCeilingExceeded, LLMCallError, PipelineError, SchemaValidationError
from .llm.base import Completion, LLMProvider
from .persistence import Repository
from .schemas import CompanyProfile, LeadScore, QualifyResponse, TokenUsage

_SYSTEM = (
    "You are a B2B lead qualification analyst. Given a company profile assembled "
    "from public sources, score the lead's fit and buying intent from 0 to 100, "
    "state your confidence (0.0-1.0) calibrated to how complete the profile is, "
    "give a short reasoning, and draft a concise outreach message. "
    "Respond with ONLY a JSON object matching this schema, no prose, no code fences:\n"
    '{"score": int 0-100, "confidence": float 0.0-1.0, "reasoning": str, "draft_message": str}'
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _build_user_prompt(profile: CompanyProfile) -> str:
    return (
        f"Company profile (JSON). Missing fields are null; a thin profile should "
        f"lower your confidence:\n{profile.model_dump_json(indent=2)}"
    )


def _extract_json(text: str) -> str:
    return _FENCE.sub("", text).strip()


async def qualify(
    domain: str,
    *,
    provider: LLMProvider,
    repo: Repository,
    trace_id: str,
) -> QualifyResponse:
    try:
        return await _qualify(domain, provider=provider, repo=repo, trace_id=trace_id)
    except PipelineError as exc:
        # §4.5: a failed request must leave a trace too, or "everything that
        # happened for domain X" silently stops at its last successful stage.
        await repo.log(
            trace_id=trace_id,
            domain=domain,
            stage="request_failed",
            level="error",
            detail={"failure_stage": exc.failure_stage, "error": exc.__class__.__name__},
        )
        raise


async def _qualify(
    domain: str,
    *,
    provider: LLMProvider,
    repo: Repository,
    trace_id: str,
) -> QualifyResponse:
    # Cost ceiling gate (§4.7) — bound worst-case spend before doing LLM work.
    if await repo.daily_cost_usd() >= settings.daily_cost_ceiling_usd:
        raise CostCeilingExceeded(
            f"daily cost ceiling of ${settings.daily_cost_ceiling_usd} reached"
        )

    started = time.monotonic()
    await repo.log(trace_id=trace_id, domain=domain, stage="enrichment_started")
    profile = await enrich_domain(domain)
    await repo.log(
        trace_id=trace_id,
        domain=domain,
        stage="enrichment_completed",
        detail={
            "sources_ok": [s.value for s in profile.sources_ok],
            "completeness": profile.completeness,
        },
    )

    user = _build_user_prompt(profile)
    score, token_usage, attempts = await _score_with_repair(
        provider, user, repo=repo, trace_id=trace_id, domain=domain
    )

    latency_ms = int((time.monotonic() - started) * 1000)
    await repo.log(
        trace_id=trace_id,
        domain=domain,
        stage="request_completed",
        detail={
            "score": score.score,
            "cost_usd": token_usage.cost_usd,
            "llm_attempts": attempts,
            "latency_ms": latency_ms,
        },
    )
    return QualifyResponse(
        domain=domain,
        trace_id=trace_id,
        profile=profile,
        score=score,
        tier=Thresholds.tier_for(score.score),
        usage=token_usage,
        latency_ms=latency_ms,
    )


async def _score_with_repair(
    provider: LLMProvider,
    user: str,
    *,
    repo: Repository,
    trace_id: str,
    domain: str,
) -> tuple[LeadScore, TokenUsage, int]:
    """Call the LLM and validate; on schema failure, re-prompt with the error
    appended, up to the configured bound. This is a *different* retry from the
    transport retry n8n does — it changes the prompt each attempt.

    Every attempt is billed by the provider, so every attempt is recorded the
    moment it returns — including the invalid ones, and including a loop that
    ends in SchemaValidationError. Recording only the successful call would
    understate cost-per-lead and, worse, blind the daily ceiling (§4.7) in
    exactly the pathological case it exists for: a model returning garbage
    over and over.
    """
    prompt = user
    last_error = ""
    total = TokenUsage(model="", input_tokens=0, output_tokens=0, cost_usd=0.0)
    for attempt in range(1, settings.schema_repair_max_retries + 2):
        completion = await _complete_within_budget(provider, prompt)
        spent = TokenUsage(
            model=completion.usage.model,
            input_tokens=completion.usage.input_tokens,
            output_tokens=completion.usage.output_tokens,
            cost_usd=cost_usd(
                completion.usage.model,
                completion.usage.input_tokens,
                completion.usage.output_tokens,
            ),
        )
        await repo.record_usage(trace_id=trace_id, domain=domain, usage=spent)
        total = TokenUsage(
            model=spent.model,
            input_tokens=total.input_tokens + spent.input_tokens,
            output_tokens=total.output_tokens + spent.output_tokens,
            cost_usd=round(total.cost_usd + spent.cost_usd, 6),
        )
        try:
            score = LeadScore.model_validate_json(_extract_json(completion.text))
            return score, total, attempt
        except ValidationError as exc:
            last_error = str(exc)
            prompt = (
                f"{user}\n\nYour previous response failed validation:\n{last_error}\n"
                f"Return ONLY corrected JSON matching the schema."
            )

    raise SchemaValidationError(
        f"LLM output failed schema validation after "
        f"{settings.schema_repair_max_retries + 1} attempts: {last_error}"
    )


async def _complete_within_budget(provider: LLMProvider, prompt: str) -> Completion:
    """One LLM call, bounded by §4.8's per-call budget.

    Enforced here rather than trusted to each provider, so the guarantee the n8n
    node timeout is computed from (enrichment + attempts x this budget) holds
    for any provider, including ones that ignore their own timeout settings.
    """
    try:
        return await asyncio.wait_for(
            provider.complete(system=_SYSTEM, user=prompt, max_tokens=1024),
            timeout=settings.llm_timeout_s,
        )
    except asyncio.TimeoutError as exc:
        raise LLMCallError(
            f"LLM call exceeded its {settings.llm_timeout_s}s budget"
        ) from exc
