from pydantic import BaseModel
from typing import Optional

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class UserProfileUpdate(BaseModel):
    personal_email: Optional[str] = None
    personal_phone: Optional[str] = None
