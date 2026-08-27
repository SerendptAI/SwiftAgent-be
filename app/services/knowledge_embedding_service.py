import uuid
from collections.abc import Sequence

from qdrant_client.http import models

from app.core.config import settings
from app.core.database import qdrant_client
from app.services.knowledge_service import _get_gemini_client, ensure_collection

COLLECTION_NAME = settings.QDRANT_COLLECTION_NAME
_POINT_NAMESPACE = uuid.UUID("da849ca1-926b-4974-ad2f-1ff238696ade")


def chunk_text(text: str, chunk_size: int = 700, overlap: int = 100) -> list[str]:
    """Split normalized text into bounded word chunks with deterministic overlap."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap must be non-negative and smaller than chunk_size")
    words = (text or "").split()
    if not words:
        return []
    chunks: list[str] = []
    step = chunk_size - overlap
    for start in range(0, len(words), step):
        chunk_words = words[start : start + chunk_size]
        if not chunk_words:
            break
        chunks.append(" ".join(chunk_words))
        if start + chunk_size >= len(words):
            break
    return chunks


def deterministic_point_id(
    company_id: str, page_id: str, content_hash: str, chunk_index: int
) -> str:
    identity = f"{company_id}:{page_id}:{content_hash}:{chunk_index}"
    return str(uuid.uuid5(_POINT_NAMESPACE, identity))


async def _embed_chunks(chunks: Sequence[str], batch_size: int = 50) -> list[list[float]]:
    client = _get_gemini_client()
    vectors: list[list[float]] = []
    for start in range(0, len(chunks), batch_size):
        batch = list(chunks[start : start + batch_size])
        response = await client.aio.models.embed_content(
            model=settings.EMBEDDING_MODEL,
            contents=batch,
            config={"task_type": "RETRIEVAL_DOCUMENT"},
        )
        embeddings = response.embeddings or []
        if len(embeddings) != len(batch):
            raise RuntimeError("embedding provider returned an unexpected vector count")
        for embedding in embeddings:
            if embedding.values is None:
                raise RuntimeError("embedding provider returned an empty vector")
            vectors.append(list(embedding.values))
    return vectors


async def replace_page_vectors(
    *,
    company_id: str,
    user_id: str,
    page_id: str,
    source_id: str,
    url: str,
    title: str,
    content: str,
    content_hash: str,
    language: str = "en",
    chunk_size: int = 700,
    overlap: int = 100,
) -> int:
    """Upsert a complete new page version, then remove stale page versions."""
    chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        raise ValueError("page content produced no indexable chunks")

    await ensure_collection()
    vectors = await _embed_chunks(chunks)
    points = [
        models.PointStruct(
            id=deterministic_point_id(company_id, page_id, content_hash, index),
            vector=vector,
            payload={
                "user_id": user_id,
                "company_id": company_id,
                "source_id": source_id,
                "page_id": page_id,
                "chunk_id": f"{page_id}:{index}",
                "chunk_index": index,
                "content_hash": content_hash,
                "url": url,
                "title": title,
                "language": language,
                "type": "crawled_page",
                "page_content": chunk,
            },
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
    ]

    await qdrant_client.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)
    await qdrant_client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="company_id", match=models.MatchValue(value=company_id)
                    ),
                    models.FieldCondition(key="page_id", match=models.MatchValue(value=page_id)),
                ],
                must_not=[
                    models.FieldCondition(
                        key="content_hash", match=models.MatchValue(value=content_hash)
                    )
                ],
            )
        ),
        wait=True,
    )
    return len(points)


async def delete_page_vectors(company_id: str, page_id: str) -> None:
    await qdrant_client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="company_id", match=models.MatchValue(value=company_id)
                    ),
                    models.FieldCondition(key="page_id", match=models.MatchValue(value=page_id)),
                ]
            )
        ),
        wait=True,
    )
