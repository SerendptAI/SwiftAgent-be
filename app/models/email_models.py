from typing import List, Optional
from pydantic import BaseModel, field_validator
from datetime import datetime
import re


class AttachmentMeta(BaseModel):
    """Metadata for a file attachment (stored in DB, not the file itself)."""
    filename: str
    content_type: Optional[str] = None
    size: Optional[int] = None  # bytes


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
    # pending | in_progress | awaiting_customer | follow_up | open | resolved
    status: str = "pending"
    priority: str = "medium"  # low | medium | high | urgent
    resolve_token: str
    messages: List[TicketMessage] = []
    unseen_count: int = 0
    avatar: Optional[str] = None
    # originating chat context
    chat_session_id: Optional[str] = None
    chat_summary: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    # assignment / ownership
    assigned_to: Optional[str] = None
    assigned_by: Optional[str] = None
    assigned_at: Optional[datetime] = None
    # response + SLA tracking
    first_response_at: Optional[datetime] = None
    sla_policy: Optional[dict] = None
    sla_first_response_deadline: Optional[datetime] = None
    sla_resolution_deadline: Optional[datetime] = None
    sla_breached: bool = False
    sla_breach_reason: Optional[str] = None
    # escalation (auto-escalated chats)
    escalation_reason: Optional[str] = None
    escalated_at: Optional[datetime] = None
    handoff_context: Optional[dict] = None
    # audit trail
    activity_log: List[dict] = []


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
    priority: Optional[str] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"pending", "in_progress", "awaiting_customer", "follow_up", "open", "resolved"}
        if v not in allowed:
            raise ValueError(f"Status must be one of {allowed}")
        return v

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        allowed = {"low", "medium", "high", "urgent"}
        if v not in allowed:
            raise ValueError(f"Priority must be one of {allowed}")
        return v


class TicketPriorityUpdate(BaseModel):
    priority: str = "medium"

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        allowed = {"low", "medium", "high", "urgent"}
        if v not in allowed:
            raise ValueError(f"Priority must be one of {allowed}")
        return v


class TicketAssignRequest(BaseModel):
    # None / null unassigns the ticket
    assignee_user_id: Optional[str] = None


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
    priority: str = "medium"
    messages: List[TicketMessage] = []
    unseen_count: int = 0
    avatar: Optional[str] = None
    chat_session_id: Optional[str] = None
    chat_summary: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    attributed_chat: Optional[dict] = None
    assigned_to: Optional[str] = None
    assigned_by: Optional[str] = None
    assigned_at: Optional[datetime] = None
    first_response_at: Optional[datetime] = None
    sla_policy: Optional[dict] = None
    sla_first_response_deadline: Optional[datetime] = None
    sla_resolution_deadline: Optional[datetime] = None
    sla_breached: bool = False
    sla_breach_reason: Optional[str] = None
    escalation_reason: Optional[str] = None
    escalated_at: Optional[datetime] = None
    handoff_context: Optional[dict] = None
    activity_log: List[dict] = []


class EmailTicketSummary(BaseModel):
    id: str
    company_id: str
    customer_email: str
    customer_name: Optional[str] = None
    subject: str
    status: str
    priority: str = "medium"
    message_count: int = 0
    unseen_count: int = 0
    avatar: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    preview_message: Optional[str] = None
    assigned_to: Optional[str] = None
    sla_breached: bool = False


class TestDispatchRequest(BaseModel):
    company_id: str
    recipients: List[str]


# ============================================================================
# AGENT COPILOT SCHEMAS
# ============================================================================


class CopilotSuggestRequest(BaseModel):
    tone: Optional[str] = "empathic_professional"  # empathic_professional | concise | technical | apologetic
    instruction: Optional[str] = None  # Custom instruction from the human agent


class CopilotActionSuggestion(BaseModel):
    action_type: str  # status_change | priority_change | internal_note | navigation_guide | api_action
    label: str
    confidence: float = 1.0
    parameters: dict = {}
    reason: Optional[str] = None


class CopilotSuggestResponse(BaseModel):
    suggested_reply: str
    suggested_subject: Optional[str] = None
    customer_sentiment: str = "neutral"
    sources_used: List[str] = []
    suggested_actions: List[CopilotActionSuggestion] = []
    reasoning: Optional[str] = None


class CopilotExecuteActionRequest(BaseModel):
    action_type: str  # status_change | priority_change | internal_note
    parameters: dict = {}
    reason: Optional[str] = None
