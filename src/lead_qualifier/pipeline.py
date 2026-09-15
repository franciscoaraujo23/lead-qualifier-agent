"""Qualification pipeline: enrichment -> LLM -> schema-validated result.

Owns the schema-repair loop (§4.2) and the token-cost accounting + ceiling
(§4.6/§4.7). Transport lives in the provider; validation lives here so the eval
harness can reuse it.
"""

from __future__ import annotations

import re
import time

from pydantic import ValidationError

from .config import Thresholds, cost_usd, settings
from .enrichment import enrich_domain
from .errors import CostCeilingExceeded, SchemaValidationError
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
    score, usage = await _score_with_repair(provider, user)

    cost = cost_usd(usage.model, usage.input_tokens, usage.output_tokens)
    token_usage = TokenUsage(
        model=usage.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=cost,
    )
    await repo.record_usage(trace_id=trace_id, domain=domain, usage=token_usage)

    latency_ms = int((time.monotonic() - started) * 1000)
    await repo.log(
        trace_id=trace_id,
        domain=domain,
        stage="request_completed",
        detail={"score": score.score, "cost_usd": cost, "latency_ms": latency_ms},
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


async def _score_with_repair(provider: LLMProvider, user: str):
    """Call the LLM and validate; on schema failure, re-prompt with the error
    appended, up to the configured bound. This is a *different* retry from the
    transport retry n8n does — it changes the prompt each attempt.
    """
    prompt = user
    last_error = ""
    for attempt in range(settings.schema_repair_max_retries + 1):
        completion: Completion = await provider.complete(
            system=_SYSTEM, user=prompt, max_tokens=1024
        )
        try:
            score = LeadScore.model_validate_json(_extract_json(completion.text))
            return score, completion.usage
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
