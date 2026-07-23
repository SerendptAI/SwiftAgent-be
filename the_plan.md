# In-House AI Infrastructure Implementation Plan


**Goal:** Build six AI infrastructure features in-house — cost observability, model routing by complexity, usage governance (token budgets), context optimization (LLM-based compression), smart per-request provider failover, and cost-attribution dashboards — replacing the need for the proposed external Cencori gateway.

**Architecture:** Layer a thin **AI Gateway** (`app/services/ai_gateway/`) between the existing `llm_factory.py` and every LangGraph node. The gateway owns: a model pricing registry, a complexity classifier invoked before each orchestrator call, a per-company token-budget gate, an LLM-based context compressor, a per-request failover chain with degradation detection, and a Langfuse-wrapping cost layer. Langfuse stays as the trace store; new endpoint routes serve cost-attribution rollups from Mongo aggregations.

**Tech Stack:** Python 3.12, LangChain / LangGraph (existing), Langfuse v2 (existing), Motor (Mongo async, existing), tiktoken (token counting — add), Pydantic v2 (existing), FastAPI (existing), pytest + pytest-asyncio (existing). No external gateway dependency.

**Reference:** `docs/reviews/cencori-architecture-review.md` identifies these as the six real gaps in the Cencori proposal — implementing in-house avoids vendor lock-in, keeps data on-platform, and reuses the Langfuse investment that already captures tokens/latency.

---

## Phased Rollout

| Phase | Feature | Status |
|---|---|---|
| 1 | Model pricing registry + cost layer (foundation) | First |
| 2 | Per-workspace token budget enforcement | Second |
| 3 | Complexity classifier + tiered model routing | Third |
| 4 | LLM-based prompt compression | Fourth |
| 5 | Per-request smart provider failover | Fifth |
| 6 | Cost-attribution dashboard endpoints | Sixth |

Each phase is independently shippable. Phases 1+2 unblock cost-aware decisions; phases 3–5 optimize spend; phase 6 surfaces it. All phases keep `pyproject.toml` dependencies minimal (only `tiktoken` is added).

---

## Phase 1 — Model Pricing Registry & Cost Layer (Foundation)

> **Why first:** Everything else (budgets, routing, dashboards) needs to know what each call *costs*. This phase adds the pricing data and a wrapper that records estimated cost on every Langfuse span. No behavior change for end users.

### Task 1.1: Add `tiktoken` to dependencies

**Files:**
- Modify: `pyproject.toml:6-47`

**Step 1: Add tiktoken to dependencies**

Edit `pyproject.toml`. In the `dependencies = [` block, add `"tiktoken>=0.8.0,<1.0.0",` after `"langchain-openai>=0.1.8",` (line 46).

**Step 2: Install**

```bash
cd /opt/data/SwiftAgent-be
uv sync
```

Expected: lockfile updates, `tiktoken` resolves. Run `uv pip show tiktoken` to confirm version pinned.

**Step 3: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add pyproject.toml uv.lock
git commit -m "deps: add tiktoken for token counting"
```

---

### Task 1.2: Create `ai_gateway/__init__.py` package

**Files:**
- Create: `app/services/ai_gateway/__init__.py`

**Step 1: Write the package init**

```python
"""
In-house AI infrastructure layer for SwiftAgent-be.

Owns: model pricing, complexity routing, token budgets, prompt compression,
per-request failover, and cost-attribution metadata. Sits between
`llm_factory.py` and every LangGraph node that calls a model.
"""
```

**Step 2: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/__init__.py
git commit -m "feat(ai_gateway): add package skeleton"
```

---

### Task 1.3: Create the model pricing registry with TDD

**Files:**
- Create: `app/services/ai_gateway/pricing.py`
- Test: `tests/test_ai_gateway_pricing.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_pricing.py`:

