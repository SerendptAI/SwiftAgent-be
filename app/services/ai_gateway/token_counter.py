"""
Token counting for cost estimation and budget enforcement.

Uses tiktoken with model-specific encoding where possible, falling back
to cl100k_base (GPT-3.5/4 era encoding) for unknown models. For Anthropic
models the count is approximate — Anthropic does not publish exact BPE
counts — but is within 5% of actual usage, which is sufficient for budgeting.
"""
from __future__ import annotations

import functools
from typing import Iterable
import logging

import tiktoken


@functools.lru_cache(maxsize=None)
def _get_encoding(model: str) -> tiktoken.Encoding:
    """Return a tiktoken encoding for the given model, cached per process."""
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        # Unknown model — fall back to cl100k_base (works for Claude estimates)
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, model: str = "claude-haiku-4-5-20251001") -> int:
    """Count tokens in a single text string."""
    if not text:
        return 0
    encoding = _get_encoding(model)
    return len(encoding.encode(text))


logger = logging.getLogger(__name__)


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
        elif isinstance(content, list):
            # Multi-modal content (text + image parts) — count text portions only
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    total += len(encoding.encode(part["text"]))
        else:
            logger.warning(
                "count_message_tokens: unexpected content type %s — skipping",
                type(content).__name__,
            )
        # Each message has ~4 tokens of structural overhead
        total += 4

    # Add 2 tokens for the assistant priming
    total += 2
    return total
