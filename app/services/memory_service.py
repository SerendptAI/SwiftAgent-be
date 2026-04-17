from typing import List, Optional, Dict, Any
import logging
from uuid import uuid4
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from app.core.config import settings
from app.core.database import db, qdrant_client, redis_client
from app.models.memory_models import (
    EpisodeSummary,
    EpisodicEvent,
    SemanticMemory,
    WorkingMemory,
    MemoryContext,
)
from app.services.knowledge_service import _get_gemini_client, ensure_collection

logger = logging.getLogger(__name__)

MEMORY_COLLECTION_NAME = "semantic_memory"
EMBEDDING_DIMENSION = 3072


async def ensure_memory_collection():
    if not await qdrant_client.collection_exists(MEMORY_COLLECTION_NAME):
        await qdrant_client.create_collection(
            collection_name=MEMORY_COLLECTION_NAME,
            vectors_config=models.VectorParams(
                size=EMBEDDING_DIMENSION,
                distance=models.Distance.COSINE,
            ),
        )

    await qdrant_client.create_payload_index(
        collection_name=MEMORY_COLLECTION_NAME, field_name="user_id", field_schema="keyword"
    )
    await qdrant_client.create_payload_index(
        collection_name=MEMORY_COLLECTION_NAME, field_name="company_id", field_schema="keyword"
    )
    await qdrant_client.create_payload_index(
        collection_name=MEMORY_COLLECTION_NAME, field_name="memory_type", field_schema="keyword"
    )


async def ensure_memory_indexes():
    await db.episodic_episodes.create_index("session_id")
    await db.episodic_episodes.create_index("company_id")
    await db.episodic_episodes.create_index("user_id")
    await db.episodic_episodes.create_index([("company_id", 1), ("created_at", -1)])

    await db.episodic_events.create_index("session_id")
    await db.episodic_events.create_index([("session_id", 1), ("timestamp", -1)])


async def save_episode(episode: EpisodeSummary) -> str:
    episode.updated_at = datetime.utcnow()
    result = await db.episodic_episodes.update_one(
        {"session_id": episode.session_id},
        {"$set": episode.model_dump()},
        upsert=True,
    )
    return str(result.upserted_id) if result.upserted_id else episode.session_id


async def get_episode(session_id: str) -> Optional[EpisodeSummary]:
    doc = await db.episodic_episodes.find_one({"session_id": session_id})
    if doc:
        doc.pop("_id", None)
        return EpisodeSummary(**doc)
    return None


async def get_recent_episodes(
    company_id: str, user_id: Optional[str] = None, limit: int = 5
) -> List[EpisodeSummary]:
    query = {"company_id": company_id}
    if user_id:
        query["user_id"] = user_id

    cursor = db.episodic_episodes.find(query).sort("created_at", -1).limit(limit)
    episodes = []
    async for doc in cursor:
        doc.pop("_id", None)
        episodes.append(EpisodeSummary(**doc))
    return episodes


async def log_event(event: EpisodicEvent):
    await db.episodic_events.insert_one(event.model_dump())


async def get_session_events(session_id: str) -> List[EpisodicEvent]:
    cursor = db.episodic_events.find({"session_id": session_id}).sort("timestamp", -1)
    events = []
    async for doc in cursor:
        doc.pop("_id", None)
        events.append(EpisodicEvent(**doc))
    return events


async def save_semantic_memory(memory: SemanticMemory) -> str:
    await ensure_memory_collection()

    gemini_client = _get_gemini_client()
    response = await gemini_client.aio.models.embed_content(
        model="gemini-embedding-001",
        contents=memory.content,
        config={"task_type": "RETRIEVAL_DOCUMENT"},
    )
    embedding = response.embeddings[0].values

    memory_id = memory.id or str(uuid4())
    memory.id = memory_id
    memory.embedding = embedding
    memory.updated_at = datetime.utcnow()

    await qdrant_client.upsert(
        collection_name=MEMORY_COLLECTION_NAME,
        points=[
            models.PointStruct(
                id=memory_id,
                vector=embedding,
                payload=memory.model_dump(exclude={"embedding"}),
            )
        ],
    )
    return memory_id


