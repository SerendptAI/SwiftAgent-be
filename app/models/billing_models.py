from typing import List, Optional
from pydantic import BaseModel

class SavedCard(BaseModel):
    brand: str
    last4: str

class SubscribeRequest(BaseModel):
    plan_name: str # "yellow", "purple", "orange"

class BillingDetailsResponse(BaseModel):
    present_plan: str
    saved_cards: List[SavedCard]
