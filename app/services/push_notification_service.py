"""
Push Notification Service — Expo Push Notifications integration.

Sends push notifications to the mobile app when the stroll agent
encounters an OTP/2FA challenge during automated login.

Uses Expo's push notification HTTP API:
https://docs.expo.dev/push-notifications/sending-notifications/

Gracefully degrades to a no-op when the device has no Expo push token
(the mobile app can still poll the challenges endpoint as a fallback).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.core.config import settings
from app.core.database import db

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


async def register_device(
    user_id: str,
    device_token: str,
    device_name: str = "",
    platform: str = "android",
) -> dict:
    """Register or update an Expo push token for a user."""
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
    """Remove an Expo push token."""
    result = await db.device_tokens.delete_one(
        {"user_id": user_id, "device_token": device_token}
    )
    if result.deleted_count:
        logger.info(f"Device unregistered for user {user_id}")
        return True
    return False


async def _get_user_device_tokens(user_id: str) -> list[str]:
    """Get all Expo push tokens for a user."""
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
    for an OTP challenge via Expo Push Service.

    Returns True if at least one notification was sent successfully.
    """
    tokens = await _get_user_device_tokens(user_id)
    if not tokens:
        logger.warning(
            f"No device tokens found for user {user_id} — cannot push OTP challenge"
        )
        return False

    # Build Expo push messages (one per token)
    data_payload = {
        "type": "otp_challenge",
        "challenge_id": challenge_id,
        "login_url": login_url,
    }
    if screenshot_url:
        data_payload["screenshot_url"] = screenshot_url

    messages = []
    for token in tokens:
        messages.append(
            {
                "to": token,
                "title": "🔐 OTP Required",
                "body": "Your agent needs an OTP to log into a dashboard. Tap to enter the code.",
                "data": data_payload,
                "sound": "default",
                "priority": "high",
                "channelId": "otp_challenge",
            }
        )

    # Build request headers
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    access_token = settings.EXPO_ACCESS_TOKEN
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    # Send to Expo Push API
    success_count = 0
    stale_tokens = []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers=headers,
            )
            response.raise_for_status()
            result = response.json()

            # Expo returns {"data": [{ "status": "ok"|"error", ... }, ...]}
            ticket_data = result.get("data", [])

            for i, ticket in enumerate(ticket_data):
                token = tokens[i] if i < len(tokens) else "unknown"

                if ticket.get("status") == "ok":
                    success_count += 1
                elif ticket.get("status") == "error":
                    error_detail = ticket.get("details", {})
                    error_type = error_detail.get("error", "")

                    if error_type == "DeviceNotRegistered":
                        stale_tokens.append(token)
                        logger.info(
                            f"Expo push token {token[:30]}... is no longer registered"
                        )
                    else:
                        logger.warning(
                            f"Expo push error for token {token[:30]}...: "
                            f"{ticket.get('message', 'unknown error')}"
                        )

    except httpx.HTTPStatusError as e:
        logger.error(f"Expo Push API returned HTTP {e.response.status_code}: {e}")
    except Exception as e:
        logger.error(f"Failed to send Expo push notification: {e}")

    # Clean up stale tokens
    if stale_tokens:
        for stale in stale_tokens:
            await db.device_tokens.delete_one(
                {"user_id": user_id, "device_token": stale}
            )
        logger.info(
            f"Removed {len(stale_tokens)} stale Expo push token(s) for user {user_id}"
        )

    logger.info(
        f"OTP challenge push sent to {success_count}/{len(tokens)} device(s) "
        f"for user {user_id}"
    )
    return success_count > 0


async def send_test_push(
    user_id: str,
    title: str = "🔐 OTP Required",
    body: str = "Test: Your agent needs an OTP to log into a dashboard. Tap to enter the code.",
) -> dict:
    """
    Send a test OTP-themed push notification to all of the user's registered devices.
    
    Returns a dict with:
      - success_count: number of devices that received the notification
      - total_devices: total number of registered devices
      - stale_tokens_removed: number of stale tokens cleaned up
    """
    tokens = await _get_user_device_tokens(user_id)
    if not tokens:
        logger.warning(f"No device tokens found for user {user_id}")
        return {
            "success_count": 0,
            "total_devices": 0,
            "stale_tokens_removed": 0,
        }

    # Build Expo push messages
    messages = []
    for token in tokens:
        messages.append(
            {
                "to": token,
                "title": title,
                "body": body,
                "data": {"type": "test_otp_notification"},
                "sound": "default",
                "priority": "high",
                "channelId": "otp_challenge",
            }
        )

    # Build request headers
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    access_token = settings.EXPO_ACCESS_TOKEN
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    # Send to Expo Push API
    success_count = 0
    stale_tokens = []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers=headers,
            )
            response.raise_for_status()
            result = response.json()

            ticket_data = result.get("data", [])

            for i, ticket in enumerate(ticket_data):
                token = tokens[i] if i < len(tokens) else "unknown"

                if ticket.get("status") == "ok":
                    success_count += 1
                elif ticket.get("status") == "error":
                    error_detail = ticket.get("details", {})
                    error_type = error_detail.get("error", "")

                    if error_type == "DeviceNotRegistered":
                        stale_tokens.append(token)
                        logger.info(f"Expo push token {token[:30]}... is stale")
                    else:
                        logger.warning(
                            f"Expo push error for token {token[:30]}...: "
                            f"{ticket.get('message', 'unknown error')}"
                        )

    except httpx.HTTPStatusError as e:
        logger.error(f"Expo Push API returned HTTP {e.response.status_code}: {e}")
    except Exception as e:
        logger.error(f"Failed to send test push notification: {e}")

    # Clean up stale tokens
    stale_count = 0
    if stale_tokens:
        for stale in stale_tokens:
            await db.device_tokens.delete_one(
                {"user_id": user_id, "device_token": stale}
            )
        stale_count = len(stale_tokens)
        logger.info(f"Removed {stale_count} stale Expo push token(s) for user {user_id}")

    logger.info(f"Test push sent to {success_count}/{len(tokens)} device(s) for user {user_id}")
    
    return {
        "success_count": success_count,
        "total_devices": len(tokens),
        "stale_tokens_removed": stale_count,
    }
