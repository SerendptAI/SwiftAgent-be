from typing import List, Dict, Any
from uuid import uuid4
from qdrant_client.http import models
from app.core.database import qdrant_client
from app.core.config import settings
from google import genai

COLLECTION_NAME = settings.QDRANT_COLLECTION_NAME

def _get_gemini_client() -> genai.Client:
    return genai.Client(api_key=settings.GEMINI_API_KEY)

async def ensure_collection():
    if not await qdrant_client.collection_exists(COLLECTION_NAME):
        await qdrant_client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=models.VectorParams(
                size=3072,  # Gemini text-embedding-001 dimension size
                distance=models.Distance.COSINE
            ),
        )

    # ensure index exists for filtering
    await qdrant_client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="user_id",
        field_schema="keyword"
    )

async def ingest_document(user_id: str, doc_id: str, title: str, content: str, metadata: dict):
    await ensure_collection()

    gemini_client = _get_gemini_client()
    
    # embed using gemini
    response = await gemini_client.aio.models.embed_content(
        model='gemini-embedding-001',
        contents=content,
        config={"task_type": "RETRIEVAL_DOCUMENT"}
    )
    
    vector = response.embeddings[0].values

    await qdrant_client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            models.PointStruct(
                id=str(uuid4()),
                vector=vector,
                payload={
                    "doc_id": doc_id,
                    "title": title,
                    "user_id": user_id,
                    "type": "knowledge_doc",
                    "page_content": content,
                    **metadata,
                }
            )
        ]
    )

async def search_knowledge(user_id: str, query: str, limit: int = 5, threshold: float = 0.7) -> dict:
    # ensure collection exists
    if not await qdrant_client.collection_exists(COLLECTION_NAME):
        return {"results": [], "confidence": 0.0, "escalate": True}

    gemini_client = _get_gemini_client()

    try:
        # embed the search query
        response = await gemini_client.aio.models.embed_content(
            model='gemini-embedding-001',
            contents=query,
            config={"task_type": "RETRIEVAL_QUERY"}
        )
        query_vector = response.embeddings[0].values
    except Exception as e:
        print(f"Embedding error: {e}")
        return {"results": [], "confidence": 0.0, "escalate": True}

    search_result = await qdrant_client.query_points(
        collection_name=COLLECTION_NAME,
        query=query_vector,
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
