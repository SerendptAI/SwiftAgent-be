from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_chunk_text_is_bounded_and_overlapping():
    from app.services.knowledge_embedding_service import chunk_text

    text = " ".join(f"word-{index}" for index in range(240))
    chunks = chunk_text(text, chunk_size=60, overlap=10)

    assert len(chunks) > 1
    assert all(len(chunk.split()) <= 60 for chunk in chunks)
    assert chunks[0].split()[-10:] == chunks[1].split()[:10]


def test_point_id_is_deterministic_and_chunk_specific():
    from app.services.knowledge_embedding_service import deterministic_point_id

    first = deterministic_point_id("comp", "page", "hash", 0)
    repeated = deterministic_point_id("comp", "page", "hash", 0)
    next_chunk = deterministic_point_id("comp", "page", "hash", 1)

    assert first == repeated
    assert first != next_chunk


@pytest.mark.asyncio
async def test_replace_page_vectors_upserts_before_deleting_stale_vectors():
    from app.services import knowledge_embedding_service as service

    events = []
    qdrant = MagicMock()

    async def upsert(**kwargs):
        events.append("upsert")

    async def delete(**kwargs):
        events.append("delete")

    qdrant.upsert = upsert
    qdrant.delete = delete
    embedding = SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1, 0.2])])
    gemini = MagicMock()
    gemini.aio.models.embed_content = AsyncMock(return_value=embedding)

    with (
        patch.object(service, "qdrant_client", qdrant),
        patch.object(service, "_get_gemini_client", return_value=gemini),
        patch.object(service, "ensure_collection", AsyncMock()),
    ):
        count = await service.replace_page_vectors(
            company_id="comp",
            user_id="user",
            page_id="page",
            source_id="source",
            url="https://example.com/docs",
            title="Docs",
            content="short page content",
            content_hash="hash-v2",
        )

    assert count == 1
    assert events == ["upsert", "delete"]


@pytest.mark.asyncio
async def test_delete_page_vectors_filters_by_company_and_page():
    from app.services import knowledge_embedding_service as service

    qdrant = MagicMock()
    qdrant.delete = AsyncMock()

    with patch.object(service, "qdrant_client", qdrant):
        await service.delete_page_vectors("comp", "page")

    qdrant.delete.assert_awaited_once()
    selector = qdrant.delete.await_args.kwargs["points_selector"]
    conditions = selector.filter.must
    assert {(item.key, item.match.value) for item in conditions} == {
        ("company_id", "comp"),
        ("page_id", "page"),
    }
