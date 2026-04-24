"""
Push Notification Service — Firebase Cloud Messaging integration.

Sends push notifications to the mobile app when the stroll agent
encounters an OTP/2FA challenge during automated login.

Gracefully degrades to a no-op when FCM is not configured (the mobile
app can still poll the challenges endpoint as a fallback).
"""

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import firebase_admin
from firebase_admin import credentials, messaging

from app.core.config import settings
from app.core.database import db

logger = logging.getLogger(__name__)

# Lazy-loaded Firebase state
_fcm_initialized = False


def _ensure_fcm() -> bool:
    """Initialize the Firebase Admin SDK on first use (lazy singleton)."""
    global _fcm_initialized
    if _fcm_initialized:
        return True

    b64_creds = settings.FCM_SERVICE_ACCOUNT_B64
    if not b64_creds:
        logger.warning(
            "FCM_SERVICE_ACCOUNT_B64 not set — push notifications disabled"
        )
        return False

    try:
        # Decode base64 → JSON dict → Firebase credential
        raw_json = base64.b64decode(b64_creds)
        service_info = json.loads(raw_json)

        # Only initialize if no default app exists yet
        if not firebase_admin._apps:
            cred = credentials.Certificate(service_info)
            firebase_admin.initialize_app(cred)

        _fcm_initialized = True
        logger.info("Firebase Admin SDK initialized for push notifications")
        return True
    except Exception as e:
        logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
        return False


async def register_device(
    user_id: str,
    device_token: str,
    device_name: str = "",
    platform: str = "android",
) -> dict:
    """Register or update an FCM device token for a user."""
    now = datetime.now(tz=timezone.utc)
    doc = {
        "user_id": user_id,
        "device_token": device_token,
        "device_name": device_name,
        "platform": platform,
        "registered_at": now,
    }

    await db.device_tokens.update_one(
        {"user_id": user_id, "device_token": device_token},
        {"$set": doc},
        upsert=True,
    )
    logger.info(f"Device registered for user {user_id}: {device_name} ({platform})")
    return doc


async def unregister_device(user_id: str, device_token: str) -> bool:
    """Remove an FCM device token."""
    result = await db.device_tokens.delete_one(
        {"user_id": user_id, "device_token": device_token}
    )
    if result.deleted_count:
        logger.info(f"Device unregistered for user {user_id}")
        return True
    return False


async def _get_user_device_tokens(user_id: str) -> list[str]:
    """Get all FCM tokens for a user."""
    cursor = db.device_tokens.find(
        {"user_id": user_id},
        {"device_token": 1, "_id": 0},
    )
    tokens = []
    async for doc in cursor:
        tokens.append(doc["device_token"])
    return tokens


async def send_otp_challenge_push(
    user_id: str,
    challenge_id: str,
    login_url: str,
    screenshot_url: Optional[str] = None,
) -> bool:
    """
    Send a high-priority push notification to the user's mobile device(s)
    for an OTP challenge.

    Returns True if at least one notification was sent successfully.
    """
    if not _ensure_fcm():
        logger.info("FCM not configured — skipping push, mobile app will poll")
        return False

    tokens = await _get_user_device_tokens(user_id)
    if not tokens:
        logger.warning(f"No device tokens found for user {user_id} — cannot push OTP challenge")
        return False

    # Build the notification
    notification = messaging.Notification(
        title="🔐 OTP Required",
        body="Your agent needs an OTP to log into a dashboard. Tap to enter the code.",
    )

    # Data payload for the mobile app
    data = {
        "type": "otp_challenge",
        "challenge_id": challenge_id,
        "login_url": login_url,
    }
    if screenshot_url:
        data["screenshot_url"] = screenshot_url

    # Android high-priority config
    android_config = messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(
            channel_id="otp_challenge",
            priority="max",
            default_sound=True,
        ),
    )

    # iOS critical alert config
    apns_config = messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(
                alert=messaging.ApsAlert(
                    title="🔐 OTP Required",
                    body="Your agent needs an OTP. Tap to enter the code.",
                ),
                sound="default",
                badge=1,
                content_available=True,
            ),
        ),
    )

    success_count = 0
    stale_tokens = []

    for token in tokens:
        try:
            message = messaging.Message(
                notification=notification,
                data=data,
                token=token,
                android=android_config,
                apns=apns_config,
            )
            messaging.send(message)
            success_count += 1
        except messaging.UnregisteredError:
            stale_tokens.append(token)
        except Exception as e:
            logger.warning(f"Failed to send push to token {token[:20]}...: {e}")

    # Clean up stale tokens
    if stale_tokens:
        for stale in stale_tokens:
            await db.device_tokens.delete_one(
                {"user_id": user_id, "device_token": stale}
            )
        logger.info(f"Removed {len(stale_tokens)} stale device token(s) for user {user_id}")

    logger.info(
        f"OTP challenge push sent to {success_count}/{len(tokens)} device(s) for user {user_id}"
    )
    return success_count > 0
