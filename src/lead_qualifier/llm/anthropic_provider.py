"""Anthropic concrete provider. The only place a vendor SDK is imported.

Imported lazily so the package installs and tests run without the `anthropic`
extra. Selected via LQ_LLM_PROVIDER=anthropic.
"""

from __future__ import annotations

from ..errors import LLMCallError, LLMRefusalError
from .base import Completion, LLMProvider, RawUsage


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str, *, timeout_s: float) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMCallError(
                "anthropic package not installed; `pip install '.[anthropic]'`"
            ) from exc
        # Both settings are load-bearing, not tuning:
        # - timeout: the SDK default is 10 minutes. §4.8 budgets 20s per call, and
        #   the n8n node's 75s timeout is computed from that. A call allowed to run
        #   longer makes n8n give up, see a transport failure, and re-send while
        #   this request is still spending — duplicate LLM cost.
        # - max_retries=0: the SDK retries 429/5xx/timeouts on its own by default,
        #   which would be a third, invisible retry layer. §4.2 deliberately has
        #   two — transport in n8n, schema repair in the pipeline — and a hidden
        #   one also multiplies the time budget by (retries + 1).
        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self._model = model

    async def complete(
        self, *, system: str, user: str, max_tokens: int = 1024
    ) -> Completion:
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001 - normalize to a typed error
            raise LLMCallError(str(exc)) from exc

        # A refusal arrives as HTTP 200 with no usable text. Left unchecked it
        # would look like malformed JSON and burn the whole repair loop.
        if resp.stop_reason == "refusal":
            raise LLMRefusalError(f"model declined the request ({self._model})")

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        usage = RawUsage(
            model=self._model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        return Completion(text=text, usage=usage)
