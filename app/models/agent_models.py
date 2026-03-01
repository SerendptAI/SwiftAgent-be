from typing import Optional
from pydantic import BaseModel


class WidgetChatRequest(BaseModel):
    session_id: str
    message: str


class WidgetChatResponse(BaseModel):
    session_id: str
    reply: str
    sources: list[dict] = []
    blockchain_data: Optional[dict] = None
