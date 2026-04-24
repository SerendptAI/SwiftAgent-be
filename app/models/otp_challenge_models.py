"""
OTP Challenge models — push-based OTP relay for stroll 2FA encounters.

When the stroll agent hits a 2FA/OTP page during automated login, it creates
an OTPChallenge, pushes a notification to the company admin's mobile app,
and waits for the admin to supply the OTP value.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DeviceToken(BaseModel):
    """FCM push token registered from the mobile app."""
    user_id: str
    device_token: str
    device_name: str = ""
    platform: str = "android"          # "android" | "ios"
    registered_at: Optional[datetime] = None


class OTPChallenge(BaseModel):
    """A pending OTP request from the stroll agent to a human."""
    id: str
    company_id: str
    user_id: str                        # admin who should respond
    status: str = "pending"             # "pending" | "answered" | "expired"
    otp_value: Optional[str] = None     # filled by the human via mobile app
    screenshot_url: Optional[str] = None  # screenshot of the OTP page (Cloudinary)
    login_url: str = ""                 # the dashboard URL being logged into
    created_at: Optional[datetime] = None
    answered_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class RegisterDeviceRequest(BaseModel):
    """Mobile app request to register a push notification token."""
    device_token: str
    device_name: str = ""
    platform: str = Field(default="android", pattern="^(android|ios)$")


class ChallengeResponseRequest(BaseModel):
    """Mobile app request to respond to an OTP challenge."""
    otp_value: str = Field(..., min_length=1, max_length=20)
