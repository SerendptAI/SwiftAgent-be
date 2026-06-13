from typing import List, Optional
from pydantic import BaseModel, field_validator
from datetime import datetime
import re


class AttachmentMeta(BaseModel):
    """Metadata for a file attachment (stored in DB, not the file itself)."""
    filename: str
    content_type: str
    size: int  # bytes


class TicketMessage(BaseModel):
    direction: str  # "inbound" (customer) | "outbound" (company)
    body_text: str
    body_html: Optional[str] = None
    sender_email: str
    message_id: Optional[str] = None  # SMTP Message-ID for threading
    timestamp: Optional[datetime] = None
    seen: bool = False
    attachments: List[AttachmentMeta] = []


class EmailTicket(BaseModel):
    id: str
    company_id: str
    customer_email: str
    customer_name: Optional[str] = None
    subject: str
    status: str = "pending"  # pending | awaiting_customer | follow_up | resolved
    resolve_token: str
    messages: List[TicketMessage] = []
    unseen_count: int = 0
    avatar: Optional[str] = None
    # originating chat context
    chat_session_id: Optional[str] = None
    chat_summary: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class EmailReplyRequest(BaseModel):
    body_text: str
    body_html: Optional[str] = None

    @field_validator("body_text")
    @classmethod
    def body_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Reply body cannot be empty")
        return v.strip()


class TicketStatusUpdate(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"pending", "awaiting_customer", "follow_up", "resolved"}
        if v not in allowed:
            raise ValueError(f"Status must be one of {allowed}")
        return v


class EmailSlugCheck(BaseModel):
    slug: str

    @field_validator("slug")
    @classmethod
    def validate_slug_format(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9\-]{1,28}[a-z0-9]$", v):
            raise ValueError(
                "Slug must be 3-30 characters, lowercase alphanumeric and hyphens, "
                "no leading/trailing hyphens"
            )
        if "--" in v:
            raise ValueError("Slug cannot contain consecutive hyphens")
        return v


class EmailSlugCheckResponse(BaseModel):
    available: bool
    suggestion: Optional[str] = None


class EmailSlugUpdate(BaseModel):
    email_slug: str

    @field_validator("email_slug")
    @classmethod
    def validate_slug_format(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9\-]{1,28}[a-z0-9]$", v):
            raise ValueError(
                "Slug must be 3-30 characters, lowercase alphanumeric and hyphens, "
                "no leading/trailing hyphens"
            )
        if "--" in v:
            raise ValueError("Slug cannot contain consecutive hyphens")
        return v


class EmailTicketResponse(BaseModel):
    id: str
    company_id: str
    customer_email: str
    customer_name: Optional[str] = None
    subject: str
    status: str
    messages: List[TicketMessage] = []
    unseen_count: int = 0
    avatar: Optional[str] = None
    chat_session_id: Optional[str] = None
    chat_summary: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    attributed_chat: Optional[dict] = None


class EmailTicketSummary(BaseModel):
    id: str
    company_id: str
    customer_email: str
    customer_name: Optional[str] = None
    subject: str
    status: str
    message_count: int = 0
    unseen_count: int = 0
    avatar: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    preview_message: Optional[str] = None

class TestDispatchRequest(BaseModel):
    company_id: str
    recipients: List[str]