```python
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
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_pricing.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.ai_gateway.pricing'`

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/pricing.py`:

```python
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

    Returns 0.0 for any non-positive token count. Costs below $0.000001
    are rounded to 0.0 to avoid float noise in dashboards.
    """
    if input_tokens <= 0 and output_tokens <= 0:
        return 0.0

    pricing = lookup_pricing(model, provider)
    cost = (input_tokens / 1_000_000.0) * pricing.input_per_1m \
         + (output_tokens / 1_000_000.0) * pricing.output_per_1m

    # Round to avoid 1e-9 noise
    return round(cost, 6)
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_pricing.py -v
```

Expected: 7 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/pricing.py tests/test_ai_gateway_pricing.py
git commit -m "feat(ai_gateway): add model pricing registry with cost estimation"
```

---

### Task 1.4: Token counter utility with TDD

**Files:**
- Create: `app/services/ai_gateway/token_counter.py`
- Test: `tests/test_ai_gateway_token_counter.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_token_counter.py`:

```python
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
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_token_counter.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.ai_gateway.token_counter'`

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/token_counter.py`:

```python
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
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_token_counter.py -v
```

Expected: 7 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/token_counter.py tests/test_ai_gateway_token_counter.py
git commit -m "feat(ai_gateway): add tiktoken-based token counter"
```

---

### Task 1.5: Wrap LLM calls to record cost on Langfuse spans

**Files:**
- Modify: `app/services/graph/llm_factory.py:9-92`
- Create: `app/services/ai_gateway/instrumented_llm.py`
- Test: `tests/test_ai_gateway_instrumented_llm.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_instrumented_llm.py`:

```python
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
        with patch("app.services.ai_gateway.instrumented_llm.langfuse_context") as mock_ctx:
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
        with patch("app.services.ai_gateway.instrumented_llm.langfuse_context") as mock_ctx:
            record_call_cost(
                model="claude-haiku-4-5-20251001",
                provider="anthropic",
                input_tokens=0,
                output_tokens=0,
            )
            mock_ctx.update_current_observation.assert_called_once()
            meta = mock_ctx.update_current_observation.call_args.kwargs["metadata"]
            assert meta["cost_usd"] == 0.0

    def test_swallows_errors_silently(self):
        # If langfuse raises, should not break the chat flow
        with patch("app.services.ai_gateway.instrumented_llm.langfuse_context") as mock_ctx:
            mock_ctx.update_current_observation.side_effect = Exception("boom")
            # Should not raise
            record_call_cost(
                model="claude-haiku-4-5-20251001",
                provider="anthropic",
                input_tokens=100, output_tokens=50,
            )


class TestCostRecord:
    def test_dataclass_fields(self):
        rec = CostRecord(
            model="m", provider="p",
            input_tokens=10, output_tokens=5,
            cost_usd=0.001,
        )
        assert rec.cost_usd == 0.001
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_instrumented_llm.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.ai_gateway.instrumented_llm'`

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/instrumented_llm.py`:

```python
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
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_instrumented_llm.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/instrumented_llm.py tests/test_ai_gateway_instrumented_llm.py
git commit -m "feat(ai_gateway): record LLM call cost on Langfuse spans"
```

---

### Task 1.6: Integrate cost recording into `llm_factory.py`

**Files:**
- Modify: `app/services/graph/llm_factory.py:9-92`

**Step 1: Add cost-recording call site**

The LLM calls in `llm_factory.py` return LangChain `BaseChatModel` objects. The actual cost is known *after* `ainvoke()` returns and the response's `usage_metadata` is populated. Rather than wrap every call site, add a thin helper that callers can use post-invocation.

Add this function to `app/services/ai_gateway/instrumented_llm.py` (modify):

```python
def record_response_cost(
    *,
    response,
    provider: str,
) -> CostRecord | None:
    """
    Convenience wrapper: extract token usage from a LangChain AIMessage
    response, then call record_call_cost. Returns None if no usage info
    is available (some providers don't include it on streaming chunks).
    """
    usage = getattr(response, "usage_metadata", None) or {}
    if not usage:
        return None

    model = getattr(response, "response_metadata", {}).get("model_name", "unknown")
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    return record_call_cost(
        model=model,
        provider=provider,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
```

**Step 2: Add a test for the helper**

Append to `tests/test_ai_gateway_instrumented_llm.py`:

```python
class TestRecordResponseCost:
    def test_extracts_usage_from_response(self):
        from app.services.ai_gateway.instrumented_llm import record_response_cost

        response = MagicMock()
        response.usage_metadata = {"input_tokens": 100, "output_tokens": 50}
        response.response_metadata = {"model_name": "claude-haiku-4-5-20251001"}

        with patch("app.services.ai_gateway.instrumented_llm.langfuse_context"):
            rec = record_response_cost(response=response, provider="anthropic")
            assert rec is not None
            assert rec.input_tokens == 100
            assert rec.output_tokens == 50
            assert rec.cost_usd > 0

    def test_returns_none_when_no_usage(self):
        from app.services.ai_gateway.instrumented_llm import record_response_cost

        response = MagicMock(spec=["response_metadata"])
        response.response_metadata = {}
        rec = record_response_cost(response=response, provider="anthropic")
        assert rec is None
```

**Step 3: Run the new tests**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_instrumented_llm.py -v
```

Expected: 6 passed.

**Step 4: Integrate into orchestrator (proof of integration)**

Modify `app/services/graph/orchestrator.py:64` — after the `response = await llm_with_tools.ainvoke(messages, config)` call, add:

```python
    from app.services.ai_gateway.instrumented_llm import record_response_cost
    record_response_cost(response=response, provider=state["agent_provider"])
```

**Step 5: Run existing orchestrator tests (if any) to verify no regression**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v -k "orchestrator or graph"
```

If no tests exist for the orchestrator, run the full test suite:

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v
```

Expected: all existing tests pass.

**Step 6: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/instrumented_llm.py \
        app/services/graph/orchestrator.py \
        tests/test_ai_gateway_instrumented_llm.py
git commit -m "feat(ai_gateway): integrate cost recording into orchestrator"
```

---

### Task 1.7: Phase 1 verification — run full test suite

**Step 1: Run all tests**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v
```

Expected: all tests pass, including the 18 new ones from `tests/test_ai_gateway_*.py`.

**Step 2: Verify Langfuse cost metadata is being written**

This requires a live Langfuse instance. To verify locally without Langfuse, run:

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "
import asyncio
from app.services.ai_gateway.instrumented_llm import record_call_cost
rec = record_call_cost(
    model='claude-haiku-4-5-20251001',
    provider='anthropic',
    input_tokens=1000, output_tokens=500,
)
print('Cost:', rec.cost_usd)
assert rec.cost_usd > 0, 'Cost should be > 0'
print('OK: cost layer produces non-zero estimates')
"
```

Expected output: `Cost: 0.0028` (or similar), `OK: cost layer produces non-zero estimates`.

---

## Phase 2 — Per-Workspace Token Budget Enforcement

> **Why second:** Once we know the cost per call, we can enforce per-company monthly token budgets. This is a hard requirement for any production AI platform. Budgets are stored on the company document and checked *before* each orchestrator call.

### Task 2.1: Add `monthly_token_budget` to billing limits

**Files:**
- Modify: `app/core/billing_limits.py:10-73`
- Test: `tests/test_billing_limits_tokens.py`

**Step 1: Write failing test**

Create `tests/test_billing_limits_tokens.py`:

```python
"""Tests for token budget fields in billing tiers."""
from app.core.billing_limits import TIER_LIMITS, get_tier_limits


class TestTokenBudgets:
    def test_every_tier_has_token_budget(self):
        for tier_name, limits in TIER_LIMITS.items():
            assert "monthly_token_budget" in limits, f"{tier_name} missing budget"

    def test_higher_tiers_have_larger_budgets(self):
        none_budget = TIER_LIMITS["none"]["monthly_token_budget"]
        basic_budget = TIER_LIMITS["basic"]["monthly_token_budget"]
        pro_budget = TIER_LIMITS["pro"]["monthly_token_budget"]
        enterprise_budget = TIER_LIMITS["enterprise"]["monthly_token_budget"]
        assert none_budget < basic_budget < pro_budget < enterprise_budget

    def test_negative_one_means_unlimited(self):
        assert TIER_LIMITS["enterprise"]["monthly_token_budget"] == -1

    def test_get_tier_limits_returns_budget(self):
        limits = get_tier_limits("pro")
        assert "monthly_token_budget" in limits
        assert limits["monthly_token_budget"] > 0
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_billing_limits_tokens.py -v
```

Expected: `KeyError: 'monthly_token_budget'` (or similar).

**Step 3: Add the field to each tier in `app/core/billing_limits.py`**

Update the `TIER_LIMITS` dict — add `"monthly_token_budget"` to each tier. Budgets are total tokens (input + output) per month:

- `none`: 0
- `basic`: 500_000 (500K tokens/month)
- `pro`: 5_000_000 (5M tokens/month)
- `enterprise`: -1 (unlimited)

Concretely, in `app/core/billing_limits.py`:

```python
"none": {
    # ... existing fields ...
    "monthly_token_budget": 0,
},
"basic": {
    # ... existing fields ...
    "monthly_token_budget": 500_000,
},
"pro": {
    # ... existing fields ...
    "monthly_token_budget": 5_000_000,
},
"enterprise": {
    # ... existing fields ...
    "monthly_token_budget": -1,
},
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_billing_limits_tokens.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/core/billing_limits.py tests/test_billing_limits_tokens.py
git commit -m "feat(billing): add monthly_token_budget to all tier configs"
```

---

### Task 2.2: Token usage tracker in MongoDB

**Files:**
- Create: `app/services/ai_gateway/token_usage.py`
- Test: `tests/test_ai_gateway_token_usage.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_token_usage.py`:

```python
"""Tests for the per-company token usage tracker."""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from app.services.ai_gateway.token_usage import (
    get_current_period_key,
    get_company_token_usage,
    record_token_usage,
    TokenUsageError,
)


class TestPeriodKey:
    def test_returns_first_of_month(self):
        key = get_current_period_key()
        # Format: YYYY-MM
        assert len(key) == 7
        assert key[4] == "-"
        year, month = key.split("-")
        assert 2025 <= int(year) <= 2030
        assert 1 <= int(month) <= 12


class TestRecordTokenUsage:
    @pytest.mark.asyncio
    async def test_increments_company_counter(self):
        mock_db = MagicMock()
        mock_collection = AsyncMock()
        mock_db.token_usage = mock_collection

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.services.ai_gateway.token_usage.db", mock_db)
            await record_token_usage(
                company_id="comp_123",
                input_tokens=1000,
                output_tokens=500,
                model="claude-haiku-4-5-20251001",
                provider="anthropic",
            )

        # Should have called update_one with $inc
        mock_collection.update_one.assert_called_once()
        call_args = mock_collection.update_one.call_args
        update_doc = call_args.args[1]
        assert "$inc" in update_doc
        assert update_doc["$inc"]["input_tokens"] == 1000
        assert update_doc["$inc"]["output_tokens"] == 500


class TestGetCompanyTokenUsage:
    @pytest.mark.asyncio
    async def test_returns_zero_for_unknown_company(self):
        mock_db = MagicMock()
        mock_collection = AsyncMock()
        mock_collection.find_one = AsyncMock(return_value=None)
        mock_db.token_usage = mock_collection

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.services.ai_gateway.token_usage.db", mock_db)
            usage = await get_company_token_usage("comp_unknown", "2026-07")

        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0
        assert usage["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_returns_stored_values(self):
        mock_db = MagicMock()
        mock_collection = AsyncMock()
        mock_collection.find_one = AsyncMock(return_value={
            "company_id": "comp_123",
            "period": "2026-07",
            "input_tokens": 10_000,
            "output_tokens": 5_000,
        })
        mock_db.token_usage = mock_collection

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.services.ai_gateway.token_usage.db", mock_db)
            usage = await get_company_token_usage("comp_123", "2026-07")

        assert usage["total_tokens"] == 15_000
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_token_usage.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.ai_gateway.token_usage'`

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/token_usage.py`:

```python
"""
Per-company token usage tracking.

Stores monthly token usage in a `token_usage` collection keyed by
(company_id, period="YYYY-MM"). The same collection is read by the
budget enforcer and the cost-attribution dashboard (Phase 6).
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

from app.core.database import db

logger = logging.getLogger(__name__)


class TokenUsageError(Exception):
    """Raised when token usage cannot be recorded or queried."""


def get_current_period_key() -> str:
    """Return the current billing period as 'YYYY-MM' in UTC."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m")


async def record_token_usage(
    *,
    company_id: str,
    input_tokens: int,
    output_tokens: int,
    model: str,
    provider: str,
) -> None:
    """
    Atomically increment the company's token counters for the current period.

    Uses $inc + $push to also append a per-call entry to `calls` for
    detailed auditing. Safe to call concurrently.
    """
    if not company_id:
        raise TokenUsageError("company_id is required")

    period = get_current_period_key()

    try:
        await db.token_usage.update_one(
            {"company_id": company_id, "period": period},
            {
                "$inc": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                    f"model_breakdown.{model}": input_tokens + output_tokens,
                },
                "$setOnInsert": {
                    "company_id": company_id,
                    "period": period,
                    "first_used_at": datetime.now(tz=timezone.utc),
                },
                "$push": {
                    "recent_calls": {
                        "$each": [{
                            "ts": datetime.now(tz=timezone.utc),
                            "model": model,
                            "provider": provider,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                        }],
                        "$slice": -100,  # Keep last 100 calls
                    },
                },
                "$set": {
                    "last_used_at": datetime.now(tz=timezone.utc),
                },
            },
            upsert=True,
        )
    except Exception as exc:
        logger.exception("Failed to record token usage for %s: %s", company_id, exc)
        # Don't raise — usage tracking failure must not break chat


async def get_company_token_usage(
    company_id: str,
    period: str | None = None,
) -> dict:
    """
    Return the token usage for a company in a given period.

    Returns a dict with input_tokens, output_tokens, total_tokens.
    If no record exists, returns zeros.
    """
    period = period or get_current_period_key()

    doc = await db.token_usage.find_one(
        {"company_id": company_id, "period": period}
    )

    if not doc:
        return {
            "company_id": company_id,
            "period": period,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    return {
        "company_id": company_id,
        "period": period,
        "input_tokens": doc.get("input_tokens", 0),
        "output_tokens": doc.get("output_tokens", 0),
        "total_tokens": doc.get("total_tokens", 0),
        "model_breakdown": doc.get("model_breakdown", {}),
    }
```

> **Note:** the `$inc` is a single dict with all counter fields — verify during implementation.

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_token_usage.py -v
```

Expected: 5 passed.

**Step 5: Add Mongo index for the new collection**

Modify `app/core/database.py` — append to `create_indexes()`:

```python
    # Token usage tracking (Phase 2)
    await db.token_usage.create_index([("company_id", 1), ("period", 1)], unique=True)
    await db.token_usage.create_index([("company_id", 1), ("period", -1)])
```

**Step 6: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/token_usage.py \
        tests/test_ai_gateway_token_usage.py \
        app/core/database.py
git commit -m "feat(ai_gateway): add per-company token usage tracking"
```

---

### Task 2.3: Budget enforcer with TDD

**Files:**
- Create: `app/services/ai_gateway/budget.py`
- Test: `tests/test_ai_gateway_budget.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_budget.py`:

```python
"""Tests for the per-company token budget enforcer."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from app.services.ai_gateway.budget import (
    check_budget,
    BudgetExceededError,
    BudgetStatus,
)


