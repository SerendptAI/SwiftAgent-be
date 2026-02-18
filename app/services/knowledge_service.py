from typing import List, Dict, Any
from uuid import uuid4
from qdrant_client.http import models
from app.core.database import qdrant_client
from app.core.config import settings

COLLECTION_NAME = settings.QDRANT_COLLECTION_NAME

async def ensure_collection():
    if not await qdrant_client.collection_exists(COLLECTION_NAME):
        await qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=qdrant_client.get_fastembed_vector_params(),
        )

    # ensure index exists for filtering
    await qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="user_id",
        field_schema="keyword"
    )

async def ingest_document(user_id: str, doc_id: str, title: str, content: str, metadata: dict):
    await ensure_collection()

    # embed the full content, store title and metadata alongside
    point = models.Document(
        page_content=content,
        metadata={
            "doc_id": doc_id,
            "title": title,
            "user_id": user_id,
            "type": "knowledge_doc",
            **metadata,
        },
        id=str(uuid4()),
    )

    await qdrant_client.upsert(
        collection_name=COLLECTION_NAME,
        points=[point],
    )

async def search_knowledge(user_id: str, query: str, limit: int = 5, threshold: float = 0.7) -> dict:
    # ensure collection exists
    if not await qdrant_client.collection_exists(COLLECTION_NAME):
        return {"results": [], "confidence": 0.0, "escalate": True}

    # embed the search query locally using fastembed
    embeddings = list(qdrant_client._embed_documents(
        documents=[query],
        embedding_model_name=qdrant_client.embedding_model_name,
    ))
    if not embeddings or not embeddings[0]:
        return {"results": [], "confidence": 0.0, "escalate": True}

    query_vector = embeddings[0][1]

    search_result = await qdrant_client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
        using="fast-bge-small-en",
        query_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="user_id",
                    match=models.MatchValue(value=user_id)
                )
            ]
        ),
        limit=limit,
    )

    hits = search_result.points
    # filter by threshold
    hits = [h for h in hits if h.score >= threshold]

    results = []
    for h in hits:
        results.append({
            "content": h.payload.get("page_content", ""),
            "title": h.payload.get("title", ""),
            "score": h.score,
            "metadata": {k: v for k, v in h.payload.items() if k not in ("page_content", "user_id", "type")},
        })

    # confidence is the top score, or 0 if no results
    confidence = results[0]["score"] if results else 0.0
    escalate = confidence < threshold

    return {"results": results, "confidence": confidence, "escalate": escalate}
