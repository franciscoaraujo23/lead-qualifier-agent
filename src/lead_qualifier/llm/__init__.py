from ..config import settings
from .base import Completion, LLMProvider, RawUsage
from .mock import MockProvider

__all__ = [
    "LLMProvider",
    "Completion",
    "RawUsage",
    "MockProvider",
    "get_provider",
]


def get_provider() -> LLMProvider:
    """Select the provider from config. Defaults to the mock so the app and
    tests run offline; set LQ_LLM_PROVIDER=anthropic for a real model.
    """
    if settings.llm_provider == "anthropic":
        from .anthropic_provider import AnthropicProvider

        if not settings.anthropic_api_key:
            raise RuntimeError("LQ_ANTHROPIC_API_KEY is required for the anthropic provider")
        return AnthropicProvider(
            settings.anthropic_api_key, settings.llm_model, timeout_s=settings.llm_timeout_s
        )
    if settings.llm_provider == "openrouter":
        from .openrouter_provider import OpenRouterProvider

        if not settings.openrouter_api_key:
            raise RuntimeError("LQ_OPENROUTER_API_KEY is required for the openrouter provider")
        return OpenRouterProvider(
            settings.openrouter_api_key,
            settings.openrouter_model,
            timeout_s=settings.llm_timeout_s,
        )
    return MockProvider(model="mock")
