"""Runtime configuration. All tunables live here so a reviewer sees a single
surface for thresholds, cost, and safety limits — never hardcoded in logic.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schemas import Tier


class Thresholds:
    """Score bands for routing. Held as one constant, not embedded in the n8n
    Switch node, so the hot/warm/cold cutoffs are tunable in one place.
    """

    HOT = 70
    WARM = 40

    @classmethod
    def tier_for(cls, score: int) -> Tier:
        if score >= cls.HOT:
            return Tier.HOT
        if score >= cls.WARM:
            return Tier.WARM
        return Tier.COLD


# Per-model cost in USD per 1M tokens (input, output). Used by token accounting.
# Extend when adding models; the provider-agnostic interface means the key is
# just the model id the provider reports.
COST_TABLE: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (15.00, 75.00),
    "mock": (0.0, 0.0),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rate_in, rate_out = COST_TABLE.get(model, (0.0, 0.0))
    return round(
        (input_tokens / 1_000_000) * rate_in
        + (output_tokens / 1_000_000) * rate_out,
        6,
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LQ_", env_file=".env", extra="ignore")

    # --- LLM ---
    llm_provider: str = "mock"  # "mock" | "anthropic"
    llm_model: str = "claude-haiku-4-5"
    anthropic_api_key: str | None = None

    # --- Safety / abuse ceiling (§4.7) ---
    webhook_auth_token: str | None = None
    daily_cost_ceiling_usd: float = 5.0

    # --- Timeouts (§4.8), seconds ---
    enrichment_timeout_s: float = 8.0
    llm_timeout_s: float = 20.0

    # --- Schema repair (§4.2) ---
    schema_repair_max_retries: int = 2

    # --- Optional hot-path action (§8) ---
    hot_path_webhook_url: str | None = None

    # --- Persistence ---
    persistence: str = "memory"  # "memory" | "postgres"
    database_url: str = "postgresql://lq:lq@localhost:5432/lead_qualifier"


settings = Settings()
