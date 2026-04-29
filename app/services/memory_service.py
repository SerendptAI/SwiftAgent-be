"""
Session-scoped memory for chat conversations.

Users are anonymous - they are clients' end users who chat via widget.
Memory is only for the current session to maintain context during conversation.
"""

from typing import List, Optional, Dict
import logging
from datetime import datetime, timedelta, timezone

from google import genai

from app.core.config import settings
from app.core.database import db
from app.models.memory_models import (
    EpisodeSummary,
    WorkingMemory,
    MemoryContext,
)


def _get_gemini_client() -> genai.Client:
    """Get Gemini client for memory operations."""
    return genai.Client(api_key=settings.GEMINI_API_KEY)

logger = logging.getLogger(__name__)

WORKING_MEMORY_PREFIX = "working_memory:"
WORKING_MEMORY_TTL = settings.WORKING_MEMORY_TTL_SECONDS


async def ensure_memory_indexes():
    await db.episodic_episodes.create_index("session_id")
    await db.episodic_episodes.create_index("company_id")
    await db.episodic_episodes.create_index("user_id")
    await db.episodic_episodes.create_index([("company_id", 1), ("created_at", -1)])

    await db.episodic_events.create_index("session_id")
    await db.episodic_events.create_index([("session_id", 1), ("timestamp", -1)])


async def save_episode(episode: EpisodeSummary) -> str:
    episode.updated_at = datetime.now(timezone.utc)
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


async def log_event(event):
    await db.episodic_events.insert_one(event.model_dump())


async def get_session_events(session_id: str):
    cursor = db.episodic_events.find({"session_id": session_id}).sort("timestamp", -1)
    events = []
    async for doc in cursor:
        doc.pop("_id", None)
        events.append(doc)
    return events


# Working memory (session-scoped, stored in in-memory dict instead of Redis for now)

_working_memory_cache: Dict[str, str] = {}


async def save_working_memory(memory: WorkingMemory):
    memory.updated_at = datetime.now(timezone.utc)
    key = f"{WORKING_MEMORY_PREFIX}{memory.session_id}"
    data = memory.model_dump_json()
    _working_memory_cache[key] = data


async def get_working_memory(session_id: str) -> Optional[WorkingMemory]:
    key = f"{WORKING_MEMORY_PREFIX}{session_id}"
    data = _working_memory_cache.get(key)
    if data:
        return WorkingMemory.model_validate_json(data)
    return None


async def delete_working_memory(session_id: str):
    key = f"{WORKING_MEMORY_PREFIX}{session_id}"
    _working_memory_cache.pop(key, None)


async def load_memory_context(
    company_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> MemoryContext:
    context = MemoryContext()

    if session_id:
        working_mem = await get_working_memory(session_id)
        context.working_memory = working_mem

    return context


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

    return "\n".join(parts) if parts else ""


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

    episode = None
    try:
        gemini_client = _get_gemini_client()
        response = await gemini_client.aio.models.generate_content(
            model=settings.GEMINI_MODEL,
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


async def cleanup_old_memories() -> dict:
    """Remove memories older than retention policy."""
    from app.core.config import settings

    deleted = {"episodes": 0}

    cut_off = datetime.now(timezone.utc) - timedelta(days=settings.EPISODIC_RETENTION_DAYS)
    result = await db.episodic_episodes.delete_many({"created_at": {"$lt": cut_off}})
    deleted["episodes"] = result.deleted_count

    return deleted
