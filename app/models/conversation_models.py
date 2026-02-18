from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

class ConversationMessage(BaseModel):
    role: str
    content: str
    timestamp: Optional[datetime] = None

class ConversationCreate(BaseModel):
    messages: List[ConversationMessage]

class ConversationResponse(BaseModel):
    id: str
    user_id: str
    messages: List[ConversationMessage]
    created_at: datetime
    updated_at: datetime