async def get_user_memories(
    user_id: str,
    company_id: str,
    query: str = "",
    limit: int = 5,
) -> List[SemanticMemory]:
    await ensure_memory_collection()

    if not query:
        must_conditions = [
            models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
            models.FieldCondition(key="company_id", match=models.MatchValue(value=company_id)),
        ]
        search_result = await qdrant_client.query_points(
            collection_name=MEMORY_COLLECTION_NAME,
            query_filter=models.Filter(must=must_conditions),
            limit=limit,
        )
    else:
        gemini_client = _get_gemini_client()
        response = await gemini_client.aio.models.embed_content(
            model="gemini-embedding-001",
            contents=query,
            config={"task_type": "RETRIEVAL_QUERY"},
        )
        query_vector = response.embeddings[0].values

        must_conditions = [
            models.FieldCondition(key="user_id", match=models.MatchValue(value=user_id)),
            models.FieldCondition(key="company_id", match=models.MatchValue(value=company_id)),
        ]
        search_result = await qdrant_client.query_points(
            collection_name=MEMORY_COLLECTION_NAME,
            query=query_vector,
            query_filter=models.Filter(must=must_conditions),
            limit=limit,
        )

    memories = []
    for point in search_result.points:
        payload = point.payload
        payload["id"] = point.id
        memories.append(SemanticMemory(**payload))
    return memories


from app.core.database import db, qdrant_client, redis_client

WORKING_MEMORY_PREFIX = "working_memory:"
WORKING_MEMORY_TTL = settings.WORKING_MEMORY_TTL_SECONDS


async def save_working_memory(memory: WorkingMemory):
    memory.updated_at = datetime.utcnow()
    key = f"{WORKING_MEMORY_PREFIX}{memory.session_id}"
    data = memory.model_dump_json()
    await redis_client.setex(key, WORKING_MEMORY_TTL, data)


async def get_working_memory(session_id: str) -> Optional[WorkingMemory]:
    key = f"{WORKING_MEMORY_PREFIX}{session_id}"
    data = await redis_client.get(key)
    if data:
        return WorkingMemory.model_validate_json(data)
    return None


async def delete_working_memory(session_id: str):
    key = f"{WORKING_MEMORY_PREFIX}{session_id}"
    await redis_client.delete(key)


async def load_memory_context(
    company_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> MemoryContext:
    context = MemoryContext()

    if session_id:
        working_mem = await get_working_memory(session_id)
        context.working_memory = working_mem

        recent_eps = await get_recent_episodes(company_id, user_id, limit=3)
        context.episodic_memories = recent_eps

    if user_id and company_id:
        semantic_mems = await get_user_memories(user_id, company_id, limit=10)
        context.semantic_memories = semantic_mems

    return context


async def extract_and_store_facts(
    user_id: str,
    company_id: str,
    messages: List[Dict[str, str]],
) -> List[str]:
    if not messages:
        return []

    conversation_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages
    )

    # Enhanced fact extraction with user entity detection
    fact_extraction_prompt = f"""Extract key facts and user identity from this conversation. Focus on:

1. USER IDENTITY (extract these fields):
   - name: User's name if mentioned
   - email: User's email address if provided  
   - wallet: Any wallet address (ETH/ BTC) mentioned

2. USER PREFERENCES:
   - Tone preference (formal/casual)
   - Language preference
   - Communication style

3. IMPORTANT INFORMATION:
   - Project names or ticket IDs mentioned
   - Issues they're experiencing
   - Topics of interest

Return a JSON object with these fields:
- "user_identity": {{"name": "...", "email": "...", "wallet": "..."}} or null
- "preferences": array of preference facts
- "other_facts": array of other important facts

Conversation:
{conversation_text}"""

    try:
        gemini_client = _get_gemini_client()
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=fact_extraction_prompt,
            config={"response_mime_type": "application/json"},
        )

        import json

        result = json.loads(response.text)
        if not isinstance(result, dict):
            result = {}

        stored_ids = []

        # Store user identity if found
        user_identity = result.get("user_identity")
        if user_identity:
            for key, value in user_identity.items():
                if value and isinstance(value, str) and len(value) > 2:
                    memory = SemanticMemory(
                        user_id=user_id,
                        company_id=company_id,
                        memory_type=f"identity_{key}",
                        content=f"{key}: {value}",
                        importance=0.9,
                    )
                    mem_id = await save_semantic_memory(memory)
                    stored_ids.append(mem_id)

        # Store preferences
        for fact in result.get("preferences", []):
            if isinstance(fact, str) and len(fact) > 5:
                memory = SemanticMemory(
                    user_id=user_id,
                    company_id=company_id,
                    memory_type="preference",
                    content=fact,
                    importance=0.7,
                )
                mem_id = await save_semantic_memory(memory)
                stored_ids.append(mem_id)

        # Store other facts
        for fact in result.get("other_facts", []):
            if isinstance(fact, str) and len(fact) > 10:
                memory = SemanticMemory(
                    user_id=user_id,
                    company_id=company_id,
                    memory_type="learned_fact",
                    content=fact,
                    importance=0.6,
                )
                mem_id = await save_semantic_memory(memory)
                stored_ids.append(mem_id)

        return stored_ids
    except Exception as e:
        logger.error(f"Failed to extract and store facts: {e}")
        return []


