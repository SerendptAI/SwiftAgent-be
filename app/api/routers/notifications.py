import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_user
from app.core.config import settings
from app.models.notification_models import NotificationListResponse, NotificationResponse, VapidPublicKeyResponse
from app.models.otp_challenge_models import RegisterDeviceRequest, TestPushNotificationRequest
from app.services import notification_service
from app.services.push_notification_service import register_device, unregister_device, send_test_push

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Notifications"])

@router.get("/vapid-public-key", response_model=VapidPublicKeyResponse)
async def get_vapid_public_key():
    """Get the VAPID public key for Web Push subscriptions."""
    if not settings.VAPID_PUBLIC_KEY:
        raise HTTPException(status_code=404, detail="Web push is not configured on this server")
    return {"public_key": settings.VAPID_PUBLIC_KEY}

@router.post("/devices/register")
async def register_push_device(
    body: RegisterDeviceRequest,
    user: dict = Depends(get_current_user),
):
    """Register a push token (Expo or Web Push subscription)."""
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
    """Unregister a push token."""
    removed = await unregister_device(user["user_id"], device_token)
    if not removed:
        raise HTTPException(status_code=404, detail="Device token not found")
    return {"status": "unregistered"}

@router.post("/test-push")
async def send_test_push_notification(
    body: TestPushNotificationRequest,
    user: dict = Depends(get_current_user),
):
    """Send a test push notification to all registered devices of the authenticated user."""
    # Also save it to history for testing purposes
    await notification_service.create_notification(
        user_id=user["user_id"],
        title=body.title,
        body=body.body,
        type="test_otp_notification",
        data={"type": "test_otp_notification"}
    )
    
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

@router.get("", response_model=NotificationListResponse)
async def get_notifications(
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
):
    """Get the notification history for the authenticated user."""
    return await notification_service.get_user_notifications(
        user_id=user["user_id"], 
        limit=limit, 
        skip=skip
    )

@router.put("/{notification_id}/read")
async def mark_notification_as_read(
    notification_id: str,
    user: dict = Depends(get_current_user),
):
    """Mark a specific notification as read."""
    success = await notification_service.mark_as_read(user["user_id"], notification_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "success"}

@router.post("/read-all")
async def mark_all_notifications_as_read(
    user: dict = Depends(get_current_user),
):
    """Mark all unread notifications as read."""
    updated_count = await notification_service.mark_all_as_read(user["user_id"])
    return {"status": "success", "updated_count": updated_count}

@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    user: dict = Depends(get_current_user),
):
    """Delete a specific notification from history."""
    success = await notification_service.delete_notification(user["user_id"], notification_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "success"}
