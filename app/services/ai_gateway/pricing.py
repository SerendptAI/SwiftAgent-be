"""
Model pricing registry for cost estimation.

All prices are USD per 1M tokens. Sources are documented per-model and
should be reviewed quarterly. Update via PR — never edit at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPricing:
    model: str
    provider: str
    input_per_1m: float   # USD per 1M input tokens
    output_per_1m: float  # USD per 1M output tokens
    notes: str = ""


# Pricing as of 2026-07. Verify against provider pricing pages before changes.
# See: https://docs.anthropic.com/en/docs/about-claude/pricing
#      https://ai.google.dev/pricing
#      https://openrouter.ai/models
PRICING_REGISTRY: dict[str, ModelPricing] = {
    # Anthropic
    "claude-haiku-4-5-20251001": ModelPricing(
        model="claude-haiku-4-5-20251001", provider="anthropic",
        input_per_1m=0.80, output_per_1m=4.00,
        notes="Default orchestrator + agent model",
    ),
    "claude-sonnet-4-5-20250929": ModelPricing(
        model="claude-sonnet-4-5-20250929", provider="anthropic",
        input_per_1m=3.00, output_per_1m=15.00,
        notes="Capable model — used for complex reasoning tier",
    ),
    # Google Gemini
    "gemini-2.5-flash": ModelPricing(
        model="gemini-2.5-flash", provider="gemini",
        input_per_1m=0.075, output_per_1m=0.30,
        notes="Fast/cheap default for Gemini",
    ),
    "gemini-2.5-pro": ModelPricing(
        model="gemini-2.5-pro", provider="gemini",
        input_per_1m=1.25, output_per_1m=5.00,
        notes="Capable model for Gemini",
    ),
    # OpenRouter (free tier proxy through OpenRouter)
    "google/gemma-4-31b-it:free": ModelPricing(
        model="google/gemma-4-31b-it:free", provider="openrouter",
        input_per_1m=0.0, output_per_1m=0.0,
        notes="Free model — cost is $0",
    ),
}

# Fallback when model is unknown: assume mid-tier Claude pricing
_DEFAULT_PRICING = ModelPricing(
    model="__default__", provider="unknown",
    input_per_1m=3.00, output_per_1m=15.00,
    notes="Conservative fallback for unknown models",
)


def lookup_pricing(model: str, provider: str = "") -> ModelPricing:
    """
    Return pricing for a model. Falls back to default if model is not in
    the registry. Provider hint is used only when the model name is ambiguous.
    """
    if model in PRICING_REGISTRY:
        return PRICING_REGISTRY[model]

    # Try provider-prefixed match (e.g. "anthropic/claude-...")
    for key, pricing in PRICING_REGISTRY.items():
        if key.endswith(model) and (not provider or pricing.provider == provider):
            return pricing

    return _DEFAULT_PRICING


def estimate_cost(
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """
    Estimate the cost in USD for a single LLM call.

    Returns 0.0 when both input and output token counts are non-positive
    (zero or negative). Costs below $0.000001 are rounded to 0.0 to
    avoid float noise in dashboards.
    """
    if input_tokens <= 0 and output_tokens <= 0:
        return 0.0

    pricing = lookup_pricing(model, provider)
    cost = (input_tokens / 1_000_000.0) * pricing.input_per_1m \
         + (output_tokens / 1_000_000.0) * pricing.output_per_1m

    # Round to avoid 1e-9 noise
    return round(cost, 6)
