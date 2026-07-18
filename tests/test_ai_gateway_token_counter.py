"""Tests for the token counter utility."""
import pytest
from app.services.ai_gateway.token_counter import count_tokens, count_message_tokens


class TestCountTokens:
    def test_empty_string(self):
        assert count_tokens("") == 0

    def test_short_phrase(self):
        # "hello world" = 2 tokens
        n = count_tokens("hello world")
        assert 1 <= n <= 4

    def test_longer_text_scales(self):
        short = count_tokens("hello")
        long = count_tokens("hello " * 50)
        assert long > short

    def test_uses_anthropic_encoding_for_anthropic(self):
        # Should not raise for anthropic model
        n = count_tokens("test message", model="claude-haiku-4-5-20251001")
        assert n > 0


class TestCountMessageTokens:
    def test_single_human_message(self):
        msgs = [{"role": "user", "content": "hello world"}]
        n = count_message_tokens(msgs)
        # Should include message overhead (~4 tokens) + content
        assert n >= count_tokens("hello world")

    def test_empty_list(self):
        assert count_message_tokens([]) == 0

    def test_multiple_messages_accumulate(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you?"},
        ]
        n = count_message_tokens(msgs)
        assert n > 0
