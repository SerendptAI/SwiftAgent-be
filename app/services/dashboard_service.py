from datetime import datetime, timedelta
from uuid import uuid4
from app.core.database import db

async def get_stats(company_id: str) -> dict:
    now = datetime.utcnow()
    
    # Time boundaries
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    
    last_7_days_start = now - timedelta(days=7)
    previous_7_days_start = last_7_days_start - timedelta(days=7)

    async def get_stat_group(collection, date_field):
        # today's count
        today_count = await collection.count_documents({
            "company_id": company_id,
            date_field: {"$gte": today_start}
        })
        
        # yesterday's count
        yesterday_count = await collection.count_documents({
            "company_id": company_id,
            date_field: {"$gte": yesterday_start, "$lt": today_start}
        })
        
        # last 7 days count
        last_7_days_count = await collection.count_documents({
            "company_id": company_id,
            date_field: {"$gte": last_7_days_start}
        })
        
        # previous 7 days count
        previous_7_days_count = await collection.count_documents({
            "company_id": company_id,
            date_field: {"$gte": previous_7_days_start, "$lt": last_7_days_start}
        })
        
        # calculate percent change (today vs yesterday)
        if yesterday_count == 0:
            percent_change = 100.0 if today_count > 0 else 0.0
        else:
            percent_change = ((today_count - yesterday_count) / yesterday_count) * 100.0
            
        # calculate up/down trends
        trend_diff = last_7_days_count - previous_7_days_count
        trend_up = trend_diff if trend_diff > 0 else 0
        trend_down = abs(trend_diff) if trend_diff < 0 else 0
            
        return {
            "today": today_count,
            "percent_change": round(percent_change, 1),
            "last_7_days_up": trend_up,
            "last_7_days_down": trend_down
        }

    visitors_stat = await get_stat_group(db.visitors, "timestamp")
    calls_stat = await get_stat_group(db.calls, "timestamp")
    documents_stat = await get_stat_group(db.knowledge_sources, "uploaded_at")
    scrapes_stat = await get_stat_group(db.scrapes, "timestamp")
    
    # chats (widget conversations) has an extra 'pending' field
    chats_stat = await get_stat_group(db.widget_conversations, "created_at")
    pending_chats = await db.widget_conversations.count_documents({
        "company_id": company_id,
        "seen": {"$ne": True},
    })
    chats_stat["pending"] = pending_chats

    return {
        "visitors": visitors_stat,
        "chats": chats_stat,
        "calls": calls_stat,
        "documents": documents_stat,
        "scrapes": scrapes_stat,
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
