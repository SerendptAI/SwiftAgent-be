from pydantic import BaseModel, EmailStr
from typing import Optional


# ── existing ─────────────────────────────────────────────

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class UserProfileUpdate(BaseModel):
    name: Optional[str] = None
    picture: Optional[str] = None
    personal_email: Optional[EmailStr] = None
    personal_phone: Optional[str] = None

class UserNameUpdate(BaseModel):
    name: str

class UserSecurityUpdate(BaseModel):
    backup_email: Optional[EmailStr] = None
    access_code: Optional[str] = None

class ReferralRequest(BaseModel):
    code: str


# ── unified passwordless flow ────────────────────────────

class OTPSendRequest(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    
    # Allows a hint on whether they click "Sign In" or "Sign Up"
    # just to customize the email subject, defaults to login.
    is_signup: bool = False


class OTPVerifyRequest(BaseModel):
    email: EmailStr
    otp_code: str


class LoginResponse(BaseModel):
    message: str
    email: str
    otp_required: bool
    is_new_user: bool = False
    
    # Present if otp_required = False
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[str] = None


# explicit registration flow 

class RegistrationInterestRequest(BaseModel):
    company_name: str
    company_email: EmailStr
    company_description: str
    customer_size: str
    company_website: Optional[str] = None

class RegistrationInterestResponse(BaseModel):
    status: str
    message: str
