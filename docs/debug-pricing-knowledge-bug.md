# Debugging & Fix Plan: Chatbot Cannot Retrieve Pricing from Knowledge Base

**Platform:** Swift Agents (swiftagents.org) — `SwiftAgent-be` repo
**Reporter:** Ebuka (co-founder, SerendptAI)
**Severity:** P1 — production chatbot leaking hallucinated "I don't have access" answer to paying customers
**Affected surface:** all `/api/sdk/{company_id}/chat` and `/api/chat/{company_id}/chat` widget/SDK chat endpoints

---

## 1. Executive Summary

The bot says *"I don't have access to our pricing information in the knowledge base..."* because the `search_knowledge_base` tool returns an error (or an empty result) for **every** end-customer message, not because the knowledge base is missing data. The orchestrator then routes to the knowledge agent, the tool fails silently, the agent has no retrieved context, and the LLM falls back to a templated "escalate to sales" hallucination.

**The bug is in the access-control contract between three layers, not in Qdrant, not in the embeddings, and not in ingestion.** A pricing doc uploaded by the dashboard owner is correctly embedded into Qdrant with a specific `user_id` and `company_id`, but the SDK/widget end-user has *neither* the same `user_id` (they are anonymous) nor does the tool allow `user_id`-less lookups. So retrieval is structurally unreachable for the customer-facing path.

Two distinct defects combine to produce the symptom:

| # | Defect | Location | Impact |
|---|--------|----------|--------|
| 1 | `search_knowledge_base` tool hard-rejects calls when `state["user_id"]` is falsy | `app/services/graph/tools.py:30` | Tool returns `{"error": "Company context not available"}` for every SDK/widget user (they always have `user_id=None`) |
| 2 | `knowledge_service.search_knowledge` requires `user_id` in Qdrant `must` filter | `app/services/knowledge_service.py:97-104` | Even if the tool check were removed, queries without a `user_id` would return zero hits — knowledge docs are tagged with the dashboard owner's `user_id`, not the customer's |

A third, lower-likelihood contributor:

| # | Defect | Location | Impact |
|---|--------|----------|--------|
| 3 | Hard-coded `threshold=0.5` in the tool, but global `CONFIDENCE_THRESHOLD=0.7` in the service default and `QueryRequest` schema | `tools.py:34`, `config.py:36`, `knowledge_models.py:40` | Even when retrieval does run, the tool's relaxed threshold masks low-quality matches; switching to the service default can drop borderline-pricing hits |
| 4 | KB ingest is `BackgroundTasks` — failures are silently logged, not surfaced to the uploader | `api/routers/knowledge.py:60-75` | A failed Cloudinary/text-extract/Gemini-embed during ingest is invisible to the user who uploaded the pricing PDF |
| 5 | KB ingest writes to MongoDB **before** Qdrant is populated (write-then-async-embed) | `api/routers/knowledge.py:58-75` | `GET /api/knowledge/` shows the doc, but `search` finds nothing — the "ghost document" anti-pattern |

The plan below addresses all five.

---

## 2. Root Cause Investigation — In Priority Order

Run these in sequence. Stop as soon as one reproduces the failure mode; the rest of the steps confirm scope and rule out adjacent causes.

### Step 1 (5 min) — Reproduce the exact symptom against the live SwiftAgents deployment

Why first: cheapest confirmation, isolates whether the bug is the tool-error path or a true empty-result path.

```bash
# In a local clone with .env pointed at production Qdrant + Mongo
curl -X POST https://swiftagents.org/api/sdk/<COMPANY_ID>/chat \
  -H "Authorization: Bearer <SDK_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"session_id":"debug-pricing-001","email":"[email protected]","message":"what are your pricing plans?"}'
```

Observe the SSE stream. You are looking for the `tool` event named `search_knowledge_base`. Capture:

- The full SSE log to disk (`--no-buffer` + tee)
- Whether the response includes `"error": "Company context not available"` → confirms **Defect #1**
- Or returns `"results": []` with no error → confirms **Defect #2** or empty index

If you cannot reproduce, jump to Step 2.

