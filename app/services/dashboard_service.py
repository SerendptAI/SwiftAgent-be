from datetime import datetime, timedelta
from uuid import uuid4
from app.core.database import db

async def get_stats(company_id: str) -> dict:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    # visitor count today
    visitors_today = await db.visitors.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": today_start},
    })

    # chats (widget conversations)
    chats_today = await db.widget_conversations.count_documents({
        "company_id": company_id,
        "created_at": {"$gte": today_start},
    })

    pending_chats = await db.widget_conversations.count_documents({
        "company_id": company_id,
        "seen": {"$ne": True},
    })

    # calls today
    calls_today = await db.calls.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": today_start},
    })

    # documents count (uploaded knowledge sources)
    documents_today = await db.knowledge_sources.count_documents({
        "company_id": company_id,
        "uploaded_at": {"$gte": today_start},
    })

    # scrapes count
    scrapes_today = await db.scrapes.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": today_start},
    })

    return {
        "visitors": {"today": visitors_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
        "chats": {"today": chats_today, "pending": pending_chats, "last_7_days_up": 0, "last_7_days_down": 0},
        "calls": {"today": calls_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
        "documents": {"today": documents_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
        "scrapes": {"today": scrapes_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
    }

async def get_visitors(company_id: str, limit: int = 20) -> list:
    cursor = db.visitors.find({"company_id": company_id}).sort("timestamp", -1).limit(limit)
    return await cursor.to_list(length=limit)

async def get_chats(company_id: str, limit: int = 50, skip: int = 0) -> list:
    cursor = db.widget_conversations.aggregate([
        {"$match": {"company_id": company_id}},
        {"$sort": {"updated_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {"$project": {
            "id": 1,
            "company_id": 1,
            "session_id": 1,
            "created_at": 1,
            "updated_at": 1,
            "message_count": {"$size": {"$ifNull": ["$messages", []]}},
            "seen": {"$ifNull": ["$seen", False]}
        }}
    ])
    return await cursor.to_list(length=limit)

async def get_chat_by_id(company_id: str, chat_id: str) -> dict:
    return await db.widget_conversations.find_one({
        "company_id": company_id,
        "id": chat_id
    })

async def mark_chat_seen(company_id: str, chat_id: str) -> bool:
    result = await db.widget_conversations.update_one(
        {"company_id": company_id, "id": chat_id},
        {"$set": {"seen": True}}
    )
    return result.modified_count > 0

async def log_visitor(company_id: str, ip_address: str) -> dict:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Check if a document with company_id, visitor_id (IP), and timestamp >= today_start exists
    existing_visitor = await db.visitors.find_one({
        "company_id": company_id,
        "visitor_id": ip_address,
        "timestamp": {"$gte": today_start}
    })

    if existing_visitor:
        return {"status": "already_logged", "id": existing_visitor["id"]}

    # Insert a new record
    new_id = str(uuid4())
    doc = {
        "id": new_id,
        "company_id": company_id,
        "visitor_id": ip_address,
        "timestamp": now,
        "duration_seconds": 0
    }
    await db.visitors.insert_one(doc)
    return {"status": "logged", "id": new_id}
