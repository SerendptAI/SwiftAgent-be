"""Tests for KB search tool gate and service — covering the user_id=None SDK flow."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.runnables import RunnableConfig

from app.services.knowledge_service import search_knowledge
from app.services.graph.tools import search_knowledge_base, scrape_documentation_link


# ---------------------------------------------------------------------------
# search_knowledge_base tool gate
# ---------------------------------------------------------------------------

def _make_config(user_id=None, company_id="co_test") -> RunnableConfig:
    return {"configurable": {"state": {"user_id": user_id, "company_id": company_id}}}


@pytest.mark.asyncio
async def test_tool_allows_company_only():
    """SDK/widget users have user_id=None but a valid company_id — should NOT be rejected."""
    with patch("app.services.graph.tools.knowledge_service.search_knowledge", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = {
            "results": [{"title": "Pricing", "content": "Pro plan $49", "score": 0.85}],
            "confidence": 0.85,
            "escalate": False,
        }
        result = await search_knowledge_base.ainvoke(
            {"query": "pricing plans"},
            config=_make_config(user_id=None, company_id="co_test"),
        )
        assert "error" not in result, f"Tool rejected anonymous SDK user: {result}"
        assert len(result["results"]) == 1
        assert result["results"][0]["title"] == "Pricing"
        # verify the service was called with user_id=None
        mock_search.assert_called_once()
        args, _ = mock_search.call_args
        assert args[0] is None, f"Expected user_id=None, got {args[0]}"


@pytest.mark.asyncio
async def test_tool_rejects_no_company():
    """Both user_id and company_id missing — must reject."""
    result = await search_knowledge_base.ainvoke(
        {"query": "pricing"},
        config=_make_config(user_id=None, company_id=None),
    )
    assert "error" in result
    assert "Company context not available" in result["error"]


@pytest.mark.asyncio
async def test_tool_dashboard_user_still_works():
    """Dashboard users with a real user_id still pass through."""
    with patch("app.services.graph.tools.knowledge_service.search_knowledge", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = {
            "results": [{"title": "Guide", "content": "Setup steps", "score": 0.92}],
            "confidence": 0.92,
            "escalate": False,
        }
        result = await search_knowledge_base.ainvoke(
            {"query": "how to set up"},
            config=_make_config(user_id="owner_uuid", company_id="co_test"),
        )
        assert "error" not in result
        mock_search.assert_called_once()
        args, _ = mock_search.call_args
        assert args[0] == "owner_uuid"


@pytest.mark.asyncio
async def test_tool_empty_results():
    """Tool returns empty results gracefully (no error)."""
    with patch("app.services.graph.tools.knowledge_service.search_knowledge", new_callable=AsyncMock) as mock_search:
        mock_search.return_value = {"results": [], "confidence": 0.0, "escalate": True}
        result = await search_knowledge_base.ainvoke(
            {"query": "nonexistent"},
            config=_make_config(user_id=None, company_id="co_test"),
        )
        assert "error" not in result
        assert result.get("results") == []
        assert "No relevant documents found" in result.get("message", "")


# ---------------------------------------------------------------------------
# scrape_documentation_link tool gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_company_only():
    """scrape_documentation_link should also allow user_id=None SDK users."""
    with patch("app.services.documentation_scraper_service.scrape_and_ingest_docs", new_callable=AsyncMock) as mock_scrape:
        mock_scrape.return_value = True
        result = await scrape_documentation_link.ainvoke(
            {"url": "https://example.com/docs"},
            config=_make_config(user_id=None, company_id="co_test"),
        )
        assert "error" not in result
        assert result["success"] is True


@pytest.mark.asyncio
async def test_scrape_rejects_no_company():
    """scrape_documentation_link without company_id must reject."""
    result = await scrape_documentation_link.ainvoke(
        {"url": "https://example.com/docs"},
        config=_make_config(user_id=None, company_id=None),
    )
    assert "error" in result


# ---------------------------------------------------------------------------
# search_knowledge service — Qdrant filter logic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_filters_by_company_only_when_no_user():
    """When user_id=None, the Qdrant must filter should NOT include user_id."""
    with patch("app.services.knowledge_service.qdrant_client") as mock_qdrant, \
         patch("app.services.knowledge_service._get_gemini_client") as mock_gemini_factory:

        # mock gemini embed response
        mock_embed_response = MagicMock()
        mock_embed_response.embeddings = [MagicMock(values=[0.1] * 3072)]
        mock_gemini = MagicMock()
        mock_gemini.aio.models.embed_content = AsyncMock(return_value=mock_embed_response)
        mock_gemini_factory.return_value = mock_gemini

        # mock collection exists check
        mock_qdrant.collection_exists = AsyncMock(return_value=True)

        # mock query_points
        mock_qdrant.query_points = AsyncMock()
        mock_qdrant.query_points.return_value.points = []

        await search_knowledge(
            user_id=None,
            query="pricing plans",
            limit=5,
            threshold=0.5,
            company_id="co_test",
        )

        # verify the Qdrant filter
        call_kwargs = mock_qdrant.query_points.call_args.kwargs
        qfilter = call_kwargs.get("query_filter")
        assert qfilter is not None
        must = qfilter.must
        # should have company_id but NOT user_id
        field_keys = [cond.key for cond in must]
        assert "company_id" in field_keys
        assert "user_id" not in field_keys, "user_id filter should be absent for anonymous users"


@pytest.mark.asyncio
async def test_service_includes_user_id_filter_when_present():
    """Dashboard user — must filter by both user_id and company_id."""
    with patch("app.services.knowledge_service.qdrant_client") as mock_qdrant, \
         patch("app.services.knowledge_service._get_gemini_client") as mock_gemini_factory:

        mock_embed_response = MagicMock()
        mock_embed_response.embeddings = [MagicMock(values=[0.1] * 3072)]
        mock_gemini = MagicMock()
        mock_gemini.aio.models.embed_content = AsyncMock(return_value=mock_embed_response)
        mock_gemini_factory.return_value = mock_gemini

        mock_qdrant.collection_exists = AsyncMock(return_value=True)
        mock_qdrant.query_points = AsyncMock()
        mock_qdrant.query_points.return_value.points = []

        await search_knowledge(
            user_id="owner_uuid",
            query="pricing",
            company_id="co_test",
        )

        call_kwargs = mock_qdrant.query_points.call_args.kwargs
        qfilter = call_kwargs.get("query_filter")
        must = qfilter.must
        field_keys = [cond.key for cond in must]
        assert "company_id" in field_keys
        assert "user_id" in field_keys


@pytest.mark.asyncio
async def test_service_safety_when_both_missing():
    """Neither user_id nor company_id — must return empty, never query unfiltered."""
    with patch("app.services.knowledge_service.qdrant_client") as mock_qdrant, \
         patch("app.services.knowledge_service._get_gemini_client") as mock_gemini_factory:

        mock_embed_response = MagicMock()
        mock_embed_response.embeddings = [MagicMock(values=[0.1] * 3072)]
        mock_gemini = MagicMock()
        mock_gemini.aio.models.embed_content = AsyncMock(return_value=mock_embed_response)
        mock_gemini_factory.return_value = mock_gemini

        mock_qdrant.collection_exists = AsyncMock(return_value=True)
        mock_qdrant.query_points = AsyncMock()

        result = await search_knowledge(
            user_id=None,
            query="pricing",
            company_id=None,
        )

        # Should short-circuit before query_points
        assert result["results"] == []
        assert result["escalate"] is True
        mock_qdrant.query_points.assert_not_called()