### Step 2 (10 min) — Query Qdrant directly for the company

Bypasses the bot entirely. Proves whether pricing data exists at all and is retrievable.

```python
# scripts/debug_qdrant.py
import asyncio
from qdrant_client import QdrantClient
from app.core.config import settings
from app.core.database import qdrant_client

COLL = settings.QDRANT_COLLECTION_NAME
COMPANY_ID = "<COMPANY_ID>"  # from the live tenant

async def main():
    # 1. Collection exists?
    exists = await qdrant_client.collection_exists(COLL)
    print("collection_exists:", exists)

    # 2. Total points
    info = await qdrant_client.get_collection(COLL)
    print("points_count:", info.points_count)

    # 3. Filter by company_id only (no user_id)
    pts, _ = await qdrant_client.scroll(
        collection_name=COLL,
        scroll_filter=models.Filter(must=[
            models.FieldCondition(key="company_id", match=models.MatchValue(value=COMPANY_ID))
        ]),
        limit=20,
        with_payload=True,
    )
    print(f"docs_for_company={COMPANY_ID}: {len(pts)}")
    for p in pts:
        print(" -", p.payload.get("title"), "user_id=", p.payload.get("user_id"),
              "len(content)=", len(p.payload.get("page_content", "")))

asyncio.run(main())
```

**Expected outcomes:**

