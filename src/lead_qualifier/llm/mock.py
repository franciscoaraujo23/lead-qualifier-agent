"""Deterministic mock provider.

Locks the transport contract and lets the whole pipeline (and the future eval
harness) run with zero API keys. Returns schema-valid JSON; the score varies by
prompt so different domains produce different results, but is stable per prompt.
"""

from __future__ import annotations

import hashlib
import json

from .base import Completion, LLMProvider, RawUsage


class MockProvider(LLMProvider):
    def __init__(self, model: str = "mock") -> None:
        self._model = model

    async def complete(
        self, *, system: str, user: str, max_tokens: int = 1024
    ) -> Completion:
        digest = hashlib.sha256(user.encode("utf-8")).hexdigest()
        score = 40 + int(digest[:8], 16) % 61  # 40..100, stable per prompt
        payload = {
            "score": score,
            "confidence": 0.7,
            "reasoning": "Deterministic mock score derived from the prompt hash.",
            "draft_message": (
                "Hi — I came across your company and think there may be a strong "
                "fit. Would a short conversation be worthwhile?"
            ),
        }
        text = json.dumps(payload)
        usage = RawUsage(
            model=self._model,
            input_tokens=len(user) // 4,
            output_tokens=len(text) // 4,
        )
        return Completion(text=text, usage=usage)
