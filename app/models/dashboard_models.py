from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

class StatCard(BaseModel):
    today: int = 0
    percent_change: float = 0.0
    last_7_days_up: int = 0
    last_7_days_down: int = 0

class ChatStatCard(BaseModel):
    today: int = 0
    pending: int = 0
    last_7_days_up: int = 0
    last_7_days_down: int = 0

class DashboardStats(BaseModel):
    visitors: StatCard = StatCard()
    chats: ChatStatCard = ChatStatCard()
    calls: StatCard = StatCard()
    documents: StatCard = StatCard()
    scrapes: StatCard = StatCard()

class VisitorRecord(BaseModel):
    id: str
    company_id: str
    visitor_id: str
    country_code: Optional[str] = None
    duration_seconds: int = 0
    timestamp: datetime

class WidgetConfig(BaseModel):
    company_id: str
    embed_code: str
