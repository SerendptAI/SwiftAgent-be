from typing import List, Optional, Dict, Any
from pydantic import BaseModel

class SavedCard(BaseModel):
    brand: str
    last4: str

class CheckoutSessionRequest(BaseModel):
    company_id: str
    tier: str # "basic", "pro", "enterprise"
    user_timezone: str = "" # e.g. "Africa/Lagos", "America/New_York"

class CheckoutSessionResponse(BaseModel):
    checkout_url: str

class BillingDetailsResponse(BaseModel):
    subscription_tier: Optional[str]
    subscription_status: str
    billing_provider: Optional[str]
    saved_cards: List[SavedCard] = []

class WebhookResponse(BaseModel):
    received: bool
