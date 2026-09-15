"""Anthropic concrete provider. The only place a vendor SDK is imported.

Imported lazily so the package installs and tests run without the `anthropic`
extra. Selected via LQ_LLM_PROVIDER=anthropic.
"""

from __future__ import annotations

from ..errors import LLMCallError
from .base import Completion, LLMProvider, RawUsage


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMCallError(
                "anthropic package not installed; `pip install '.[anthropic]'`"
            ) from exc
        self._client = AsyncAnthropic(api_key=api_key)
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

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        )
        usage = RawUsage(
            model=self._model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        return Completion(text=text, usage=usage)
