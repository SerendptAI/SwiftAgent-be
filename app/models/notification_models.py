from datetime import datetime
from typing import Optional, Any
from pydantic import BaseModel, Field

class NotificationResponse(BaseModel):
    """A notification shown to a user (web or mobile)."""
    id: str
    user_id: str
    type: str  # e.g., 'ticket_reply', 'ticket_open', 'ticket_close', 'system_notification', 'plan_upgrade'
    title: str
    body: str
    data: Optional[dict[str, Any]] = None
    read: bool = False
    created_at: datetime

class NotificationListResponse(BaseModel):
    notifications: list[NotificationResponse]
    total: int
    unread_count: int

class VapidPublicKeyResponse(BaseModel):
    public_key: str
