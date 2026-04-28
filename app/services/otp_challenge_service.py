"""
OTP Challenge Service — lifecycle management for stroll 2FA relay.

Orchestrates the create → push → wait → respond flow:
1. Stroll agent detects an OTP page and calls create_challenge()
2. Push notification is sent to the admin's mobile device
3. Agent calls wait_for_challenge_response() which polls until answered
4. Mobile app calls respond_to_challenge() with the OTP value
5. Agent receives the OTP and continues login
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from app.core.config import settings
from app.core.database import db
from app.models.otp_challenge_models import OTPChallenge
from app.services.push_notification_service import send_otp_challenge_push

logger = logging.getLogger(__name__)


async def create_challenge(
    company_id: str,
    user_id: str,
    login_url: str,
    screenshot_url: Optional[str] = None,
) -> OTPChallenge:
    """
    Create a new OTP challenge and push a notification to the admin.

    Args:
        company_id: Company whose stroll triggered this
        user_id: Admin to notify
        login_url: Dashboard URL the agent is logging into
        screenshot_url: Optional Cloudinary URL of the OTP page screenshot

    Returns:
        The created OTPChallenge document
    """
    now = datetime.now(tz=timezone.utc)
    expiry = now + timedelta(seconds=settings.OTP_CHALLENGE_EXPIRY_SECONDS)

    challenge = OTPChallenge(
        id=f"otp_{str(uuid4())[:8]}",
        company_id=company_id,
        user_id=user_id,
        status="pending",
        otp_value=None,
        screenshot_url=screenshot_url,
        login_url=login_url,
        created_at=now,
        answered_at=None,
        expires_at=expiry,
    )

    await db.otp_challenges.insert_one(challenge.model_dump())
    logger.info(
        f"OTP challenge {challenge.id} created for company {company_id}, "
        f"notifying user {user_id}"
    )

    # Fire push notification (non-blocking — if it fails, mobile app can still poll)
    await send_otp_challenge_push(
        user_id=user_id,
        challenge_id=challenge.id,
        login_url=login_url,
        screenshot_url=screenshot_url,
    )

    return challenge


async def wait_for_challenge_response(
    challenge_id: str,
    timeout_seconds: Optional[int] = None,
    poll_interval: float = 2.0,
) -> Optional[str]:
    """
    Poll MongoDB until the challenge is answered or the timeout expires.

    This is called by the stroll agent and blocks the coroutine (not the
    event loop) while waiting for the human to respond via the mobile app.

    Args:
        challenge_id: The challenge to wait for
        timeout_seconds: Max wait time (defaults to config value)
        poll_interval: Seconds between polls

    Returns:
        The OTP value if answered, None if timed out or expired
    """
    if timeout_seconds is None:
        timeout_seconds = settings.OTP_CHALLENGE_TIMEOUT_SECONDS

    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=timeout_seconds)

    logger.info(
        f"Waiting for OTP challenge {challenge_id} response "
        f"(timeout: {timeout_seconds}s)"
    )

    while datetime.now(tz=timezone.utc) < deadline:
        doc = await db.otp_challenges.find_one({"id": challenge_id})
        if not doc:
            logger.error(f"OTP challenge {challenge_id} not found in DB")
            return None

        status = doc.get("status")

        if status == "answered":
            otp_value = doc.get("otp_value")
            logger.info(f"OTP challenge {challenge_id} answered")
            return otp_value

        if status == "expired":
            logger.warning(f"OTP challenge {challenge_id} expired before response")
            return None

        await asyncio.sleep(poll_interval)

    # Timed out — mark as expired
    logger.warning(f"OTP challenge {challenge_id} timed out after {timeout_seconds}s")
    await db.otp_challenges.update_one(
        {"id": challenge_id, "status": "pending"},
        {"$set": {"status": "expired"}},
    )
    return None


async def respond_to_challenge(
    challenge_id: str,
    user_id: str,
    otp_value: str,
) -> bool:
    """
    Submit an OTP value for a pending challenge.

    Called by the mobile app via the API. Validates ownership and
    updates the challenge status to 'answered'.

    Returns True if the response was accepted, False otherwise.
    """
    now = datetime.now(tz=timezone.utc)

    doc = await db.otp_challenges.find_one({"id": challenge_id})
    if not doc:
        logger.warning(f"Challenge {challenge_id} not found")
        return False

    if doc.get("user_id") != user_id:
        logger.warning(
            f"User {user_id} tried to respond to challenge {challenge_id} "
            f"owned by {doc.get('user_id')}"
        )
        return False

    if doc.get("status") != "pending":
        logger.warning(
            f"Challenge {challenge_id} is not pending (status: {doc.get('status')})"
        )
        return False

    # Check expiry
    expires_at = doc.get("expires_at")
    if expires_at:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now > expires_at:
            await db.otp_challenges.update_one(
                {"id": challenge_id},
                {"$set": {"status": "expired"}},
            )
            logger.warning(f"Challenge {challenge_id} already expired")
            return False

    result = await db.otp_challenges.update_one(
        {"id": challenge_id, "status": "pending"},
        {
            "$set": {
                "status": "answered",
                "otp_value": otp_value,
                "answered_at": now,
            }
        },
    )

    if result.modified_count == 0:
        logger.warning(f"Failed to update challenge {challenge_id} — race condition or already answered")
        return False

    logger.info(f"OTP challenge {challenge_id} answered by user {user_id}")
    return True


async def get_pending_challenges(user_id: str) -> list[dict]:
    """Get all pending OTP challenges for a user."""
    cursor = db.otp_challenges.find(
        {"user_id": user_id, "status": "pending"},
    ).sort("created_at", -1)

    results = []
    async for doc in cursor:
        doc.pop("_id", None)
        doc.pop("otp_value", None)  # don't leak to listing
        results.append(doc)
    return results


async def get_challenge(challenge_id: str) -> Optional[dict]:
    """Get a single challenge by ID."""
    doc = await db.otp_challenges.find_one({"id": challenge_id})
    if doc:
        doc.pop("_id", None)
        return doc
    return None