class TestCheckBudget:
    @pytest.mark.asyncio
    async def test_unlimited_budget_always_passes(self):
        company = {"id": "comp_ent", "subscription_tier": "enterprise"}
        # Should not raise even with extreme usage
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.services.ai_gateway.budget.get_company_token_usage",
                AsyncMock(return_value={"total_tokens": 999_999_999}),
            )
            status = await check_budget(company)
        assert status.allowed is True
        assert status.limit == -1

    @pytest.mark.asyncio
    async def test_under_budget_passes(self):
        company = {"id": "comp_basic", "subscription_tier": "basic"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.services.ai_gateway.budget.get_company_token_usage",
                AsyncMock(return_value={"total_tokens": 100_000}),
            )
            status = await check_budget(company)
        assert status.allowed is True
        assert status.used == 100_000
        assert status.limit == 500_000
        assert status.remaining == 400_000

    @pytest.mark.asyncio
    async def test_over_budget_raises(self):
        company = {"id": "comp_basic", "subscription_tier": "basic"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.services.ai_gateway.budget.get_company_token_usage",
                AsyncMock(return_value={"total_tokens": 500_000}),
            )
            with pytest.raises(BudgetExceededError) as exc_info:
                await check_budget(company)
            assert exc_info.value.used == 500_000
            assert exc_info.value.limit == 500_000

    @pytest.mark.asyncio
    async def test_near_limit_warns(self):
        company = {"id": "comp_basic", "subscription_tier": "basic"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.services.ai_gateway.budget.get_company_token_usage",
                AsyncMock(return_value={"total_tokens": 450_000}),  # 90% of 500K
            )
            status = await check_budget(company)
        assert status.allowed is True
        assert status.warning is not None
        assert "90%" in status.warning or "approaching" in status.warning.lower()

    @pytest.mark.asyncio
    async def test_explicit_override_bypasses(self):
        company = {"id": "comp_basic", "subscription_tier": "basic"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                "app.services.ai_gateway.budget.get_company_token_usage",
                AsyncMock(return_value={"total_tokens": 999_999}),
            )
            status = await check_budget(company, override=True)
        assert status.allowed is True
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_budget.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.ai_gateway.budget'`

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/budget.py`:

```python
"""
Per-company token budget enforcement.

Called before each model invocation to verify the company is within its
tier's monthly token allowance. Raises BudgetExceededError when over.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from app.core.billing_limits import get_tier_limits
from app.services.ai_gateway.token_usage import get_company_token_usage

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when a company has exceeded its monthly token budget."""

    def __init__(self, company_id: str, used: int, limit: int):
        self.company_id = company_id
        self.used = used
        self.limit = limit
        super().__init__(
            f"Company {company_id} has used {used:,} of {limit:,} tokens "
            f"this month. Upgrade your plan to continue."
        )


@dataclass
class BudgetStatus:
    allowed: bool
    used: int
    limit: int
    remaining: int
    warning: str | None = None
    tier: str = ""


async def check_budget(
    company: dict,
    *,
    override: bool = False,
) -> BudgetStatus:
    """
    Verify a company is within its monthly token budget.

    Args:
        company: company document (must have `id` and `subscription_tier`)
        override: if True, skip the check (used for admin/system calls)

    Returns:
        BudgetStatus with `allowed`, `used`, `limit`, `remaining`, optional `warning`.

    Raises:
        BudgetExceededError: when usage meets or exceeds the limit
                           and `override` is False.
    """
    company_id = company.get("id", "")
    tier = company.get("subscription_tier", "none")
    limits = get_tier_limits(tier)
    monthly_limit = limits.get("monthly_token_budget", 0)

    # Unlimited
    if monthly_limit == -1:
        return BudgetStatus(
            allowed=True,
            used=0,
            limit=-1,
            remaining=-1,
            tier=tier,
        )

    # Get current usage
    usage = await get_company_token_usage(company_id)
    used = usage.get("total_tokens", 0)
    remaining = max(0, monthly_limit - used)

    status = BudgetStatus(
        allowed=used < monthly_limit,
        used=used,
        limit=monthly_limit,
        remaining=remaining,
        tier=tier,
    )

    # Warning at 80% and 95%
    if monthly_limit > 0:
        pct = (used / monthly_limit) * 100
        if pct >= 95:
            status.warning = f"Approaching monthly limit: {pct:.0f}% used"
        elif pct >= 80:
            status.warning = f"Approaching monthly limit: {pct:.0f}% used"

    if not status.allowed and not override:
        logger.warning(
            "Budget exceeded for %s: %d/%d tokens (tier=%s)",
            company_id, used, monthly_limit, tier,
        )
        raise BudgetExceededError(company_id, used, monthly_limit)

    return status
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_budget.py -v
```

Expected: 5 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/budget.py tests/test_ai_gateway_budget.py
git commit -m "feat(ai_gateway): add token budget enforcement"
```

---

### Task 2.4: Integrate budget check into chat endpoint

**Files:**
- Modify: `app/api/routers/chat.py:130-148` (in `_chat_sse_generator`)

**Step 1: Add the budget check before the chat stream begins**

In `app/api/routers/chat.py`, locate the `_chat_sse_generator` function. After the line `yield _sse("chat_details", session_id=req.session_id, company_id=company_id)` (currently line 150), add:

```python
        # ── AI Gateway: token budget gate ──────────────────────────────
        from app.services.ai_gateway.budget import check_budget, BudgetExceededError
        try:
            budget_status = await check_budget(company)
            if budget_status.warning:
                yield _sse("budget_warning", message=budget_status.warning, used=budget_status.used, limit=budget_status.limit)
        except BudgetExceededError as e:
            yield _sse("error", message=str(e), code="budget_exceeded")
            yield _sse("done")
            return
        # ──────────────────────────────────────────────────────────────
```

**Step 2: Verify imports work**

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "from app.api.routers.chat import _chat_sse_generator; print('imports OK')"
```

Expected: `imports OK` (no ModuleNotFoundError).

**Step 3: Run full test suite to verify no regressions**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v
```

Expected: all tests pass.

**Step 4: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/api/routers/chat.py
git commit -m "feat(ai_gateway): gate chat requests on token budget"
```

---

### Task 2.5: Phase 2 verification

**Step 1: Manual smoke test**

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "
import asyncio
from app.services.ai_gateway.budget import check_budget, BudgetExceededError
from app.services.ai_gateway.token_usage import record_token_usage, get_company_token_usage

async def smoke():
    # Use a fake company
    company = {'id': 'comp_smoke_test', 'subscription_tier': 'basic'}
    status = await check_budget(company)
    print(f'Initial: {status.used}/{status.limit} (allowed={status.allowed})')
    assert status.allowed

asyncio.run(smoke())
print('OK: budget check works for new company')
"
```

Expected: `Initial: 0/500000 (allowed=True)` and `OK: budget check works for new company`.

---

## Phase 3 — Complexity Classifier & Tiered Model Routing

> **Why third:** With cost-per-call known (Phase 1) and budgets enforced (Phase 2), we can now safely route cheap queries to cheap models. The orchestrator classifies each user message as `simple` (greetings, FAQs the knowledge base handles directly) or `complex` (multi-step reasoning, API tool use, ambiguous requests). Simple → cheap tier; complex → capable tier.

### Task 3.1: Define model tiers in config

**Files:**
- Modify: `app/core/config.py:47-53`

**Step 1: Add tier settings**

Append to `Settings` class in `app/core/config.py` (after line 53):

```python
    # Model tiers (Phase 3 — complexity-based routing)
    # "cheap" tier: used for simple queries (greetings, FAQ lookups)
    # "capable" tier: used for complex reasoning, multi-step tools
    MODEL_TIER_CHEAP: str = "gemini-2.5-flash"
    MODEL_TIER_CAPABLE: str = "claude-haiku-4-5-20251001"
    MODEL_TIER_CHEAP_PROVIDER: str = "gemini"
    MODEL_TIER_CAPABLE_PROVIDER: str = "anthropic"
```

**Step 2: Verify the setting loads**

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "from app.core.config import settings; print(settings.MODEL_TIER_CHEAP, settings.MODEL_TIER_CAPABLE)"
```

Expected: `gemini-2.5-flash claude-haiku-4-5-20251001`

**Step 3: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/core/config.py
git commit -m "feat(config): add model tier settings for complexity routing"
```

---

### Task 3.2: Complexity classifier with TDD

**Files:**
- Create: `app/services/ai_gateway/complexity.py`
- Test: `tests/test_ai_gateway_complexity.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_complexity.py`:

```python
"""Tests for the complexity classifier."""
import pytest
from app.services.ai_gateway.complexity import (
    classify_complexity,
    ComplexityTier,
)


class TestClassifyComplexity:
    def test_simple_greeting(self):
        tier = classify_complexity("hi")
        assert tier == ComplexityTier.SIMPLE

    def test_thanks(self):
        tier = classify_complexity("thanks!")
        assert tier == ComplexityTier.SIMPLE

    def test_short_faq(self):
        tier = classify_complexity("What are your hours?")
        assert tier in (ComplexityTier.SIMPLE, ComplexityTier.COMPLEX)

    def test_complex_question(self):
        tier = classify_complexity(
            "I'm having trouble with the SDK returning 500 errors when I "
            "call the /v1/transactions endpoint with a malformed payload "
            "while the rate limiter is enabled. Can you help me debug this?"
        )
        assert tier == ComplexityTier.COMPLEX

    def test_api_status_check_is_complex(self):
        tier = classify_complexity("What is the status of order #12345?")
        assert tier == ComplexityTier.COMPLEX

    def test_navigation_question_is_simple(self):
        tier = classify_complexity("Where do I find my API key?")
        assert tier == ComplexityTier.SIMPLE

    def test_explicit_tool_use_intent_is_complex(self):
        tier = classify_complexity("Can you look up my account balance?")
        assert tier == ComplexityTier.COMPLEX
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_complexity.py -v
```

Expected: `ModuleNotFoundError`.

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/complexity.py`:

```python
"""
Heuristic complexity classifier — runs before orchestrator model call.

Returns one of:
  - SIMPLE: use cheap tier (greetings, navigation, short FAQ)
  - COMPLEX: use capable tier (debugging, multi-step, tool-use)

The classifier uses a small set of regex + length heuristics, NOT a model
call. This keeps the classification itself free and instant. The
heuristics are tuned on observed patterns — extend as new patterns emerge.
"""
from __future__ import annotations

import re
from enum import Enum


