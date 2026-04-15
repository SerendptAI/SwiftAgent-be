from pydantic import BaseModel, EmailStr, field_validator
from typing import Literal, Optional


# ── existing ──────────────────────────────────────────────────────────────────

class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UserProfileUpdate(BaseModel):
    personal_email: Optional[str] = None
    personal_phone: Optional[str] = None


class ReferralRequest(BaseModel):
    code: str


# ── credential auth ───────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    full_name: str
    email: EmailStr
    password: str
    confirm_password: str

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("full_name cannot be blank")
        return v.strip().title()

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class SignupResponse(BaseModel):
    message: str
    email: str


class CredentialLoginRequest(BaseModel):
    email: EmailStr
    password: str


class CredentialLoginResponse(BaseModel):
    message: str
    email: str
    otp_required: bool
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: Optional[str] = "bearer"


class OTPVerifyRequest(BaseModel):
    email: EmailStr
    otp_code: str


class OTPResendRequest(BaseModel):
    email: EmailStr
    purpose: Literal["signup", "login", "password_reset"]


class ForgotPasswordInitRequest(BaseModel):
    email: EmailStr


class ForgotPasswordVerifyRequest(BaseModel):
    email: EmailStr
    otp_code: str


class ForgotPasswordResetRequest(BaseModel):
    email: EmailStr
    new_password: str
    confirm_new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v