async def generate_session_summary(
    session_id: str,
    company_id: str,
    user_id: Optional[str],
    messages: List[Dict[str, str]],
    tools_used: List[str],
    outcome: str = "unknown",
) -> EpisodeSummary:
    if not messages:
        return EpisodeSummary(
            session_id=session_id,
            company_id=company_id,
            user_id=user_id,
            summary="Empty conversation",
        )

    conversation_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')[:200]}" for m in messages[-20:]
    )

    summary_prompt = f"""Summarize this conversation concisely. Return a JSON object with:
- summary: 2-3 sentence summary of what was discussed
- topics: array of main topics (max 5)
- outcome: one of resolved, escalated, abandoned, or pending

Conversation:
{conversation_text}"""

    try:
        gemini_client = _get_gemini_client()
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=summary_prompt,
            config={"response_mime_type": "application/json"},
        )

        import json

        result = json.loads(response.text)

        episode = EpisodeSummary(
            session_id=session_id,
            company_id=company_id,
            user_id=user_id,
            summary=result.get("summary", "Conversation ended"),
            topics=result.get("topics", [])[:5],
            tools_used=tools_used,
            outcome=outcome,
            message_count=len(messages),
        )
    except Exception as e:
        logger.error(f"Failed to generate session summary: {e}")
        episode = EpisodeSummary(
            session_id=session_id,
            company_id=company_id,
            user_id=user_id,
            summary="Conversation ended",
            tools_used=tools_used,
            outcome=outcome,
            message_count=len(messages),
        )

    await save_episode(episode)
    return episode


def format_memory_context(context: MemoryContext) -> str:
    parts = []

    if context.working_memory:
        wm = context.working_memory
        parts.append("Current Session:")
        if wm.user_name:
            parts.append(f"- User: {wm.user_name}")
        if wm.current_issue:
            parts.append(f"- Current issue: {wm.current_issue}")
        if wm.issue_resolved:
            parts.append("- Issue resolved: yes")

    if context.semantic_memories:
        parts.append("\nLearned about user:")
        for mem in context.semantic_memories[:5]:
            parts.append(f"- {mem.content}")

    if context.episodic_memories:
        parts.append("\nPrevious conversations:")
        for ep in context.episodic_memories[:3]:
            parts.append(f"- {ep.summary} ({ep.outcome})")

    return "\n".join(parts) if parts else ""


async def cleanup_old_memories() -> dict:
    """Remove memories older than retention policy."""
    from app.core.config import settings
    from datetime import timedelta

    deleted = {"episodes": 0, "semantic": 0}

    # Cleanup episodic memories
    cut_off = datetime.utcnow() - timedelta(days=settings.EPISODIC_RETENTION_DAYS)
    result = await db.episodic_episodes.delete_many({"created_at": {"$lt": cut_off}})
    deleted["episodes"] = result.deleted_count

    return deleted


async def identify_user_from_conversation(
    messages: List[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    """Auto-detect user identity from conversation messages."""
    if not messages:
        return None

    conversation_text = "\n".join(
        f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages[-20:]
    )

    extraction_prompt = f"""Extract user contact information from this conversation. Look for:
- Email addresses (user@example.com)
- Wallet addresses (0x... or bc1...)
- Names mentioned as "my name is..."

Return JSON with found fields:
{{"name": "...", "email": "...", "wallet": "..."}}

Only include fields you find with high confidence.

Conversation:
{conversation_text}"""

    try:
        gemini_client = _get_gemini_client()
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=extraction_prompt,
            config={"response_mime_type": "application/json"},
        )

        import json

        result = json.loads(response.text)
        if isinstance(result, dict):
            # Filter out null values
            return {k: v for k, v in result.items() if v}
        return None
    except Exception as e:
        logger.error(f"Failed to identify user: {e}")
        return None


async def update_working_memory_from_identity(
    session_id: str,
    identity: Dict[str, str],
):
    """Update working memory with auto-detected user identity."""
    working = await get_working_memory(session_id)
    if working:
        if identity.get("name") and not working.user_name:
            working.user_name = identity["name"]
        if identity.get("email") and not working.user_email:
            working.user_email = identity["email"]
        await save_working_memory(working)
