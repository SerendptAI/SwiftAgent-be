"""
Mobile Router — API endpoints for the mobile companion app.

Endpoints:
- POST   /devices/register               — register Expo push token
- DELETE /devices/{device_token}          — unregister a device
- GET    /challenges                      — list pending OTP challenges
- GET    /challenges/{challenge_id}       — get challenge details
- POST   /challenges/{challenge_id}/respond — submit OTP value
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_user
from app.core.database import db
from app.models.otp_challenge_models import (
    ChallengeResponseRequest,
    RegisterDeviceRequest,
    TestPushNotificationRequest,
)
from app.services import otp_challenge_service
from app.services.push_notification_service import register_device, unregister_device, send_test_push

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Mobile"])


# Device Registration 

@router.post("/devices/register")
async def register_push_device(
    body: RegisterDeviceRequest,
    user: dict = Depends(get_current_user),
):
    """Register an Expo push token for push notifications."""
    user_id = user["user_id"]
    doc = await register_device(
        user_id=user_id,
        device_token=body.device_token,
        device_name=body.device_name,
        platform=body.platform,
    )
    return {"status": "registered", "device_token": doc["device_token"]}


@router.delete("/devices/{device_token}")
async def unregister_push_device(
    device_token: str,
    user: dict = Depends(get_current_user),
):
    """Unregister an Expo push token."""
    removed = await unregister_device(user["user_id"], device_token)
    if not removed:
        raise HTTPException(status_code=404, detail="Device token not found")
    return {"status": "unregistered"}


@router.post("/test-notification")
async def send_test_notification(
    body: TestPushNotificationRequest,
    user: dict = Depends(get_current_user),
):
    """Send a test push notification to all registered devices of the authenticated user."""
    result = await send_test_push(
        user_id=user["user_id"],
        title=body.title,
        body=body.body,
    )
    
    if result["total_devices"] == 0:
        raise HTTPException(
            status_code=404,
            detail="No registered devices found for this user. Register a device first."
        )
    
    return result


# OTP Challenges 

@router.get("/challenges")
async def list_challenges(user: dict = Depends(get_current_user)):
    """List all pending OTP challenges for the authenticated user."""
    challenges = await otp_challenge_service.get_pending_challenges(user["user_id"])
    return {"challenges": challenges}


@router.get("/challenges/{challenge_id}")
async def get_challenge(
    challenge_id: str,
    user: dict = Depends(get_current_user),
):
    """Get details of a specific OTP challenge."""
    challenge = await otp_challenge_service.get_challenge(challenge_id)
    if not challenge:
        raise HTTPException(status_code=404, detail="Challenge not found")

    # Ensure the user owns this challenge
    if challenge.get("user_id") != user["user_id"]:
        raise HTTPException(status_code=403, detail="Not authorized for this challenge")

    # Don't expose the OTP value in the response
    challenge.pop("otp_value", None)
    return challenge


@router.post("/challenges/{challenge_id}/respond")
async def respond_to_challenge(
    challenge_id: str,
    body: ChallengeResponseRequest,
    user: dict = Depends(get_current_user),
):
    """Submit an OTP value for a pending challenge."""
    success = await otp_challenge_service.respond_to_challenge(
        challenge_id=challenge_id,
        user_id=user["user_id"],
        otp_value=body.otp_value,
    )

    if not success:
        raise HTTPException(
            status_code=400,
            detail="Challenge not found, already answered, or expired.",
        )

    return {"status": "answered", "message": "OTP submitted successfully."}
