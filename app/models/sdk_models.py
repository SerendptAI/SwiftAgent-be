"""
Pydantic models for the SDK endpoints.

Covers: API key management, SDK init, chat, conversation history & detail.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


# ── API Key Management (dashboard) ─────────────────────────────────────

class ApiKeyCreateRequest(BaseModel):
    label: str = Field("Default", max_length=64, description="Friendly name for the key.")


class ApiKeyCreateResponse(BaseModel):
    """Returned ONCE when a key is generated — the raw key is never shown again."""
    id: str
    key: str  # full raw key, e.g. "swa_live_Ab3x..."
    key_prefix: str
    label: str
    created_at: datetime


class ApiKeyListItem(BaseModel):
    id: str
    key_prefix: str
    label: str
    active: bool
    created_at: datetime
    last_used_at: Optional[datetime] = None


# ── SDK Init ───────────────────────────────────────────────────────────

class SdkInitRequest(BaseModel):
    email: EmailStr = Field(..., description="End-user email — used to identify and scope all data.")


class SdkInitCompany(BaseModel):
    name: str
    logo_url: Optional[str] = None
    suggested_ai_prompts: List[str] = []


class SdkInitUser(BaseModel):
    email: str
    is_new: bool


class SdkInitResponse(BaseModel):
    session_token: str
    company: SdkInitCompany
    user: SdkInitUser


# ── SDK Chat ───────────────────────────────────────────────────────────

class SdkAttachmentMeta(BaseModel):
    url: str
    type: str
    mime_type: str
    filename: str

class SdkChatRequest(BaseModel):
    session_id: str = Field(..., description="Client-generated session ID for this chat.")
    message: str = Field(..., min_length=1, description="The user's message.")
    attachments: Optional[List[SdkAttachmentMeta]] = None


# ── Conversation History (unified list) ────────────────────────────────

class SdkConversationItem(BaseModel):
    """A single item in the conversation history list — can be a chat or a ticket."""
    id: str
    type: Literal["chat", "ticket"]
    subject: Optional[str] = None  # null for chats, string for tickets
    last_message: Optional[str] = None
    resolved: bool = False
    message_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SdkConversationListResponse(BaseModel):
    items: List[SdkConversationItem]
    next_cursor: Optional[str] = None
    has_next: bool = False


# ── Conversation Detail ───────────────────────────────────────────────

class SdkMessageItem(BaseModel):
    role: str  # "user" | "assistant" | "system" | "inbound" | "outbound"
    content: str
    timestamp: Optional[str] = None


class SdkConversationDetail(BaseModel):
    """Full detail for a single conversation — chat or ticket."""
    id: str
    type: Literal["chat", "ticket"]
    subject: Optional[str] = None
    resolved: bool = False
    messages: List[SdkMessageItem] = []
    attributed_chat: Optional[List[SdkMessageItem]] = None  # chat context for escalated tickets
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
