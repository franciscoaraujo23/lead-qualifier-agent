"""Provider-agnostic LLM interface (§5e).

The provider is pure transport: it takes a system + user prompt and returns raw
text plus token usage. It does NOT parse or validate — schema validation and the
bounded repair loop are orchestrated by the pipeline (the caller), so the same
validation logic is reused by the future eval harness. This keeps vendor SDKs
out of the scoring logic and makes the core mockable in tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class RawUsage:
    """Token counts as reported by the provider. Cost is computed separately by
    the config cost table, so providers stay dumb about pricing.
    """

    model: str
    input_tokens: int
    output_tokens: int


@dataclass
class Completion:
    text: str
    usage: RawUsage


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self, *, system: str, user: str, max_tokens: int = 1024
    ) -> Completion: ...
