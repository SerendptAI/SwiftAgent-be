"""
In-house AI infrastructure layer for SwiftAgent-be.

Owns: model pricing, complexity routing, token budgets, prompt compression,
per-request failover, and cost-attribution metadata. Sits between
`llm_factory.py` and every LangGraph node that calls a model.
"""

from app.services.ai_gateway.instrumented_llm import CostRecord, record_call_cost
from app.services.ai_gateway.pricing import ModelPricing, estimate_cost, lookup_pricing
from app.services.ai_gateway.token_counter import count_message_tokens, count_tokens

__all__ = [
    "CostRecord",
    "ModelPricing",
    "count_message_tokens",
    "count_tokens",
    "estimate_cost",
    "lookup_pricing",
    "record_call_cost",
]