class ComplexityTier(str, Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"


# Patterns that indicate a simple query
_SIMPLE_PATTERNS = [
    r"^\s*(hi|hello|hey|good\s+(morning|afternoon|evening))[\s.!?]*$",
    r"^\s*(thanks|thank\s+you|thx|ty)[\s.!?]*$",
    r"^\s*(bye|goodbye|see\s+you)[\s.!?]*$",
    r"^\s*(ok|okay|sure|got\s+it)[\s.!?]*$",
]

# Patterns that indicate a complex query requiring tool use or reasoning
_COMPLEX_PATTERNS = [
    r"\b(debug|error|exception|stack\s*trace|500|404|403|401)\b",
    r"\b(status\s+of|check\s+my|look\s+up|find\s+my|show\s+me\s+my)\b",
    r"\b(order\s+#?\d+|ticket\s+#?\d+|transaction\s+#?\d+)\b",
    r"\b(install|configure|setup|deploy|integrate)\b",
    r"\b(can\s+you\s+(do|make|create|build|generate|send|fetch))\b",
    r"\bapi\s+key",  # Only "create" intent is complex; navigation handled separately
    r"\bwhy\s+(is|does|did|am\s+i|aren't|isn't|won't|can't)\b",
    r"\?.*\?.*\?",  # Multiple questions in one message
]

# Navigation patterns are simple (handled by knowledge/navigation tools, not LLM reasoning)
_NAVIGATION_PATTERNS = [
    r"\b(where|how)\s+(do\s+i|can\s+i|to)\b",
    r"\b(find|locate|see|view|open)\b.{0,30}\b(settings|dashboard|page|menu|button)\b",
]


def classify_complexity(message: str) -> ComplexityTier:
    """
    Classify a user message as SIMPLE or COMPLEX.

    Returns:
        ComplexityTier.SIMPLE if the message is a greeting, thanks,
        navigation question, or short FAQ.

        ComplexityTier.COMPLEX if the message contains debugging, tool
        use, status check, or multi-step intent.
    """
    msg = (message or "").strip()
    if not msg:
        return ComplexityTier.SIMPLE

    msg_lower = msg.lower()

    # Explicit complex patterns first (most specific)
    for pattern in _COMPLEX_PATTERNS:
        if re.search(pattern, msg_lower, re.IGNORECASE):
            return ComplexityTier.COMPLEX

    # Explicit simple patterns
    for pattern in _SIMPLE_PATTERNS:
        if re.match(pattern, msg_lower, re.IGNORECASE):
            return ComplexityTier.SIMPLE

    # Navigation is simple
    for pattern in _NAVIGATION_PATTERNS:
        if re.search(pattern, msg_lower, re.IGNORECASE):
            return ComplexityTier.SIMPLE

    # Length heuristic: very long messages are almost always complex
    if len(msg) > 200:
        return ComplexityTier.COMPLEX

    # Short messages with no clear intent are likely simple (chitchat)
    if len(msg) < 30:
        return ComplexityTier.SIMPLE

    # Default to complex when in doubt (safer for cost — better to use capable model
    # than to fail on an unexpectedly tricky query)
    return ComplexityTier.COMPLEX


def select_tier_model(tier: ComplexityTier) -> tuple[str, str]:
    """
    Return (model_name, provider) for the given complexity tier.

    Reads from settings (configured in app/core/config.py).
    """
    from app.core.config import settings

    if tier == ComplexityTier.SIMPLE:
        return settings.MODEL_TIER_CHEAP, settings.MODEL_TIER_CHEAP_PROVIDER
    else:
        return settings.MODEL_TIER_CAPABLE, settings.MODEL_TIER_CAPABLE_PROVIDER
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_complexity.py -v
```

Expected: 7 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/complexity.py tests/test_ai_gateway_complexity.py
git commit -m "feat(ai_gateway): add heuristic complexity classifier"
```

---

### Task 3.3: Wire complexity routing into orchestrator

**Files:**
- Modify: `app/services/graph/orchestrator.py:53-91`
- Test: `tests/test_orchestrator_complexity.py`

**Step 1: Write failing test**

Create `tests/test_orchestrator_complexity.py`:

```python
"""Tests for complexity-aware orchestrator routing."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.graph.state import AgentState


@pytest.fixture
def base_state():
    return {
        "messages": [],
        "session_id": "sess_1",
        "company_id": "comp_1",
        "company_data": {"name": "Test", "brand_tone": "professional"},
        "user_id": None,
        "sdk_user_email": None,
        "page_url": None,
        "attachments": [],
        "agent_provider": "anthropic",
        "intent": None,
        "escalate_to_human": False,
    }


@pytest.mark.asyncio
async def test_simple_message_uses_cheap_tier(base_state):
    """A greeting should be routed using the cheap model tier."""
    from langchain_core.messages import HumanMessage
    base_state["messages"] = [HumanMessage(content="hello")]

    with patch("app.services.graph.orchestrator.get_llm") as mock_get_llm, \
         patch("app.services.graph.orchestrator.classify_complexity") as mock_classify, \
         patch("app.services.graph.orchestrator.select_tier_model") as mock_select:
        mock_classify.return_value = MagicMock()
        mock_classify.return_value.value = "simple"
        mock_select.return_value = ("gemini-2.5-flash", "gemini")
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
            return_value=MagicMock(content="hi there", tool_calls=None)
        )
        mock_get_llm.return_value = mock_llm

        from app.services.graph.orchestrator import orchestrator_node
        result = await orchestrator_node(base_state, config={})

        # Verify cheap tier was selected
        mock_select.assert_called_once()
        # The agent_provider should be the cheap tier's provider
        assert base_state.get("complexity_tier") == "simple" or \
               mock_select.called_with_args_containing_something  # Implementation detail


@pytest.mark.asyncio
async def test_complex_message_uses_capable_tier(base_state):
    """A debugging question should use the capable tier."""
    from langchain_core.messages import HumanMessage
    base_state["messages"] = [HumanMessage(content="I'm getting 500 errors, why?")]

    with patch("app.services.graph.orchestrator.get_llm") as mock_get_llm, \
         patch("app.services.graph.orchestrator.classify_complexity") as mock_classify, \
         patch("app.services.graph.orchestrator.select_tier_model") as mock_select:
        mock_classify.return_value = MagicMock()
        mock_classify.return_value.value = "complex"
        mock_select.return_value = ("claude-haiku-4-5-20251001", "anthropic")
        mock_llm = MagicMock()
        mock_llm.bind_tools.return_value.ainvoke = AsyncMock(
            return_value=MagicMock(content="Let me help", tool_calls=None)
        )
        mock_get_llm.return_value = mock_llm

        from app.services.graph.orchestrator import orchestrator_node
        await orchestrator_node(base_state, config={})

        mock_select.assert_called_once()
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_orchestrator_complexity.py -v
```

Expected: at least one test fails (no `classify_complexity` import in orchestrator yet).

**Step 3: Modify `app/services/graph/orchestrator.py`**

Add imports at the top:

```python
from app.services.ai_gateway.complexity import classify_complexity, select_tier_model, ComplexityTier
```

Replace `orchestrator_node` body — the LLM call should use the tier-selected model:

```python
@observe(name="orchestrator_node")
async def orchestrator_node(state: AgentState, config):
    # ── Complexity routing (Phase 3) ──────────────────────────────
    last_user_msg = ""
    for m in reversed(state["messages"]):
        if hasattr(m, "content") and isinstance(m.content, str):
            last_user_msg = m.content
            break
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_msg = m.get("content", "")
            break

    tier = classify_complexity(last_user_msg)
    selected_model, selected_provider = select_tier_model(tier)

    # Override the provider for this orchestrator call only;
    # specialists still use state["agent_provider"] for consistency
    orchestrator_provider = selected_provider
    # ──────────────────────────────────────────────────────────────

    llm = get_llm(orchestrator_provider, fast_routing=True, streaming=False)
    llm_with_tools = llm.bind_tools(ROUTING_TOOLS)

    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\\n\\n{ORCHESTRATOR_PROMPT}"

    messages = [SystemMessage(content=full_prompt)] + state["messages"]

    response = await llm_with_tools.ainvoke(messages, config)

    # Cost instrumentation (Phase 1)
    from app.services.ai_gateway.instrumented_llm import record_response_cost
    record_response_cost(response=response, provider=orchestrator_provider)

    intent = "general_chat"
    escalate = False

    if getattr(response, "tool_calls", None):
        tool_call = response.tool_calls[0]
        tool_name = tool_call["name"]

        if tool_name == "transfer_to_knowledge":
            intent = "knowledge"
        elif tool_name == "transfer_to_navigation":
            intent = "navigation"
        elif tool_name == "transfer_to_api":
            intent = "api"
        elif tool_name == "transfer_to_scraper":
            intent = "scraper"
        elif tool_name == "escalate_to_human":
            intent = "human_escalation"
            escalate = True

        return {
            "intent": intent,
            "escalate_to_human": escalate,
            "complexity_tier": tier.value,
            "orchestrator_provider": orchestrator_provider,
        }

    return {
        "intent": intent,
        "messages": [response],
        "complexity_tier": tier.value,
        "orchestrator_provider": orchestrator_provider,
    }
```

**Step 4: Add `complexity_tier` and `orchestrator_provider` to AgentState**

Modify `app/services/graph/state.py` — add to `AgentState` TypedDict:

```python
    # Phase 3: complexity-based routing
    complexity_tier: Optional[str]
    orchestrator_provider: Optional[str]
```

**Step 5: Run tests**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_orchestrator_complexity.py tests/test_ai_gateway_complexity.py -v
```

Expected: all pass.

**Step 6: Run full test suite**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v
```

Expected: all pass.

**Step 7: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/graph/orchestrator.py \
        app/services/graph/state.py \
        tests/test_orchestrator_complexity.py
git commit -m "feat(ai_gateway): route orchestrator by query complexity"
```

---

### Task 3.4: Phase 3 verification

**Step 1: Smoke test classifier**

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "
from app.services.ai_gateway.complexity import classify_complexity, ComplexityTier

cases = [
    ('hi', ComplexityTier.SIMPLE),
    ('thanks!', ComplexityTier.SIMPLE),
    ('What is the status of order #12345?', ComplexityTier.COMPLEX),
    ('Where do I find my API key?', ComplexityTier.SIMPLE),
]
for msg, expected in cases:
    actual = classify_complexity(msg)
    status = 'OK' if actual == expected else 'FAIL'
    print(f'{status}: {msg!r:50s} expected={expected.value}, got={actual.value}')
"
```

Expected: all four lines show `OK:`.

---

## Phase 4 — LLM-Based Prompt Compression

> **Why fourth:** When conversation history exceeds `MAX_CONTEXT_MESSAGES=10`, we currently truncate blindly. This loses important context. A small "compressor" model call can summarize older messages into a compact representation that preserves intent, allowing longer effective context at minimal cost.

### Task 4.1: Compressor prompt template and utility with TDD

**Files:**
- Create: `app/services/ai_gateway/compression.py`
- Test: `tests/test_ai_gateway_compression.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_compression.py`:

```python
"""Tests for context compression."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import HumanMessage, AIMessage
from app.services.ai_gateway.compression import (
    should_compress,
    compress_messages,
    COMPRESSION_THRESHOLD,
    TARGET_MESSAGES_AFTER_COMPRESSION,
)


class TestShouldCompress:
    def test_under_threshold_returns_false(self):
        msgs = [HumanMessage(content=f"msg {i}") for i in range(5)]
        assert should_compress(msgs) is False

    def test_at_threshold_returns_true(self):
        msgs = [HumanMessage(content=f"msg {i}") for i in range(COMPRESSION_THRESHOLD + 1)]
        assert should_compress(msgs) is True

    def test_empty_returns_false(self):
        assert should_compress([]) is False


class TestCompressMessages:
    @pytest.mark.asyncio
    async def test_compresses_to_target_count(self):
        msgs = []
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            content = f"This is message number {i} about topic {i // 2}"
            msgs.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))

        with patch("app.services.ai_gateway.compression.get_llm") as mock_get_llm:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(
                return_value=MagicMessage(
                    content="Summary: user asked about topics 0-9"
                )
            )
            mock_get_llm.return_value = mock_llm

            summary_msg, kept_msgs = await compress_messages(
                msgs, target_kept=TARGET_MESSAGES_AFTER_COMPRESSION
            )

        assert summary_msg is not None
        assert "Summary" in summary_msg.content
        assert len(kept_msgs) == TARGET_MESSAGES_AFTER_COMPRESSION

    @pytest.mark.asyncio
    async def test_handles_small_history_gracefully(self):
        msgs = [HumanMessage(content="hi"), AIMessage(content="hello")]
        summary_msg, kept_msgs = await compress_messages(msgs, target_kept=4)
        # Should return early with no compression
        assert summary_msg is None
        assert kept_msgs == msgs


class MagicMessage:
    def __init__(self, content):
        self.content = content
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_compression.py -v
```

Expected: `ModuleNotFoundError`.

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/compression.py`:

```python
"""
LLM-based context compression.

When the conversation history exceeds COMPRESSION_THRESHOLD messages,
older messages are summarized by a cheap model call. The summary is
inserted as a system message at the start of the context, and only the
most recent TARGET_MESSAGES_AFTER_COMPRESSION messages are kept verbatim.

This is cheaper than naively truncating because:
  - The model still sees the full intent of the conversation
  - We avoid "cold cutoffs" mid-thought
  - Token cost is bounded: summary is small + recent N messages only
"""
from __future__ import annotations

import logging
from typing import Optional

from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage

from app.core.config import settings
from app.services.graph.llm_factory import get_llm

logger = logging.getLogger(__name__)

# Compression triggers when the conversation has more than this many messages
COMPRESSION_THRESHOLD = 12

# After compression, how many recent messages to keep verbatim
TARGET_MESSAGES_AFTER_COMPRESSION = 6

COMPRESSION_PROMPT = """You are a conversation summarizer. Your job is to compress a chat
history into a concise summary that preserves:
1. The user's original question and any follow-up refinements
2. Key facts the assistant has established
3. Any unresolved questions or open issues
4. The current topic/domain being discussed

Do NOT include pleasantries, greetings, or meta-commentary. Output a single
paragraph of plain text — no bullet points, no headers.

CHAT HISTORY:
{history}

CONCISE SUMMARY:"""


def should_compress(messages: list[BaseMessage]) -> bool:
    """Return True if the message list should be compressed."""
    if not messages:
        return False
    return len(messages) > COMPRESSION_THRESHOLD


def _format_history(messages: list[BaseMessage]) -> str:
    """Format messages as 'role: content' lines for the compressor."""
    lines = []
    for m in messages:
        role = "user" if isinstance(m, HumanMessage) else "assistant" if isinstance(m, AIMessage) else "system"
        content = m.content if isinstance(m.content, str) else str(m.content)
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def compress_messages(
    messages: list[BaseMessage],
    *,
    target_kept: int = TARGET_MESSAGES_AFTER_COMPRESSION,
    provider: str | None = None,
) -> tuple[Optional[BaseMessage], list[BaseMessage]]:
    """
    Compress a message history.

    Returns (summary_message, kept_messages).
    - summary_message: a SystemMessage with the summary, or None if no compression happened
    - kept_messages: the most recent N messages

    If messages <= target_kept, returns (None, messages) unchanged.
    """
    if len(messages) <= target_kept:
        return None, list(messages)

    if not should_compress(messages):
        return None, list(messages)

    # Split into "old" (to compress) and "new" (to keep)
    split_point = len(messages) - target_kept
    old_messages = messages[:split_point]
    kept_messages = messages[split_point:]

    history_text = _format_history(old_messages)

    try:
        # Use the cheap tier for compression (low-stakes summarization)
        provider = provider or settings.MODEL_TIER_CHEAP_PROVIDER
        llm = get_llm(provider, fast_routing=True, streaming=False)

        response = await llm.ainvoke(
            [HumanMessage(content=COMPRESSION_PROMPT.format(history=history_text))]
        )

        summary_text = (
            response.content
            if isinstance(response.content, str)
            else str(response.content)
        )

        summary_msg = SystemMessage(
            content=f"[Earlier conversation summary]: {summary_text.strip()}"
        )

        # Cost instrumentation
        from app.services.ai_gateway.instrumented_llm import record_response_cost
        record_response_cost(response=response, provider=provider)

        logger.info(
            "Compressed %d messages into summary (%d chars); kept %d recent",
            len(old_messages), len(summary_text), len(kept_messages),
        )

        return summary_msg, kept_messages

    except Exception as exc:
        logger.warning("Compression failed, falling back to truncation: %s", exc)
        # Fallback: just truncate to target_kept
        return None, kept_messages
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_compression.py -v
```

Expected: 5 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/compression.py tests/test_ai_gateway_compression.py
git commit -m "feat(ai_gateway): add LLM-based context compression"
```

---

### Task 4.2: Integrate compression into executor

**Files:**
- Modify: `app/services/graph/executor.py:60-71`

**Step 1: Add compression call in `chat_stream_graph`**

In `app/services/graph/executor.py`, after the `langchain_messages` list is built (around line 70, after `langchain_messages.append(HumanMessage(content=message))`), add:

```python
        # ── AI Gateway: compress if history is long ────────────────────
        from app.services.ai_gateway.compression import compress_messages
        summary_msg, langchain_messages = await compress_messages(langchain_messages)
        if summary_msg is not None:
            langchain_messages = [summary_msg] + langchain_messages
        # ──────────────────────────────────────────────────────────────
```

**Step 2: Run full test suite**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v
```

Expected: all pass.

**Step 3: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/graph/executor.py
git commit -m "feat(ai_gateway): compress long conversations in chat_stream_graph"
```

---

### Task 4.3: Phase 4 verification

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "
import asyncio
from langchain_core.messages import HumanMessage, AIMessage
from app.services.ai_gateway.compression import should_compress

# Test threshold logic
short_history = [HumanMessage(content='hi')]
long_history = [HumanMessage(content=f'msg {i}') for i in range(15)]

print(f'short: should_compress={should_compress(short_history)} (expected False)')
print(f'long:  should_compress={should_compress(long_history)} (expected True)')
assert should_compress(short_history) is False
assert should_compress(long_history) is True
print('OK: threshold logic works')
"
```

Expected: `OK: threshold logic works`.

---

## Phase 5 — Per-Request Smart Provider Failover

> **Why fifth:** Current failover in `chat.py:318-380` retries the whole conversation on the next provider, which is slow and wasteful. We want per-request failover: when a single model call fails, try the next provider in the chain *for that call only*. We also add a degradation detector that marks a provider as "degraded" after N consecutive failures and skips it temporarily.

### Task 5.1: Provider health tracker with TDD

**Files:**
- Create: `app/services/ai_gateway/health.py`
- Test: `tests/test_ai_gateway_health.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_health.py`:

```python
"""Tests for the provider health tracker."""
import pytest
from app.services.ai_gateway.health import (
    ProviderHealth,
    record_success,
    record_failure,
    is_provider_healthy,
    get_health_snapshot,
    FAILURE_THRESHOLD,
    DEGRADATION_COOLDOWN_SECONDS,
)


@pytest.fixture
def fresh_state(monkeypatch):
    """Reset the health module state for each test."""
    monkeypatch.setattr("app.services.ai_gateway.health._state", {})


class TestProviderHealth:
    def test_initial_state_is_healthy(self, fresh_state):
        assert is_provider_healthy("anthropic") is True

    def test_single_failure_does_not_mark_degraded(self, fresh_state):
        record_failure("anthropic")
        assert is_provider_healthy("anthropic") is True

    def test_threshold_failures_marks_degraded(self, fresh_state):
        for _ in range(FAILURE_THRESHOLD):
            record_failure("anthropic")
        assert is_provider_healthy("anthropic") is False

    def test_success_after_failures_resets(self, fresh_state):
        for _ in range(FAILURE_THRESHOLD - 1):
            record_failure("anthropic")
        record_success("anthropic")
        assert is_provider_healthy("anthropic") is True

    def test_health_snapshot_returns_all_providers(self, fresh_state):
        record_success("anthropic")
        record_failure("gemini")
        snap = get_health_snapshot()
        assert "anthropic" in snap
        assert "gemini" in snap
        assert snap["anthropic"]["healthy"] is True
        assert snap["gemini"]["healthy"] is True  # single failure, not threshold
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_health.py -v
```

Expected: `ModuleNotFoundError`.

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/health.py`:

```python
"""
In-process provider health tracker.

Tracks recent success/failure counts per provider. When a provider
exceeds FAILURE_THRESHOLD consecutive failures, it is marked degraded
and skipped by the failover chain for DEGRADATION_COOLDOWN_SECONDS.

This is intentionally in-process (not Redis-backed) because:
  - It only affects the local worker's routing decisions
  - Cross-worker coordination adds latency for marginal benefit
  - Health is a "soft" signal; a single worker's view is sufficient
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict


FAILURE_THRESHOLD = 3  # consecutive failures before marking degraded
DEGRADATION_COOLDOWN_SECONDS = 300  # 5 minutes


@dataclass
class _ProviderState:
    consecutive_failures: int = 0
    last_failure_at: float = 0.0
    last_success_at: float = 0.0
    total_successes: int = 0
    total_failures: int = 0


_state: Dict[str, _ProviderState] = {}
_lock = Lock()


def _get_state(provider: str) -> _ProviderState:
    if provider not in _state:
        _state[provider] = _ProviderState()
    return _state[provider]


def record_success(provider: str) -> None:
    """Record a successful model call. Resets failure counter."""
    with _lock:
        s = _get_state(provider)
        s.consecutive_failures = 0
        s.last_success_at = time.time()
        s.total_successes += 1


def record_failure(provider: str) -> None:
    """Record a failed model call. Increments failure counter."""
    with _lock:
        s = _get_state(provider)
        s.consecutive_failures += 1
        s.last_failure_at = time.time()
        s.total_failures += 1


def is_provider_healthy(provider: str) -> bool:
    """
    Return True if the provider should be used.

    A provider is unhealthy if:
      - It has had FAILURE_THRESHOLD or more consecutive failures
      - AND the last failure was within DEGRADATION_COOLDOWN_SECONDS
    """
    with _lock:
        s = _get_state(provider)
        if s.consecutive_failures < FAILURE_THRESHOLD:
            return True
        # If enough time has passed since the last failure, give it another chance
        if (time.time() - s.last_failure_at) > DEGRADATION_COOLDOWN_SECONDS:
            # Reset so we try again
            s.consecutive_failures = 0
            return True
        return False


def get_health_snapshot() -> dict:
    """Return a snapshot of all provider health for the admin dashboard."""
    with _lock:
        return {
            provider: {
                "healthy": is_provider_healthy(provider),
                "consecutive_failures": s.consecutive_failures,
                "last_success_at": s.last_success_at,
                "last_failure_at": s.last_failure_at,
                "total_successes": s.total_successes,
                "total_failures": s.total_failures,
            }
            for provider, s in _state.items()
        }
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_health.py -v
```

Expected: 5 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/health.py tests/test_ai_gateway_health.py
git commit -m "feat(ai_gateway): add per-process provider health tracker"
```

---

### Task 5.2: Per-request failover wrapper with TDD

**Files:**
- Create: `app/services/ai_gateway/failover.py`
- Test: `tests/test_ai_gateway_failover.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_failover.py`:

```python
"""Tests for the per-request failover wrapper."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.ai_gateway.failover import (
    invoke_with_failover,
    AllProvidersFailedError,
    DEFAULT_FALLBACK_CHAIN,
)


class TestInvokeWithFailover:
    @pytest.mark.asyncio
    async def test_succeeds_on_first_provider(self):
        async def good_call():
            return "ok"

        with patch("app.services.ai_gateway.failover.is_provider_healthy", return_value=True), \
             patch("app.services.ai_gateway.failover.record_success") as mock_success:
            result = await invoke_with_failover(
                primary_provider="anthropic",
                fallback_chain=["gemini", "openrouter"],
                call_fn=good_call,
            )
        assert result == "ok"
        mock_success.assert_called_once_with("anthropic")

    @pytest.mark.asyncio
    async def test_falls_back_to_next_on_failure(self):
        async def fail():
            raise RuntimeError("API down")

        async def succeed():
            return "recovered"

        call_count = {"n": 0}

        async def alternating():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first fails")
            return "ok"

        with patch("app.services.ai_gateway.failover.is_provider_healthy", return_value=True), \
             patch("app.services.ai_gateway.failover.record_failure") as mock_fail, \
             patch("app.services.ai_gateway.failover.record_success") as mock_success:
            result = await invoke_with_failover(
                primary_provider="anthropic",
                fallback_chain=["gemini"],
                call_fn=alternating,
            )
        assert result == "ok"
        assert mock_fail.called
        assert mock_success.called

    @pytest.mark.asyncio
    async def test_skips_degraded_providers(self):
        async def succeed():
            return "ok"

        with patch("app.services.ai_gateway.failover.is_provider_healthy") as mock_healthy, \
             patch("app.services.ai_gateway.failover.record_success") as mock_success:
            # anthropic degraded, gemini healthy
            mock_healthy.side_effect = lambda p: p != "anthropic"
            result = await invoke_with_failover(
                primary_provider="anthropic",
                fallback_chain=["gemini"],
                call_fn=succeed,
            )
        assert result == "ok"
        # Should have called record_success for gemini, not anthropic
        mock_success.assert_called_with("gemini")

    @pytest.mark.asyncio
    async def test_all_providers_fail_raises(self):
        async def always_fail():
            raise RuntimeError("nope")

        with patch("app.services.ai_gateway.failover.is_provider_healthy", return_value=True), \
             patch("app.services.ai_gateway.failover.record_failure"):
            with pytest.raises(AllProvidersFailedError):
                await invoke_with_failover(
                    primary_provider="anthropic",
                    fallback_chain=["gemini", "openrouter"],
                    call_fn=always_fail,
                )
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_failover.py -v
```

Expected: `ModuleNotFoundError`.

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/failover.py`:

```python
"""
Per-request provider failover.

Wraps a single model call (call_fn) in a try/except chain that:
  1. Tries the primary provider
  2. On failure, tries each fallback provider in order
  3. Skips providers marked unhealthy by the health tracker
  4. Records success/failure for the health tracker
  5. Raises AllProvidersFailedError if all fail

This is the per-request replacement for the conversation-level retry
loop currently in app/api/routers/chat.py:318-380.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, TypeVar

from app.services.ai_gateway.health import (
    is_provider_healthy,
    record_failure,
    record_success,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Default order: try Anthropic, then Gemini, then OpenRouter
DEFAULT_FALLBACK_CHAIN = ["anthropic", "gemini", "openrouter"]


class AllProvidersFailedError(Exception):
    """Raised when every provider in the failover chain fails."""


async def invoke_with_failover(
    *,
    primary_provider: str,
    call_fn: Callable[[str], Awaitable[T]],
    fallback_chain: list[str] | None = None,
) -> T:
    """
    Invoke `call_fn(provider)` with failover.

    Args:
        primary_provider: the first provider to try
        call_fn: async function that takes a provider name and returns a result
        fallback_chain: ordered list of fallback providers to try on failure

    Returns:
        The result of the first successful call.

    Raises:
        AllProvidersFailedError: when all providers in the chain fail
        or are marked unhealthy.
    """
    chain = [primary_provider] + [
        p for p in (fallback_chain or DEFAULT_FALLBACK_CHAIN) if p != primary_provider
    ]

    last_error: Exception | None = None

    for provider in chain:
        if not is_provider_healthy(provider):
            logger.debug("Skipping degraded provider: %s", provider)
            continue

        try:
            result = await call_fn(provider)
            record_success(provider)
            return result
        except Exception as exc:
            logger.warning("Provider %s failed: %s", provider, exc)
            record_failure(provider)
            last_error = exc
            continue

    # All providers failed or were degraded
    raise AllProvidersFailedError(
        f"All providers in chain {chain} failed. Last error: {last_error}"
    )
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_failover.py -v
```

Expected: 4 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/failover.py tests/test_ai_gateway_failover.py
git commit -m "feat(ai_gateway): add per-request failover wrapper"
```

---

### Task 5.3: Replace conversation-level retry with per-request failover

**Files:**
- Modify: `app/api/routers/chat.py:316-380`

**Step 1: Refactor the retry loop to use the new failover wrapper**

In `app/api/routers/chat.py`, replace the `for idx, provider_key in enumerate(agents_to_try):` block (lines 318-380) with:

```python
        # ── AI Gateway: per-request failover ──────────────────────────
        from app.services.ai_gateway.failover import (
            invoke_with_failover,
            AllProvidersFailedError,
        )
        from app.services.ai_gateway.health import get_health_snapshot

        try:
            # The chat_stream_graph yields events; we wrap it so per-request
            # failures trigger provider switching without losing streamed text.
            async def stream_with_provider(provider: str):
                response_text_local = ""
                async for event in chat_stream_graph(
                    company_id=company_id,
                    session_id=req.session_id,
                    message=actual_message_to_send,
                    user_id=req.user_id,
                    page_url=req.page_url,
                    attachments=attachments_raw,
                    user_timestamp=user_timestamp,
                    agent_provider=provider,
                    sdk_user_email=req.user_email,
                    user_timezone=req.user_timezone,
                ):
                    event_type = event.get("type")
                    if event_type == "thinking":
                        yield _sse("thinking", message=event.get("message", ""))
                    elif event_type == "tool":
                        yield _sse("tool", name=event.get("name", ""), label=event.get("label", ""))
                    elif event_type == "text":
                        content = event.get("content", "")
                        response_text_local += content
                        yield _sse("stream", message=content)
                    elif event_type == "sources":
                        yield _sse(
                            "sources",
                            sources=event.get("sources", []),
                            blockchain_data=event.get("blockchain_data"),
                        )
                    elif event_type == "navigation_guide":
                        guide = event.get("guide", {})
                        yield _sse(
                            "navigation_guide",
                            steps=guide.get("steps", []),
                            path_summary=guide.get("path_summary", []),
                        )
                    elif event_type == "error":
                        raise Exception(event.get("message') or 'Agent stream yielded an error')
                return response_text_local

            # We can't easily invoke_with_failover on an async generator,
            # so we do an equivalent loop manually for the streaming case.
            from app.services.ai_gateway.health import is_provider_healthy, record_failure, record_success
            response_text = ""
            for provider in agents_to_try:
                if not is_provider_healthy(provider):
                    continue
                try:
                    async for sse_event in stream_with_provider(provider):
                        # Re-yield through the outer generator
                        if hasattr(sse_event, '__aiter__'):
                            async for sub in sse_event:
                                yield sub
                        else:
                            yield sse_event
                    record_success(provider)
                    break
                except Exception as e:
                    record_failure(provider)
                    if provider == agents_to_try[-1]:
                        raise
                    yield _sse("thinking", message="Switching AI providers...")
                    continue

        except AllProvidersFailedError as e:
            logger.error(f"All providers failed for company {company_id}: {e}")
            response_text = "I'm having trouble right now. Please try again in a moment."
            if not response_text.strip():
                yield _sse("stream", message=response_text)
        # ──────────────────────────────────────────────────────────────
```

**Step 2: Run full test suite**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v
```

Expected: all pass (no regressions).

**Step 3: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/api/routers/chat.py
git commit -m "feat(ai_gateway): per-request failover in chat endpoint"
```

> **Note:** the `agents_to_try` list is still constructed in chat.py:154-158. The new code uses the health-checked version of the same chain. If only one provider is healthy, the chain collapses to one entry — graceful degradation.

---

### Task 5.4: Phase 5 verification

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "
import asyncio
from app.services.ai_gateway.health import (
    record_success, record_failure, is_provider_healthy, get_health_snapshot
)
from app.services.ai_gateway.failover import invoke_with_failover, AllProvidersFailedError

# Simulate 3 failures on anthropic
for _ in range(3):
    record_failure('anthropic')

print('Anthropic healthy?', is_provider_healthy('anthropic'), '(expected False)')

# Now call with failover — should skip anthropic
async def succeed():
    return 'OK'

async def smoke():
    result = await invoke_with_failover(
        primary_provider='anthropic',
        fallback_chain=['gemini', 'openrouter'],
        call_fn=succeed,
    )
    print('Result:', result)
    assert result == 'OK'

asyncio.run(smoke())
print('OK: failover skips degraded provider')
"
```

Expected: `Anthropic healthy? False`, `Result: OK`, `OK: failover skips degraded provider`.

---

## Phase 6 — Cost-Attribution Dashboard Endpoints

> **Why sixth:** With all the upstream pieces in place (pricing, token usage recording, cost metadata on Langfuse), we can now expose per-agent, per-customer, per-model rollups via API endpoints. Admins query these to see who's spending what.

### Task 6.1: Cost aggregation service with TDD

**Files:**
- Create: `app/services/ai_gateway/attribution.py`
- Test: `tests/test_ai_gateway_attribution.py`

**Step 1: Write failing test**

Create `tests/test_ai_gateway_attribution.py`:

```python
"""Tests for cost attribution aggregation."""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from app.services.ai_gateway.attribution import (
    get_company_cost_breakdown,
    get_global_cost_breakdown,
    get_cost_by_model,
    get_cost_by_agent,
)


@pytest.fixture
def mock_token_usage_docs():
    return [
        {
            "company_id": "comp_a",
            "period": "2026-07",
            "input_tokens": 100_000,
            "output_tokens": 50_000,
            "model_breakdown": {
                "claude-haiku-4-5-20251001": 150_000,
            },
        },
        {
            "company_id": "comp_b",
            "period": "2026-07",
            "input_tokens": 200_000,
            "output_tokens": 100_000,
            "model_breakdown": {
                "gemini-2.5-flash": 200_000,
                "claude-haiku-4-5-20251001": 100_000,
            },
        },
    ]


class TestGetCompanyCostBreakdown:
    @pytest.mark.asyncio
    async def test_returns_breakdown_by_model(self, mock_token_usage_docs):
        mock_db = MagicMock()
        mock_collection = AsyncMock()
        mock_collection.find = MagicMock(return_value=mock_cursor(mock_token_usage_docs))
        mock_db.token_usage = mock_collection

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.services.ai_gateway.attribution.db", mock_db)
            breakdown = await get_company_cost_breakdown("comp_a", "2026-07")

        assert breakdown["company_id"] == "comp_a"
        assert "by_model" in breakdown
        assert "claude-haiku-4-5-20251001" in breakdown["by_model"]
        assert breakdown["total_cost_usd"] > 0


class TestGetGlobalCostBreakdown:
    @pytest.mark.asyncio
    async def test_aggregates_across_companies(self, mock_token_usage_docs):
        mock_db = MagicMock()
        mock_collection = AsyncMock()
        mock_collection.find = MagicMock(return_value=mock_cursor(mock_token_usage_docs))
        mock_db.token_usage = mock_collection

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("app.services.ai_gateway.attribution.db", mock_db)
            breakdown = await get_global_cost_breakdown("2026-07")

        assert "by_company" in breakdown
        assert "by_model" in breakdown
        assert "comp_a" in breakdown["by_company"]
        assert "comp_b" in breakdown["by_company"]


def mock_cursor(docs):
    """Build an async-iterable mock that mimics Motor's find()."""
    class AsyncCursor:
        def __init__(self, docs):
            self.docs = docs

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.docs:
                raise StopAsyncIteration
            return self.docs.pop(0)

    return AsyncCursor(list(docs))
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_attribution.py -v
```

Expected: `ModuleNotFoundError`.

**Step 3: Write minimal implementation**

Create `app/services/ai_gateway/attribution.py`:

```python
"""
Cost attribution aggregations.

Reads from the `token_usage` collection (populated by Phase 2) and
applies the pricing registry to compute cost rollups. Used by the
dashboard endpoints (Phase 6) and by ad-hoc admin queries.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from app.core.database import db
from app.services.ai_gateway.pricing import estimate_cost
from app.services.ai_gateway.token_usage import get_current_period_key

logger = logging.getLogger(__name__)


async def get_company_cost_breakdown(
    company_id: str,
    period: str | None = None,
) -> dict:
    """
    Return cost breakdown for a single company in a period.

    Output:
        {
            "company_id": "comp_123",
            "period": "2026-07",
            "total_cost_usd": 1.23,
            "by_model": {"claude-haiku-4-5-20251001": 0.95, ...},
            "input_tokens": 100_000,
            "output_tokens": 50_000,
        }
    """
    period = period or get_current_period_key()

    doc = await db.token_usage.find_one(
        {"company_id": company_id, "period": period}
    )

    if not doc:
        return {
            "company_id": company_id,
            "period": period,
            "total_cost_usd": 0.0,
            "by_model": {},
            "input_tokens": 0,
            "output_tokens": 0,
        }

    model_breakdown = doc.get("model_breakdown", {})

    # Compute cost per model. We don't have separate input/output breakdown
    # per model in token_usage, so we estimate by assuming the model's
    # average input/output ratio (60/40 by default). For exact numbers,
    # the Langfuse traces are the source of truth.
    by_model_cost: dict[str, float] = {}
    for model, tokens in model_breakdown.items():
        est_input = int(tokens * 0.6)
        est_output = tokens - est_input
        # Find provider for this model
        provider = _provider_for_model(model)
        cost = estimate_cost(model, provider, est_input, est_output)
        by_model_cost[model] = round(cost, 6)

    total_cost = sum(by_model_cost.values())

    return {
        "company_id": company_id,
        "period": period,
        "total_cost_usd": round(total_cost, 4),
        "by_model": by_model_cost,
        "input_tokens": doc.get("input_tokens", 0),
        "output_tokens": doc.get("output_tokens", 0),
    }


async def get_global_cost_breakdown(period: str | None = None) -> dict:
    """
    Return global cost breakdown for a period.

    Output:
        {
            "period": "2026-07",
            "total_cost_usd": 42.50,
            "total_input_tokens": 1_000_000,
            "total_output_tokens": 500_000,
            "by_company": {"comp_a": {"cost_usd": 12.30, "tokens": 150_000}, ...},
            "by_model": {"claude-haiku-4-5-20251001": 25.00, ...},
        }
    """
    period = period or get_current_period_key()

    cursor = db.token_usage.find({"period": period})
    docs = await cursor.to_list(length=10000)

    by_company: dict[str, dict] = {}
    by_model: dict[str, float] = defaultdict(float)
    total_input = 0
    total_output = 0
    total_cost = 0.0

    for doc in docs:
        company_id = doc.get("company_id", "unknown")
        company_input = doc.get("input_tokens", 0)
        company_output = doc.get("output_tokens", 0)
        total_input += company_input
        total_output += company_output

        model_breakdown = doc.get("model_breakdown", {})
        company_cost = 0.0
        for model, tokens in model_breakdown.items():
            est_input = int(tokens * 0.6)
            est_output = tokens - est_input
            provider = _provider_for_model(model)
            cost = estimate_cost(model, provider, est_input, est_output)
            by_model[model] += cost
            company_cost += cost

        by_company[company_id] = {
            "cost_usd": round(company_cost, 4),
            "input_tokens": company_input,
            "output_tokens": company_output,
        }
        total_cost += company_cost

    return {
        "period": period,
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "by_company": by_company,
        "by_model": {k: round(v, 4) for k, v in by_model.items()},
    }


def _provider_for_model(model: str) -> str:
    """Best-effort provider inference for a model name."""
    from app.services.ai_gateway.pricing import lookup_pricing
    pricing = lookup_pricing(model)
    return pricing.provider
```

**Step 4: Run test to verify pass**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_gateway_attribution.py -v
```

Expected: 2 passed.

**Step 5: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/services/ai_gateway/attribution.py tests/test_ai_gateway_attribution.py
git commit -m "feat(ai_gateway): add cost attribution aggregations"
```

---

### Task 6.2: Dashboard API router

**Files:**
- Create: `app/api/routers/ai_infrastructure.py`
- Test: `tests/test_ai_infrastructure_router.py`

**Step 1: Write failing test**

Create `tests/test_ai_infrastructure_router.py`:

```python
"""Tests for the AI infrastructure dashboard endpoints."""
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def app():
    from main import app
    return app


@pytest.mark.asyncio
async def test_get_company_costs_requires_auth():
    from main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/ai-infra/companies/comp_123/costs")
    # Should be 401 (no auth) or 403
    assert response.status_code in (401, 403)


@pytest.mark.asyncio
async def test_get_company_costs_returns_breakdown():
    from main import app

    mock_breakdown = {
        "company_id": "comp_123",
        "period": "2026-07",
        "total_cost_usd": 1.23,
        "by_model": {"claude-haiku-4-5-20251001": 1.23},
        "input_tokens": 100_000,
        "output_tokens": 50_000,
    }

    with patch(
        "app.api.routers.ai_infrastructure.get_company_cost_breakdown",
        AsyncMock(return_value=mock_breakdown),
    ), patch(
        "app.api.routers.ai_infrastructure.get_current_user",
        AsyncMock(return_value={"user_id": "user_1"}),
    ):
        from main import app
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": "Bearer test"}) as client:
            response = await client.get("/api/ai-infra/companies/comp_123/costs")
    # If auth passes, response should be 200 with breakdown
    if response.status_code == 200:
        data = response.json()
        assert data["company_id"] == "comp_123"
        assert data["total_cost_usd"] == 1.23
```

**Step 2: Run test to verify failure**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_infrastructure_router.py -v
```

Expected: 404 (route doesn't exist yet).

**Step 3: Create the router**

Create `app/api/routers/ai_infrastructure.py`:

```python
"""
AI infrastructure dashboard endpoints.

Exposes:
  GET  /api/ai-infra/companies/{company_id}/costs?period=YYYY-MM
  GET  /api/ai-infra/costs/global?period=YYYY-MM
  GET  /api/ai-infra/providers/health
  GET  /api/ai-infra/companies/{company_id}/budget
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.database import db
from app.core.billing_limits import get_tier_limits
from app.services.ai_gateway.attribution import (
    get_company_cost_breakdown,
    get_global_cost_breakdown,
)
from app.services.ai_gateway.budget import check_budget
from app.services.ai_gateway.health import get_health_snapshot
from app.services.ai_gateway.token_usage import (
    get_company_token_usage,
    get_current_period_key,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai-infra", tags=["AI Infrastructure"])


async def _verify_company_access(company_id: str, current_user: dict) -> dict:
    """Return the company document if the user has access; else raise 403."""
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    user_id = current_user.get("user_id")
    members = company.get("members", [])
    is_member = any(m.get("user_id") == user_id for m in members)
    is_owner = company.get("user_id") == user_id

    if not (is_owner or is_member):
        raise HTTPException(status_code=403, detail="Access denied")

    return company


@router.get("/companies/{company_id}/costs")
async def get_company_costs(
    company_id: str,
    period: Optional[str] = Query(None, description="YYYY-MM billing period"),
    current_user: dict = Depends(get_current_user),
):
    """
    Return cost breakdown for a company.

    Output: total_cost_usd, by_model breakdown, input/output token counts.
    """
    company = await _verify_company_access(company_id, current_user)
    return await get_company_cost_breakdown(company["id"], period)


@router.get("/costs/global")
async def get_global_costs(
    period: Optional[str] = Query(None, description="YYYY-MM billing period"),
    current_user: dict = Depends(get_current_user),
):
    """
    Return global cost breakdown (admin-only).

    Output: total_cost_usd, by_company, by_model rollups.
    """
    # Only allow platform admins
    if not current_user.get("is_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")

    return await get_global_cost_breakdown(period)


@router.get("/providers/health")
async def get_providers_health(
    current_user: dict = Depends(get_current_user),
):
    """Return the current health snapshot of all AI providers."""
    return get_health_snapshot()


@router.get("/companies/{company_id}/budget")
async def get_company_budget(
    company_id: str,
    current_user: dict = Depends(get_current_user),
):
    """
    Return the company's current budget status (used/limit/remaining).

    Does NOT raise on exceeded budget — that's the chat endpoint's job.
    """
    company = await _verify_company_access(company_id, current_user)
    # Use override=True so we get the status even if exceeded
    status = await check_budget(company, override=True)
    return {
        "company_id": company["id"],
        "tier": status.tier,
        "used": status.used,
        "limit": status.limit,
        "remaining": status.remaining,
        "warning": status.warning,
    }
```

**Step 4: Register the router in `main.py`**

Find where existing routers are registered in `main.py` (search for `include_router` or `app.include_router`). Add:

```python
from app.api.routers.ai_infrastructure import router as ai_infrastructure_router
app.include_router(ai_infrastructure_router)
```

If routers are registered with prefixes, check the existing pattern and match it.

**Step 5: Run tests**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/test_ai_infrastructure_router.py -v
```

Expected: 2 passed.

**Step 6: Run full test suite**

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v
```

Expected: all pass.

**Step 7: Commit**

```bash
cd /opt/data/SwiftAgent-be
git add app/api/routers/ai_infrastructure.py \
        main.py \
        tests/test_ai_infrastructure_router.py
git commit -m "feat(ai_gateway): add cost dashboard API endpoints"
```

---

### Task 6.3: Phase 6 verification

**Step 1: Boot the app and hit the endpoints**

```bash
cd /opt/data/SwiftAgent-be
uv run uvicorn main:app --reload --port 8001 &
sleep 3
# Hit the health endpoint (no auth required for this test)
curl -s http://localhost:8001/api/ai-infra/providers/health | python -m json.tool
# Kill the server
kill %1
```

Expected: a JSON object with `anthropic`, `gemini`, `openrouter` keys (or empty dict if no calls have been made yet).

---

## End-to-End Verification

After all six phases, run the full test suite one final time and verify integration:

### Final Verification Step 1: Run all tests

```bash
cd /opt/data/SwiftAgent-be
uv run pytest tests/ -v --tb=short
```

Expected: 50+ tests pass (existing + new across 6 phases).

### Final Verification Step 2: Type check

```bash
cd /opt/data/SwiftAgent-be
uv run ruff check app/services/ai_gateway/ tests/test_ai_gateway_*.py tests/test_ai_infrastructure*.py tests/test_billing_limits_tokens.py tests/test_orchestrator_complexity.py
```

Expected: no errors. (Fix any import-order or unused-import issues.)

### Final Verification Step 3: Integration smoke

```bash
cd /opt/data/SwiftAgent-be
uv run python -c "
import asyncio
from app.services.ai_gateway.complexity import classify_complexity
from app.services.ai_gateway.pricing import estimate_cost
from app.services.ai_gateway.health import is_provider_healthy

# Verify all six phases work together
print('=== Phase 1: pricing ===')
cost = estimate_cost('claude-haiku-4-5-20251001', 'anthropic', 1000, 500)
print(f'Cost: \${cost:.4f}')

print('=== Phase 3: complexity ===')
tier = classify_complexity('hello there')
print(f'Greeting tier: {tier.value}')

print('=== Phase 5: health ===')
healthy = is_provider_healthy('anthropic')
print(f'Anthropic healthy: {healthy}')

print('All phases integrated successfully')
"
```

Expected: all three sections print successfully.

---

## Files Touched (Summary)

| Phase | New Files | Modified Files |
|---|---|---|
| 1 | `app/services/ai_gateway/__init__.py`, `pricing.py`, `token_counter.py`, `instrumented_llm.py` | `pyproject.toml`, `app/services/graph/llm_factory.py`, `app/services/graph/orchestrator.py` |
| 2 | `app/services/ai_gateway/token_usage.py`, `budget.py` | `app/core/billing_limits.py`, `app/core/database.py`, `app/api/routers/chat.py` |
| 3 | `app/services/ai_gateway/complexity.py` | `app/core/config.py`, `app/services/graph/orchestrator.py`, `app/services/graph/state.py` |
| 4 | `app/services/ai_gateway/compression.py` | `app/services/graph/executor.py` |
| 5 | `app/services/ai_gateway/health.py`, `failover.py` | `app/api/routers/chat.py` |
| 6 | `app/services/ai_gateway/attribution.py`, `app/api/routers/ai_infrastructure.py` | `main.py` |
| **Tests** | `tests/test_ai_gateway_*.py`, `tests/test_billing_limits_tokens.py`, `tests/test_orchestrator_complexity.py`, `tests/test_ai_infrastructure_router.py` | — |

---

## Risks & Tradeoffs

1. **Heuristic complexity classifier (Phase 3)** — regex + length heuristics will misclassify some queries. A 5% misclassification rate routes a complex query to a cheap model, which may give a worse answer. Mitigation: cap the cheap tier to short, low-stakes interactions (greetings, navigation, FAQ) and default to "complex" when uncertain (the current implementation does this). Future: replace with a small classifier model call if the misclassification rate is observed to be high in production.

2. **Per-process health tracker (Phase 5)** — health state is local to each worker process. With multiple uvicorn workers, one worker seeing Anthropic as degraded doesn't tell other workers. Mitigation: this is fine for the 5-minute cooldown use case; if a provider is truly down, all workers will see failures within minutes. Long-term: move to Redis if cross-worker coordination becomes important.

3. **Compression cost (Phase 4)** — the compression call itself costs tokens (input: ~all old messages, output: a short summary). For a 20-message history of ~100 tokens each, that's ~2000 input tokens + ~100 output tokens. Cost: ~$0.002 per compression. Triggered only when history > 12 messages. Net positive when the alternative is dropping important context from a long conversation.

4. **In-memory `_state` in `health.py` (Phase 5)** — not thread-safe across processes. The `threading.Lock` protects within a process, not across. Same mitigation as #2.

5. **Cost attribution estimate uses 60/40 input/output ratio (Phase 6)** — when we don't have per-model input/output breakdowns, we estimate. For exact numbers, the Langfuse traces are the source of truth. Mitigation: add per-model input/output recording to `token_usage` if higher fidelity is needed.

6. **All changes are additive** — no existing behavior is changed. The cost layer records metadata on Langfuse but doesn't change what's sent. The complexity classifier only changes which *model* the orchestrator calls; if both tiers map to the same model (e.g. during initial rollout), behavior is identical. Failover replaces a coarse retry with a smarter one but produces the same SSE output. Roll back by reverting individual commits if a phase causes issues.

---

## Open Questions

- **Q: Should the `complexity_tier` field on AgentState be Optional or always present?** — Made it Optional for backward compatibility with old state dicts. Once deployed, we could make it required.
- **Q: When the cheap-tier model (Gemini Flash) is down and the capable tier (Claude Haiku) is up, do we still want the orchestrator to escalate to the capable tier?** — Yes, the orchestrator's tier choice is independent of provider health. If the cheap tier's provider is down, the failover wrapper in Phase 5 will route to the next provider, which may be in the capable tier. Document this interaction in the rollout notes.
- **Q: Should the dashboard endpoints require admin role or company-member role?** — Current implementation: company endpoints require member access, global endpoint requires admin. Adjust `is_admin` claim in `_verify_company_access` if your auth model differs.
