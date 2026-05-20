from datetime import datetime, timezone
from enum import StrEnum

from typing import Any
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class MemoryType(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    WORKING = "working"


class EpisodeSummary(BaseModel):
    session_id: str
    company_id: str
    user_id: str | None = None
    summary: str
    topics: list[str] = []
    tools_used: list[str] = []
    outcome: str = "unknown"
    message_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EpisodicEvent(BaseModel):
    session_id: str
    event_type: str
    event_data: dict = {}
    timestamp: datetime = Field(default_factory=utc_now)


class SemanticMemory(BaseModel):
    id: str | None = None
    user_id: str
    company_id: str
    memory_type: str = "user_profile"
    content: str
    embedding: list[float] | None = None
    importance: float = 0.5
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class WorkingMemory(BaseModel):
    session_id: str
    identified_user: bool = False
    user_name: str | None = None
    user_email: str | None = None
    current_issue: str | None = None
    issue_resolved: bool = False
    pending_actions: list[str] = []
    context_window: list[dict[str, Any]] = []
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MemoryContext(BaseModel):
    episodic_memories: list[EpisodeSummary] = []
    semantic_memories: list[SemanticMemory] = []
    working_memory: WorkingMemory | None = None
    recent_conversations: list[str] = []


class MemorySummaryRequest(BaseModel):
    session_id: str
    company_id: str
    user_id: str | None = None
    messages: list[dict[str, Any]]


class MemoryExtractFactsRequest(BaseModel):
    user_id: str
    company_id: str
    messages: list[dict[str, Any]]