| Outcome | Diagnosis |
|---------|-----------|
| `points_count == 0` | No KB has ever been ingested (different bug — see §3.A) |
| `points_count > 0` but `docs_for_company == 0` | Pricing data is uploaded to a *different* `company_id` — check the dashboard owner vs the customer widget company |
| `docs_for_company > 0` but `len(content) == 0` | Ingest half-failed — DB has the doc, Qdrant has the embedding but empty `page_content` (Defect #5) |
| `docs_for_company > 0` and content present | Retrieval is fine, the bug is **definitely** in the tool gate — go to Step 3 |

### Step 3 (5 min) — Inspect the actual Qdrant query the tool issues

Add a one-line log statement to confirm the `must` filter the bot is sending:

```python
# app/services/knowledge_service.py — inside search_knowledge, before the query_points call
logger.info(
    "kb_search user_id=%s company_id=%s query=%r limit=%s threshold=%s",
    user_id, company_id, query[:80], limit, threshold,
)
```

Re-trigger the failing query, then `grep` the application logs for `kb_search`. You will see one of:

```
kb_search user_id=None company_id=<X> query='pricing' ...
kb_search user_id=<owner-uuid> company_id=<X> query='pricing' ...
kb_search user_id=None company_id=None query='pricing' ...
```

The first log line is **Defect #1** in action (tool didn't even run because of the `not user_id` guard). The second is the expected-but-mismatched path (Qdrant was indexed with the owner's `user_id`, queried with the same value — but the tool is being called with the SDK user's `user_id` which is `None`). The third means `company_id` is also missing — different bug, check `state` propagation.

### Step 4 (10 min) — Cross-check ingestion records vs Qdrant payload

```python
# scripts/check_kb_consistency.py
import asyncio
from app.core.database import db
from app.core.config import settings
from app.core.database import qdrant_client
from qdrant_client.http import models

COLL = settings.QDRANT_COLLECTION_NAME
COMPANY_ID = "<COMPANY_ID>"

async def main():
    mongo_docs = await db.documents.find(
        {"user_id": {"$exists": True}}
    ).to_list(length=500)
    print(f"mongo documents: {len(mongo_docs)}")

    for d in mongo_docs:
        if d.get("metadata", {}).get("company_id") != COMPANY_ID:
            continue
        # Look for matching Qdrant points
        qdrant_hits = await qdrant_client.scroll(
            collection_name=COLL,
            scroll_filter=models.Filter(must=[
                models.FieldCondition(key="doc_id", match=models.MatchValue(value=d["id"]))
            ]),
            limit=5,
        )
        pts, _ = qdrant_hits
        print(f"  mongo_id={d['id']}  title={d['title']!r}  qdrant_matches={len(pts)}")

asyncio.run(main())
```

Any row showing `qdrant_matches=0` is a confirmed **Defect #4/#5** (ingest silently failed and the doc only lives in MongoDB).

### Step 5 (5 min) — Run a direct semantic search to confirm embedding quality

```python
import asyncio
from google import genai
from app.core.config import settings
from app.core.database import qdrant_client
from qdrant_client.http import models

async def main():
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    resp = await client.aio.models.embed_content(
        model="gemini-embedding-001",
        contents="pricing plans cost",
        config={"task_type": "RETRIEVAL_QUERY"},
    )
    qvec = resp.embeddings[0].values
    results = await qdrant_client.query_points(
        collection_name=COLL,
        query=qvec,
        query_filter=models.Filter(must=[
            models.FieldCondition(key="company_id", match=models.MatchValue(value="<COMPANY_ID>"))
        ]),
        limit=5,
        with_payload=True,
    )
    for r in results.points:
        print(f"  score={r.score:.3f}  title={r.payload.get('title')!r}")

asyncio.run(main())
```

If **no** score is ≥ 0.5, the pricing document is either missing, has the wrong `company_id`, or its content is too thin to match. If scores are ≥ 0.5, retrieval is healthy and the bug is 100% in the tool/state gate.

### Step 6 (5 min) — Check the orchestrator's intent routing

If you never see `transfer_to_knowledge` being called for "pricing", the bug is one layer up:

```python
# In app/services/graph/orchestrator.py, add a log:
logger.info("orchestrator intent=%s tool_calls=%s", intent, response.tool_calls)
```

Confirmed working: the orchestrator prompt explicitly mentions pricing/policies. If the model *doesn't* call `transfer_to_knowledge`, it may be over-routing to `transfer_to_navigation` ("how do I find pricing") — Step 6 catches that.

### Step 7 — Check log lines for known failure modes

```bash
# These are the specific log patterns that should already be alerting
grep -E "Embedding error during knowledge search" app.log
grep -E "Background knowledge ingestion failed" app.log
grep -E "Company context not available" app.log
grep -E "no relevant documents found" app.log
```

Quantify: how many `Company context not available` per hour? If it's non-zero and matches your chat volume, you have a full reproduction.

---

## 3. Potential Causes — Ranked by Likelihood

| Rank | Cause | Confidence | Evidence |
|------|-------|-----------|----------|
| **1** | `search_knowledge_base` tool requires `state["user_id"]` (`tools.py:30`) but every SDK/widget chat passes `user_id=None` (`sdk.py:269`) | **Very High** | Direct read of both files; gate is `if not user_id: return error` |
| **2** | Qdrant `must` filter always includes `user_id` (`knowledge_service.py:97-98`), so even after fixing the gate, queries without a user_id return zero hits | **Very High** | Filter is unconditional; no fallback path |
| 3 | Pricing doc never ingested: empty ingest, Cloudinary failure, Gemini embed error, or BackgroundTask silently dropped | Medium | Step 4 will tell you; logs will show `Background knowledge ingestion failed` |
| 4 | Pricing doc was ingested but under a *different* `company_id` (e.g. uploaded to a staging tenant) | Medium | Step 2's `docs_for_company == 0` reveals it |
| 5 | Knowledge-agent prompt tells the LLM to refuse when empty (`knowledge_agent.py:12`: "If the answer is not in the knowledge base, say so clearly") combined with the empty result produces the exact phrasing the customer saw | High (UI-side) | The LLM was given an empty tool result; its refusal template is the visible symptom |
| 6 | Embedding model name drift: `EMBEDDING_MODEL = "gemini-embedding-001"` (config.py:86) produces 3072-dim vectors, but if a deploy was ever done with the older `models/embedding-001` the dimension may have shifted | Low | Step 5 will show low/zero similarity |
| 7 | Qdrant collection name mismatch across deploys: `QDRANT_COLLECTION_NAME = "knowledge_docs_gemini"` (config.py:20). If the env var was changed in one environment but not another, the wrong collection is queried | Low | Compare `QDRANT_COLLECTION_NAME` in `.env` against `get_collection().points_count` |
| 8 | Hard threshold mismatch: tool uses `0.5`, service default is `0.7`, schema default is `0.7`. A borderline pricing hit at 0.65 would pass in the API but be filtered out by other call sites | Low | Will manifest as "sometimes works, sometimes doesn't" |

---

## 4. Fix Strategies (Mapped to Each Cause)

### Fix A — Allow `user_id`-less lookups for SDK/widget customers  (Causes #1, #2)

The core issue: KB documents are scoped to a *company* in the customer-facing surface, not to the company owner. The dashboard owner's `user_id` is an internal identity, not a tenancy boundary.

**Change 1: `app/services/graph/tools.py` (lines 26-35)**

```python
# before
state = _get_state(config)
company_id = state.get("company_id")
user_id = state.get("user_id")
if not company_id or not user_id:
    return {"error": "Company context not available"}

search_result = await knowledge_service.search_knowledge(
    user_id, query, limit=3, threshold=0.5, company_id=company_id
)

# after
state = _get_state(config)
company_id = state.get("company_id")
user_id = state.get("user_id")  # may be None for SDK/widget anonymous users
if not company_id:
    return {"error": "Company context not available"}

search_result = await knowledge_service.search_knowledge(
    user_id, query, limit=3, threshold=0.5, company_id=company_id
)
```

Apply the same change to `scrape_documentation_link` (line 50-54) — the `not user_id` guard there is also wrong for SDK flows.

**Change 2: `app/services/knowledge_service.py` (lines 96-104)**

```python
# before
must_conditions = [
    models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))
]
if company_id:
    must_conditions.append(
        models.FieldCondition(key="company_id", match=models.MatchValue(value=company_id))
    )

# after
# For SDK/widget users (user_id is None), scope by company only.
# The dashboard-owner `user_id` is a tenancy-control column, not a customer filter.
if user_id:
    must_conditions = [
        models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id))
    ]
    if company_id:
        must_conditions.append(
            models.FieldCondition(key="company_id", match=models.MatchValue(value=company_id))
        )
else:
    must_conditions = []
    if company_id:
        must_conditions.append(
            models.FieldCondition(key="company_id", match=models.MatchValue(value=company_id))
        )
    else:
        # last-resort safety: never return unfiltered results
        return {"results": [], "confidence": 0.0, "escalate": True}
```

> **Security note:** This widens who can see company KB. That's the *intended* behavior for a public-facing widget — the doc is already on the customer's website, so any visitor can read it. But if you have any KB items with truly private content (admin-only docs, internal pricing), split them into a separate Qdrant collection with stricter access (e.g. `knowledge_docs_private`) and keep `user_id` mandatory for that path. Don't leave that decision implicit.

### Fix B — Surface ingest failures instead of swallowing them  (Cause #3)

**`app/api/routers/knowledge.py:60-75`**

Replace `BackgroundTasks` with a write-ahead log pattern: insert into a `knowledge_ingest_jobs` collection with `status="pending"`, run the embed synchronously or via a tracked worker, and update the status. Add a `GET /api/knowledge/{id}/status` endpoint the dashboard can poll.

If you can't migrate off `BackgroundTasks` in this hotfix, at minimum:
1. Add an `ingest_status: str` field on the document (`pending` / `ready` / `failed` / `error: <msg>`)
2. Add a daily reconciliation script that compares MongoDB doc count vs Qdrant point count and alerts on drift

### Fix C — Single source of truth for confidence threshold  (Cause #8)

**Three different thresholds exist:**

| File | Value |
|------|-------|
| `app/core/config.py:36` | `CONFIDENCE_THRESHOLD: float = 0.7` (unused) |
| `app/models/knowledge_models.py:40` | `threshold: float = 0.7` (API default) |
| `app/services/graph/tools.py:34` | `threshold=0.5` (tool default) |
| `app/services/knowledge_service.py:75` | `threshold: float = 0.7` (service default) |

```python
# app/services/graph/tools.py
from app.core.config import settings
...
search_result = await knowledge_service.search_knowledge(
    user_id, query, limit=3, threshold=settings.CONFIDENCE_THRESHOLD, company_id=company_id
)
```

Then delete the now-redundant `CONFIDENCE_THRESHOLD` from `config.py` *only* if no other code references it. (Do `grep -r CONFIDENCE_THRESHOLD app/` first — if only the `knowledge_models.py` schema reads it, keep the env var and have the tool import it.)

### Fix D — Robust prompt for the knowledge agent's empty case  (Cause #5)

**`app/services/graph/agents/knowledge_agent.py:8-12`**

```python
KNOWLEDGE_PROMPT = """You are the Knowledge Base Expert.
Your job is to answer the user's question using the company's knowledge base.
Always use the `search_knowledge_base` tool to find answers.

CRITICAL RULES:
- The `search_knowledge_base` tool returns the GROUND TRUTH for this company.
- If the tool returns results, base your answer strictly on those results.
- If the tool returns an error or empty results, tell the user:
  "I couldn't retrieve that information from our knowledge base right now. 
   This may be a temporary issue — please try again in a moment, or contact 
   our support team for immediate help."
  Do NOT claim access is restricted, do NOT redirect to sales, do NOT apologize
  for policy limits. The retrieval system is internal — the user does not need
  to know about it.

If the user provides a link and asks you to learn from it, use `scrape_documentation_link`.
Never guess or hallucinate information."""
```

This decouples the LLM's user-facing response from the *cause* of the empty result. Whether it's a transient Qdrant blip, a missing doc, or a permission mismatch, the customer sees the same recoverable message instead of a confident hallucination that blames them.

### Fix E — Ingest observability  (Cause #3)

Add structured logging at the boundary of every external call during ingest:

```python
# app/services/knowledge_service.py
import structlog
log = structlog.get_logger()

async def ingest_document(...):
    log.info("kb.ingest.start", doc_id=doc_id, user_id=user_id, content_len=len(content))
    try:
        response = await gemini_client.aio.models.embed_content(...)
        log.info("kb.ingest.embedded", doc_id=doc_id, vec_dim=len(response.embeddings[0].values))
    except Exception as e:
        log.error("kb.ingest.embed_failed", doc_id=doc_id, error=str(e))
        raise
    try:
        await qdrant_client.upsert(...)
        log.info("kb.ingest.upserted", doc_id=doc_id, qdrant_id=...)
    except Exception as e:
        log.error("kb.ingest.upsert_failed", doc_id=doc_id, error=str(e))
        raise
```

Pipe these to your observability stack (Langfuse is already wired via `@observe` — extend it). The first thing you'll see in production is whether the failure is `embed_failed` (Gemini quota) or `upsert_failed` (Qdrant) or something else.

---

## 5. Verification Steps After Each Fix

Run these in this order. Each step has a pass/fail criterion that takes < 5 minutes.

### V1 — Unit tests for the gate

```python
# tests/test_kb_tool_gate.py
import pytest
from app.services.graph.tools import search_knowledge_base

@pytest.mark.asyncio
async def test_search_with_company_only():
    """SDK/widget users have user_id=None but a valid company_id."""
    result = await search_knowledge_base.ainvoke(
        {"query": "pricing"},
        config={"configurable": {"state": {"company_id": "co_123", "user_id": None}}}
    )
    assert "error" not in result, f"got: {result}"

@pytest.mark.asyncio
async def test_search_with_neither_is_rejected():
    result = await search_knowledge_base.ainvoke(
        {"query": "pricing"},
        config={"configurable": {"state": {"company_id": None, "user_id": None}}}
    )
    assert "error" in result
```

**Pass:** Both pass. The first proves Cause #1 is fixed; the second proves we didn't open the floodgates.

### V2 — Integration test against a real Qdrant

```python
# tests/test_kb_service_filter.py
import pytest
from app.services import knowledge_service

@pytest.mark.asyncio
async def test_search_by_company_only_finds_docs():
    # Setup: ingest a doc tagged with one user_id, scoped to a company_id
    user_id = "owner-uuid-1"
    company_id = "co_pricing_test"
    await knowledge_service.ingest_document(
        user_id=user_id, doc_id="doc-1",
        title="Pricing", content="Our pro plan is $49/month.",
        metadata={"company_id": company_id},
    )
    # Query as an SDK user (no user_id)
    result = await knowledge_service.search_knowledge(
        user_id=None, query="how much does pro cost",
        limit=5, threshold=0.5, company_id=company_id,
    )
    assert len(result["results"]) >= 1
    assert "pro plan" in result["results"][0]["content"]
```

**Pass:** Test passes. Proves Cause #2 is fixed end-to-end.

### V3 — Live reproduction on production

Re-run the curl from §2 Step 1 with the same `session_id` and tenant.

**Pass:** The SSE stream now contains a `tool: search_knowledge_base` event with non-empty `sources` containing the pricing doc. The text response cites the plan name and price.

**Soft pass:** The tool still returns no results, but the bot response is the new "I couldn't retrieve that information right now" message instead of the sales-escalation message. (Indicates the tool is now actually being called; the underlying KB still needs investigation.)

### V4 — Tenant-isolation regression test

Critical: prove we didn't leak docs across companies.

```python
@pytest.mark.asyncio
async def test_search_does_not_leak_across_companies():
    await knowledge_service.ingest_document(
        user_id="u1", doc_id="d1", title="Pricing A",
        content="Plan A costs $10", metadata={"company_id": "co_A"},
    )
    await knowledge_service.ingest_document(
        user_id="u2", doc_id="d2", title="Pricing B",
        content="Plan B costs $20", metadata={"company_id": "co_B"},
    )
    res = await knowledge_service.search_knowledge(
        user_id=None, query="pricing", company_id="co_A"
    )
    contents = " ".join(r["content"] for r in res["results"])
    assert "Plan A" in contents
    assert "Plan B" not in contents, "tenant leak!"
```

**Pass:** Each tenant sees only its own data.

### V5 — Threshold consistency

```bash
grep -rn "threshold" app/ | grep -v test
```

**Pass:** Exactly one definition of the threshold remains, and the tool uses it.

### V6 — Empty-result prompt regression

Manual: ask the bot about a doc you know doesn't exist (e.g. "what is the CEO's home address?"). Confirm the new prompt produces the recoverable "please try again" message, not the old "reach out to sales" message.

---

## 6. Prevention — Stop This Class of Bug From Recurring

### P1 — Add a `kb_drift` daily cron

```python
# app/scripts/kb_drift_check.py — run via cron at 03:00 UTC
"""Compare MongoDB documents vs Qdrant points per company. Page on drift > 5%."""
```

Already alluded to in Fix B. This catches *every* future ingest failure: Qdrant outage, Gemini quota, schema change, bad content extraction. Without it, the only signal you'll ever get is "the bot is wrong again."

### P2 — Add a CI integration test that requires KB search to work

Embed a fixture document into a test Qdrant (via `qdrant_client` mock or testcontainers), then have the integration test invoke `search_knowledge_base` with an SDK-shaped state (`user_id=None`, `company_id="fixture"`). This would have caught Defect #1 in CI before deploy.

### P3 — Add a `/api/debug/kb` admin endpoint

```python
@router.get("/debug/kb")
async def debug_kb(company_id: str, current_user=Depends(get_admin_user)):
    """Return: doc count in Mongo, point count in Qdrant, last 5 ingest jobs, 
    top-3 semantic search hits for a probe query."""
```

Lets Ebuka and support staff triage "is the bot lying or is the data missing" in 30 seconds without SSH.

### P4 — Lock down the "user_id" semantic

The root conceptual issue is that `user_id` means two different things in the same DB:

- **Owner identity** (the dashboard account that *uploaded* a doc) → belongs in the `documents` collection as a foreign key
- **Tenancy scope** (which company's KB to query) → belongs in the `company_id` column

Refactor suggestion: rename the Qdrant `user_id` payload field to `owner_user_id` so the semantics are unambiguous. If you do this, also rename the `must` filter to use `owner_user_id` and only filter by it for *admin* endpoints, not for customer chat. This is the single change that makes Defect #1 and #2 impossible to reintroduce.

### P5 — Tighten the knowledge agent's prompt and add a refusal-classifier

Track every response where the bot says "I don't have access" or "reach out to sales" and surface them in the dashboard. If that number spikes, page on it. Right now, this bug existed silently — the dashboard shows resolved/unresolved chats but not *what kind* of answer was given.

### P6 — Use a write-ahead ingest pattern

`BackgroundTasks` is fire-and-forget. The fact that the API returns `201 Created` while the doc may never make it to Qdrant is a contract violation customers will keep hitting. Either:

- **Synchronous ingest** for KB docs (small enough; < 5s typical for < 50 pages) and 201 = "ready to query"
- **Async via a tracked job** with a `status` field the dashboard polls

The current code is the worst of both worlds.

### P7 — Add a "last successful KB search" health metric

Expose it on the dashboard. If a tenant's last successful KB search was > 24h ago and they have chats, something is wrong — and right now, you'd never know until a customer complained.

---

## 7. Suggested Rollout Order

1. **Hotfix (deploy in < 1 hour):** Fix A only (gate + filter). This unblocks the bot immediately.
2. **Same deploy:** Fix C (single threshold), Fix D (prompt rewrite).
3. **Within 24h:** Fix B (ingest observability) + Fix E (structured logs).
4. **Next sprint:** P1 (drift cron), P2 (CI test), P3 (debug endpoint), P6 (write-ahead ingest).
5. **Next quarter:** P4 (rename `user_id` → `owner_user_id`) — high churn, but eliminates the whole bug class.

---

## 8. Files Touched (Summary)

| File | Lines | Reason |
|------|-------|--------|
| `app/services/graph/tools.py` | 30, 54 | Remove bogus `not user_id` gate |
| `app/services/knowledge_service.py` | 96-104 | Conditional `user_id` filter; surface ingest errors |
| `app/services/graph/agents/knowledge_agent.py` | 8-12 | Stop hallucinating "I don't have access" |
| `app/core/config.py` | 36 | (Optional) Keep `CONFIDENCE_THRESHOLD` as the single source |
| `app/api/routers/knowledge.py` | 60-75 | Ingest status tracking |
| **NEW** `app/scripts/kb_drift_check.py` | — | Daily reconciliation cron |
| **NEW** `tests/test_kb_tool_gate.py` | — | Unit + integration coverage |
| **NEW** `app/api/routers/debug.py` | — | Admin triage endpoint |

---

## 9. Open Questions to Confirm with Ebuka Before Coding

1. **Is there any KB content that should NOT be visible to anonymous widget users?** (Internal admin docs, draft pricing, NDA-gated material.) If yes, those need a separate collection with stricter access — Fix A would otherwise expose them. The current code already has `category` in the payload; could gate on `category in {"internal", "draft"}`.
2. **Should the bot offer to fall back to a sales contact when KB truly returns nothing**, or always say "try again later"? The current behavior (escalate to sales) may be a deliberate product decision the prompt hard-codes — verify before rewriting.
3. **Is the dashboard showing a "knowledge base is empty" warning when ingest fails?** If not, the dashboard's UX contributes to the bug going undetected for so long.
4. **Is the `CONFIDENCE_THRESHOLD=0.7` env var actually deployed?** `grep -r CONFIDENCE_THRESHOLD app/` to confirm before deleting from config.

---

## 10. TL;DR for the Parent Agent

The chatbot's "I don't have access to our pricing" message is a hallucination layered on top of a **tool-gate bug**: `search_knowledge_base` in `app/services/graph/tools.py:30` rejects every SDK/widget chat because they have `user_id=None` (set at `app/api/routers/sdk.py:269`), and `knowledge_service.search_knowledge` (`app/services/knowledge_service.py:97-104`) requires a `user_id` filter that the SDK user can't satisfy. Fixing the gate and making the filter conditional on `user_id` immediately unblocks pricing retrieval. Five files, two small logic changes, one prompt rewrite. Add a daily Mongo↔Qdrant drift check so the next silent failure doesn't take weeks to surface.
