"""OpenRouter concrete provider — a second real implementation of the same
interface, proving the abstraction (§5e) holds for more than one vendor.

OpenRouter exposes an OpenAI-compatible endpoint, so this talks plain HTTP with
httpx rather than a vendor SDK. Selected via LQ_LLM_PROVIDER=openrouter.

Two things it does that the Anthropic provider cannot:
- it reads the *actual* charge for the call from `usage.cost` (OpenRouter credits
  are denominated 1:1 in USD) and reports it, so the pipeline books the real
  amount instead of a table estimate;
- it names a model with a provider-namespaced id (e.g. `anthropic/claude-haiku-4.5`),
  which is what gets recorded so a cost row says which route actually ran.
"""

from __future__ import annotations

from ..errors import LLMCallError, LLMRefusalError
from .base import Completion, LLMProvider, RawUsage

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class OpenRouterProvider(LLMProvider):
    def __init__(self, api_key: str, model: str, *, timeout_s: float) -> None:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise LLMCallError(
                "httpx not installed; `pip install '.[openrouter]'`"
            ) from exc
        # Same two load-bearing choices as the Anthropic provider (see its file):
        # an explicit per-call timeout the n8n node arithmetic depends on, and no
        # client-side retry — httpx does not retry by default, so this is just the
        # guarantee made explicit. The pipeline's own budget (§4.8) is the real
        # enforcer regardless; this keeps a single hung socket from reaching it.
        self._httpx = httpx
        self._client = httpx.AsyncClient(timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._model = model

    async def complete(
        self, *, system: str, user: str, max_tokens: int = 1024
    ) -> Completion:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = await self._client.post(_ENDPOINT, headers=self._headers, json=payload)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 - normalize to a typed error
            raise LLMCallError(str(exc)) from exc

        choice = (body.get("choices") or [{}])[0]
        finish = choice.get("finish_reason")
        text = (choice.get("message") or {}).get("content") or ""

        # A content-policy stop, or a 200 with no usable text, is the same failure
        # mode as an Anthropic refusal: re-sending the identical prompt changes
        # nothing, so it is a non-retryable 422 that skips the repair loop rather
        # than malformed JSON that burns it.
        if finish == "content_filter" or not text.strip():
            raise LLMRefusalError(
                f"model returned no usable text ({self._model}, finish_reason={finish})"
            )

        usage = body.get("usage") or {}
        cost = usage.get("cost")
        return Completion(
            text=text,
            usage=RawUsage(
                model=self._model,
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                # OpenRouter credits are 1:1 USD. Absent only on odd edge cases;
                # None then lets the COST_TABLE fallback book an estimate rather
                # than silently recording $0.
                cost_usd=float(cost) if cost is not None else None,
            ),
        )
