"""
In-house AI infrastructure layer for SwiftAgent-be.

Owns: model pricing, complexity routing, token budgets, prompt compression,
per-request failover, and cost-attribution metadata. Sits between
`llm_factory.py` and every LangGraph node that calls a model.
"""
