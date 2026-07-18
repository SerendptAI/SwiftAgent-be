"""
LLM call instrumentation — records cost on the active Langfuse span.

Use after every model invocation to write cost metadata onto the current
observation. The function is a no-op when Langfuse is disabled or when
called outside an @observe()-decorated context.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.ai_gateway.pricing import estimate_cost

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CostRecord:
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


def record_call_cost(
    *,
    model: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
) -> CostRecord:
    """
    Estimate the cost of an LLM call and attach it to the current Langfuse
    observation's metadata. Returns the CostRecord for caller-side use
    (e.g., accumulating in agent state for budget enforcement).

    Never raises — observability failures must not break chat.
    """
    cost_usd = estimate_cost(model, provider, input_tokens, output_tokens)

    record = CostRecord(
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )

    try:
        # Lazy import: langfuse_context is only present when @observe is active
        from langfuse.decorators import langfuse_context

        langfuse_context.update_current_observation(
            metadata={
                "model": model,
                "provider": provider,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
            }
        )
    except (ImportError, Exception) as exc:
        # Not in an @observe context, or langfuse is disabled — that's fine.
        logger.debug("record_call_cost: langfuse context unavailable: %s", exc)

    return record
