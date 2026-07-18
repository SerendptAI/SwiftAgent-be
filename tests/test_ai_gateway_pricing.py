"""Tests for the model pricing registry."""
import pytest
from app.services.ai_gateway.pricing import (
    ModelPricing,
    estimate_cost,
    lookup_pricing,
    PRICING_REGISTRY,
)


class TestLookupPricing:
    def test_known_anthropic_model(self):
        p = lookup_pricing("claude-haiku-4-5-20251001", provider="anthropic")
        assert isinstance(p, ModelPricing)
        assert p.input_per_1m > 0
        assert p.output_per_1m > 0

    def test_known_gemini_model(self):
        p = lookup_pricing("gemini-2.5-flash", provider="gemini")
        assert p.input_per_1m > 0

    def test_unknown_model_falls_back_to_default(self):
        p = lookup_pricing("gpt-99-unknown", provider="openai")
        # Should return default pricing, not raise
        assert p.input_per_1m >= 0
        assert p.output_per_1m >= 0

    def test_pricing_is_immutable(self):
        p = lookup_pricing("claude-haiku-4-5-20251001", provider="anthropic")
        with pytest.raises(Exception):
            p.input_per_1m = 999.0  # frozen dataclass


class TestEstimateCost:
    def test_zero_tokens_returns_zero(self):
        cost = estimate_cost("claude-haiku-4-5-20251001", "anthropic", 0, 0)
        assert cost == 0.0

    def test_haiku_typical_request(self):
        # 1000 input, 500 output tokens
        cost = estimate_cost(
            "claude-haiku-4-5-20251001", "anthropic", 1000, 500
        )
        # haiku: $0.80 input, $4.00 output per 1M (as of 2026-07)
        # expected: (1000/1e6)*0.80 + (500/1e6)*4.00 = 0.0008 + 0.002 = 0.0028
        assert 0.002 <= cost <= 0.004

    def test_cost_grows_with_tokens(self):
        small = estimate_cost("claude-haiku-4-5-20251001", "anthropic", 100, 50)
        large = estimate_cost("claude-haiku-4-5-20251001", "anthropic", 1000, 500)
        assert large > small

    def test_registry_has_at_least_three_models(self):
        assert len(PRICING_REGISTRY) >= 3
