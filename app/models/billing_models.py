from typing import List, Optional, Dict, Any
from pydantic import BaseModel

class SavedCard(BaseModel):
    brand: str
    last4: str

class CheckoutSessionRequest(BaseModel):
    company_id: str
    tier: str # "basic", "pro", "enterprise"

class CheckoutSessionResponse(BaseModel):
    checkout_url: str

class BillingDetailsResponse(BaseModel):
    subscription_tier: Optional[str]
    subscription_status: str
    billing_provider: Optional[str]
    saved_cards: List[SavedCard] = []
    subscription_started_at: Optional[str] = None

class WebhookResponse(BaseModel):
    received: bool
