import logging
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId

from app.core.database import db
from app.models.notification_models import NotificationResponse, NotificationListResponse

logger = logging.getLogger(__name__)

def _serialize_notification(doc: dict) -> dict:
    """Convert MongoDB notification doc to dict for response."""
    return {
        "id": str(doc["_id"]),
        "user_id": str(doc["user_id"]),
        "type": doc.get("type", "system_notification"),
        "title": doc["title"],
        "body": doc["body"],
        "data": doc.get("data"),
        "read": doc.get("read", False),
        "created_at": doc["created_at"],
    }

async def create_notification(
    user_id: str,
    title: str,
    body: str,
    type: str = "system_notification",
    data: Optional[dict] = None
) -> dict:
    """Create a new notification record in the database."""
    now = datetime.now(tz=timezone.utc)
    doc = {
        "user_id": user_id,
        "type": type,
        "title": title,
        "body": body,
        "data": data,
        "read": False,
        "created_at": now,
    }
    result = await db.notifications.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize_notification(doc)

async def get_user_notifications(
    user_id: str,
    limit: int = 50,
    skip: int = 0
) -> dict:
    """Get notification history for a user."""
    cursor = db.notifications.find({"user_id": user_id}).sort("created_at", -1).skip(skip).limit(limit)
    
    notifications = []
    async for doc in cursor:
        notifications.append(_serialize_notification(doc))
        
    total = await db.notifications.count_documents({"user_id": user_id})
    unread_count = await db.notifications.count_documents({"user_id": user_id, "read": False})
    
    return {
        "notifications": notifications,
        "total": total,
        "unread_count": unread_count
    }

async def mark_as_read(user_id: str, notification_id: str) -> bool:
    """Mark a specific notification as read."""
    result = await db.notifications.update_one(
        {"_id": ObjectId(notification_id), "user_id": user_id},
        {"$set": {"read": True}}
    )
    return result.modified_count > 0

async def mark_all_as_read(user_id: str) -> int:
    """Mark all unread notifications for a user as read."""
    result = await db.notifications.update_many(
        {"user_id": user_id, "read": False},
        {"$set": {"read": True}}
    )
    return result.modified_count

async def delete_notification(user_id: str, notification_id: str) -> bool:
    """Delete a specific notification."""
    result = await db.notifications.delete_one(
        {"_id": ObjectId(notification_id), "user_id": user_id}
    )
    return result.deleted_count > 0

async def notify_company(
    company_id: str,
    title: str,
    body: str,
    type: str = "system_notification",
    data: Optional[dict] = None
):
    """Send a notification to all members of a company."""
    company = await db.companies.find_one({"id": company_id})
    if not company:
        return
        
    user_ids = [company.get("user_id")]
    for member in company.get("members", []):
        if member.get("user_id"):
            user_ids.append(member["user_id"])
            
    # Remove duplicates and None values
    user_ids = list(set([uid for uid in user_ids if uid]))
    
    for uid in user_ids:
        await create_notification(
            user_id=uid,
            title=title,
            body=body,
            type=type,
            data=data
        )
