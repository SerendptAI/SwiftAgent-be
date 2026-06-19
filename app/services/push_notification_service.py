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
import json
import asyncio

import httpx
from pywebpush import webpush, WebPushException

from app.core.config import settings
from app.core.database import db
from app.services import notification_service

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


async def _get_user_devices(user_id: str) -> list[dict]:
    """Get all registered devices (tokens + platform) for a user."""
    cursor = db.device_tokens.find(
        {"user_id": user_id},
        {"device_token": 1, "platform": 1, "_id": 0},
    )
    devices = []
    async for doc in cursor:
        devices.append({
            "device_token": doc["device_token"],
            "platform": doc.get("platform", "android")
        })
    return devices


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
    devices = await _get_user_devices(user_id)
    if not devices:
        logger.warning(
            f"No device tokens found for user {user_id} — cannot push OTP challenge"
        )
        return False

    # Create history notification for the challenge without triggering a duplicate background push
    await db.notifications.insert_one({
        "user_id": user_id,
        "type": "otp_challenge",
        "title": "🔐 OTP Required",
        "body": "Your agent needs an OTP to log into a dashboard. Tap to enter the code.",
        "data": {
            "type": "otp_challenge",
            "challenge_id": challenge_id,
            "login_url": login_url,
            "screenshot_url": screenshot_url
        },
        "read": False,
        "created_at": datetime.now(tz=timezone.utc),
    })

    # Build push messages
    data_payload = {
        "type": "otp_challenge",
        "challenge_id": challenge_id,
        "login_url": login_url,
    }
    if screenshot_url:
        data_payload["screenshot_url"] = screenshot_url

    expo_messages = []
    expo_tokens = []
    web_tokens = []

    for device in devices:
        token = device["device_token"]
        if device["platform"] == "web":
            web_tokens.append(token)
        else:
            expo_tokens.append(token)
            expo_messages.append(
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

    # Build request headers for Expo
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    access_token = settings.EXPO_ACCESS_TOKEN
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"

    # Send to Expo Push API individually to avoid project mismatch errors
    success_count = 0
    stale_tokens = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        for token, message in zip(expo_tokens, expo_messages):
            try:
                response = await client.post(
                    EXPO_PUSH_URL,
                    json=[message],
                    headers=headers,
                )
                response.raise_for_status()
                result = response.json()

                ticket_data = result.get("data", [])
                for ticket in ticket_data:
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

    # Send Web Push Notifications
    if settings.VAPID_PRIVATE_KEY and settings.VAPID_CLAIMS_EMAIL:
        for web_token in web_tokens:
            try:
                subscription_info = json.loads(web_token)
                def _send():
                    webpush(
                        subscription_info=subscription_info,
                        data=json.dumps({
                            "title": "🔐 OTP Required",
                            "body": "Your agent needs an OTP to log into a dashboard. Tap to enter the code.",
                            "data": data_payload,
                        }),
                        vapid_private_key=settings.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": settings.VAPID_CLAIMS_EMAIL},
                        ttl=300
                    )
                await asyncio.to_thread(_send)
                success_count += 1
            except WebPushException as ex:
                logger.warning(f"Web Push exception: {ex}")
                if ex.response is not None and ex.response.status_code in (404, 410):
                    stale_tokens.append(web_token)
            except Exception as e:
                logger.error(f"Failed to send Web Push notification: {e}")

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
        f"OTP challenge push sent to {success_count}/{len(devices)} device(s) "
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
    devices = await _get_user_devices(user_id)
    if not devices:
        logger.warning(f"No device tokens found for user {user_id}")
        return {
            "success_count": 0,
            "total_devices": 0,
            "stale_tokens_removed": 0,
        }

    # Build Expo push messages
    expo_messages = []
    expo_tokens = []
    web_tokens = []

    for device in devices:
        token = device["device_token"]
        if device["platform"] == "web":
            web_tokens.append(token)
        else:
            expo_tokens.append(token)
            expo_messages.append(
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

    # Send to Expo Push API individually
    success_count = 0
    stale_tokens = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        for token, message in zip(expo_tokens, expo_messages):
            try:
                response = await client.post(
                    EXPO_PUSH_URL,
                    json=[message],
                    headers=headers,
                )
                response.raise_for_status()
                result = response.json()

                ticket_data = result.get("data", [])

                for ticket in ticket_data:
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

    # Send Web Push Notifications
    if settings.VAPID_PRIVATE_KEY and settings.VAPID_CLAIMS_EMAIL:
        for web_token in web_tokens:
            try:
                subscription_info = json.loads(web_token)
                def _send():
                    webpush(
                        subscription_info=subscription_info,
                        data=json.dumps({
                            "title": title,
                            "body": body,
                            "data": {"type": "test_otp_notification"},
                        }),
                        vapid_private_key=settings.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": settings.VAPID_CLAIMS_EMAIL},
                        ttl=300
                    )
                await asyncio.to_thread(_send)
                success_count += 1
            except WebPushException as ex:
                logger.warning(f"Web Push exception: {ex}")
                if ex.response is not None and ex.response.status_code in (404, 410):
                    stale_tokens.append(web_token)
            except Exception as e:
                logger.error(f"Failed to send Web Push notification: {e}")

    # Clean up stale tokens
    stale_count = 0
    if stale_tokens:
        for stale in stale_tokens:
            await db.device_tokens.delete_one(
                {"user_id": user_id, "device_token": stale}
            )
        stale_count = len(stale_tokens)
        logger.info(f"Removed {stale_count} stale Expo push token(s) for user {user_id}")

    logger.info(f"Test push sent to {success_count}/{len(devices)} device(s) for user {user_id}")
    
    return {
        "success_count": success_count,
        "total_devices": len(devices),
        "stale_tokens_removed": stale_count,
    }


async def send_generic_push(
    user_id: str,
    title: str,
    body: str,
    notification_type: str = "system_notification",
    data: Optional[dict] = None,
) -> bool:
    """
    Send a generic push notification to all of a user's registered devices.
    The type and data are included in the push payload so the mobile app
    can identify and group them (e.g. 'announcement', 'ticket_resolved').
    """
    devices = await _get_user_devices(user_id)
    if not devices:
        return False

    payload_data = data or {}
    payload_data["type"] = notification_type

    expo_messages = []
    expo_tokens = []
    web_tokens = []

    for device in devices:
        token = device["device_token"]
        if device["platform"] == "web":
            web_tokens.append(token)
        else:
            expo_tokens.append(token)
            expo_messages.append(
                {
                    "to": token,
                    "title": title,
                    "body": body,
                    "data": payload_data,
                    "sound": "default",
                    "priority": "normal",
                    "channelId": "default",
                }
            )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if settings.EXPO_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {settings.EXPO_ACCESS_TOKEN}"

    success_count = 0
    stale_tokens = []

    async with httpx.AsyncClient(timeout=15.0) as client:
        for token, message in zip(expo_tokens, expo_messages):
            try:
                response = await client.post(EXPO_PUSH_URL, json=[message], headers=headers)
                response.raise_for_status()
                for ticket in response.json().get("data", []):
                    if ticket.get("status") == "ok":
                        success_count += 1
                    elif ticket.get("status") == "error":
                        if ticket.get("details", {}).get("error") == "DeviceNotRegistered":
                            stale_tokens.append(token)
            except Exception as e:
                logger.error(f"Failed to send generic Expo push: {e}")

    if settings.VAPID_PRIVATE_KEY and settings.VAPID_CLAIMS_EMAIL:
        for web_token in web_tokens:
            try:
                sub_info = json.loads(web_token)
                def _send():
                    webpush(
                        subscription_info=sub_info,
                        data=json.dumps({"title": title, "body": body, "data": payload_data}),
                        vapid_private_key=settings.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": settings.VAPID_CLAIMS_EMAIL},
                        ttl=300
                    )
                await asyncio.to_thread(_send)
                success_count += 1
            except Exception as e:
                logger.error(f"Failed to send generic Web push: {e}")

    if stale_tokens:
        for stale in stale_tokens:
            await db.device_tokens.delete_one({"user_id": user_id, "device_token": stale})

    return success_count > 0

