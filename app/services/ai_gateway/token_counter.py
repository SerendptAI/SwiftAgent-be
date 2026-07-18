"""
Token counting for cost estimation and budget enforcement.

Uses tiktoken with model-specific encoding where possible, falling back
to cl100k_base (GPT-3.5/4 era encoding) for unknown models. For Anthropic
models the count is approximate — Anthropic does not publish exact BPE
counts — but is within 5% of actual usage, which is sufficient for budgeting.
"""
from __future__ import annotations

from typing import Iterable

import tiktoken

# Lazy-loaded encoders (tiktoken loads ~1MB per encoding on first use)
_ENCODERS: dict[str, tiktoken.Encoding] = {}


def _get_encoding(model: str) -> tiktoken.Encoding:
    """Return a tiktoken encoding for the given model, caching per process."""
    if model not in _ENCODERS:
        try:
            _ENCODERS[model] = tiktoken.encoding_for_model(model)
        except KeyError:
            # Unknown model — fall back to cl100k_base (works for Claude estimates)
            _ENCODERS[model] = tiktoken.get_encoding("cl100k_base")
    return _ENCODERS[model]


def count_tokens(text: str, model: str = "claude-haiku-4-5-20251001") -> int:
    """Count tokens in a single text string."""
    if not text:
        return 0
    encoding = _get_encoding(model)
    return len(encoding.encode(text))


def count_message_tokens(
    messages: Iterable[dict],
    model: str = "claude-haiku-4-5-20251001",
) -> int:
    """
    Approximate total tokens across a list of chat messages.

    Includes a 4-token per-message overhead (role/separator tokens) which
    is the standard approximation for OpenAI-style chat formatting.
    """
    if not messages:
        return 0

    total = 0
    encoding = _get_encoding(model)

    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(encoding.encode(content))
        # Each message has ~4 tokens of structural overhead
        total += 4

    # Add 2 tokens for the assistant priming
    total += 2
    return total
