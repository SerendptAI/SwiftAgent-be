"""Tests for the instrumented LLM wrapper."""
import pytest
from unittest.mock import MagicMock, patch
from app.services.ai_gateway.instrumented_llm import (
    record_call_cost,
    CostRecord,
)


class TestRecordCallCost:
    def test_records_cost_in_metadata(self):
        # Mock the langfuse context
        with patch("langfuse.decorators.langfuse_context") as mock_ctx:
            record_call_cost(
                model="claude-haiku-4-5-20251001",
                provider="anthropic",
                input_tokens=1000,
                output_tokens=500,
            )
            mock_ctx.update_current_observation.assert_called_once()
            call_kwargs = mock_ctx.update_current_observation.call_args.kwargs
            assert "metadata" in call_kwargs
            meta = call_kwargs["metadata"]
            assert "cost_usd" in meta
            assert meta["cost_usd"] > 0
            assert meta["model"] == "claude-haiku-4-5-20251001"
            assert meta["provider"] == "anthropic"

    def test_handles_zero_tokens(self):
        with patch("langfuse.decorators.langfuse_context") as mock_ctx:
            record_call_cost(
                model="claude-haiku-4-5-20251001",
                provider="anthropic",
                input_tokens=0,
                output_tokens=0,
            )
            mock_ctx.update_current_observation.assert_called_once()
            meta = mock_ctx.update_current_observation.call_args.kwargs["metadata"]
            assert meta["cost_usd"] == 0.0

    def test_swallows_errors_silently(self, caplog):
        # If langfuse raises, should not break the chat flow
        import logging
        caplog.set_level(logging.DEBUG)
        with patch("langfuse.decorators.langfuse_context") as mock_ctx:
            mock_ctx.update_current_observation.side_effect = Exception("boom")
            # Should not raise
            record_call_cost(
                model="claude-haiku-4-5-20251001",
                provider="anthropic",
                input_tokens=100, output_tokens=50,
            )
        # Verify the error was logged
        assert any(
            "record_call_cost: langfuse context unavailable" in msg
            for msg in caplog.messages
        ), "Expected debug log about langfuse context"


class TestCostRecord:
    def test_dataclass_fields(self):
        rec = CostRecord(
            model="m", provider="p",
            input_tokens=10, output_tokens=5,
            cost_usd=0.001,
        )
        assert rec.cost_usd == 0.001
