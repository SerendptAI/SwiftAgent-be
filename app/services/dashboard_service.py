from datetime import datetime, timedelta, timezone
from uuid import uuid4
from app.core.database import db


async def get_stats(company_id: str) -> dict:
    now = datetime.now(tz=timezone.utc)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    last_7_days_start = now - timedelta(days=7)
    previous_7_days_start = last_7_days_start - timedelta(days=7)

    async def get_stat_group(collection, date_field):
        pipeline = [
            {"$match": {"company_id": company_id}},
            {
                "$facet": {
                    "today": [
                        {"$match": {date_field: {"$gte": today_start}}},
                        {"$count": "count"},
                    ],
                    "yesterday": [
                        {
                            "$match": {
                                date_field: {
                                    "$gte": yesterday_start,
                                    "$lt": today_start,
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                    "last_7_days": [
                        {"$match": {date_field: {"$gte": last_7_days_start}}},
                        {"$count": "count"},
                    ],
                    "previous_7_days": [
                        {
                            "$match": {
                                date_field: {
                                    "$gte": previous_7_days_start,
                                    "$lt": last_7_days_start,
                                }
                            }
                        },
                        {"$count": "count"},
                    ],
                }
            },
        ]

        results = await collection.aggregate(pipeline).to_list(length=1)
        data = results[0] if results else {}

        today_count = data["today"][0]["count"] if data.get("today") else 0
        yesterday_count = data["yesterday"][0]["count"] if data.get("yesterday") else 0
        last_7_days_count = (
            data["last_7_days"][0]["count"] if data.get("last_7_days") else 0
        )
        previous_7_days_count = (
            data["previous_7_days"][0]["count"] if data.get("previous_7_days") else 0
        )

        if yesterday_count == 0:
            percent_change = 100.0 if today_count > 0 else 0.0
        else:
            percent_change = ((today_count - yesterday_count) / yesterday_count) * 100.0

        trend_diff = last_7_days_count - previous_7_days_count
        trend_up = trend_diff if trend_diff > 0 else 0
        trend_down = abs(trend_diff) if trend_diff < 0 else 0

        return {
            "today": today_count,
            "percent_change": round(percent_change, 1),
            "last_7_days_up": trend_up,
            "last_7_days_down": trend_down,
        }

    visitors_stat = await get_stat_group(db.visitors, "timestamp")
    calls_stat = await get_stat_group(db.calls, "timestamp")
    documents_stat = await get_stat_group(db.knowledge_sources, "uploaded_at")
    scrapes_stat = await get_stat_group(db.scrapes, "timestamp")
    strolls_stat = await get_stat_group(db.stroll_versions, "timestamp")

    chats_stat = await get_stat_group(db.widget_conversations, "created_at")
    pending_chats = await db.widget_conversations.count_documents(
        {
            "company_id": company_id,
            "seen": {"$ne": True},
        }
    )
    chats_stat["pending"] = pending_chats

    return {
        "visitors": visitors_stat,
        "chats": chats_stat,
        "calls": calls_stat,
        "documents": documents_stat,
        "scrapes": scrapes_stat,
        "strolls": strolls_stat,
    }


async def get_visitors(company_id: str, limit: int = 20) -> list:
    cursor = (
        db.visitors.find({"company_id": company_id}, {"_id": 0}).sort("timestamp", -1).limit(limit)
    )
    return await cursor.to_list(length=limit)


async def get_chats(company_id: str, limit: int = 50, skip: int = 0) -> list:
    """Return resolved tickets only (Resolved section).
    
    Note: Escalated chats (now tickets) are excluded; admins view them via ticket listing.
    """

    # NOTE: escalated chats excluded intentionally—they now have corresponding tickets
    # 1. Non-escalated chats ONLY
    chat_pipeline = [
        {"$match": {"company_id": company_id, "escalated": {"$ne": True}}},
        {"$sort": {"updated_at": -1}},
        {
            "$project": {
                "_id": 0,
                "id": 1,
                "company_id": 1,
                "session_id": 1,
                "created_at": 1,
                "updated_at": 1,
                "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                "seen": {"$ifNull": ["$seen", False]},
                "type": {"$literal": "chat"},
            }
        },
    ]
    chats = await db.widget_conversations.aggregate(chat_pipeline).to_list(length=None)

    # 2. Resolved tickets
    ticket_pipeline = [
        {"$match": {"company_id": company_id, "status": "resolved"}},
        {"$sort": {"updated_at": -1}},
        {
            "$project": {
                "_id": 0,
                "id": 1,
                "company_id": 1,
                "customer_email": 1,
                "customer_name": 1,
                "subject": 1,
                "status": 1,
                "unseen_count": 1,
                "created_at": 1,
                "updated_at": 1,
                "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                "type": {"$literal": "ticket"},
            }
        },
    ]
    tickets = await db.email_tickets.aggregate(ticket_pipeline).to_list(length=None)

    # 3. Merge and sort by updated_at descending
    merged = chats + tickets
    merged.sort(key=lambda x: x.get("updated_at", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    # 4. Apply pagination
    return merged[skip : skip + limit]


async def count_resolved_items(company_id: str) -> int:
    """Count total resolved items (non-escalated chats + resolved tickets)."""
    chat_count = await db.widget_conversations.count_documents(
        {"company_id": company_id, "escalated": {"$ne": True}}
    )
    ticket_count = await db.email_tickets.count_documents(
        {"company_id": company_id, "status": "resolved"}
    )
    return chat_count + ticket_count


async def get_chat_by_id(company_id: str, chat_id: str) -> dict:
    return await db.widget_conversations.find_one(
        {"company_id": company_id, "id": chat_id}, {"_id": 0}
    )


async def mark_chat_seen(company_id: str, chat_id: str) -> bool:
    result = await db.widget_conversations.update_one(
        {"company_id": company_id, "id": chat_id},
        {"$set": {"seen": True}},
    )
    return result.modified_count > 0


async def log_visitor(company_id: str, ip_address: str) -> dict:
    now = datetime.now(tz=timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    existing_visitor = await db.visitors.find_one(
        {
            "company_id": company_id,
            "visitor_id": ip_address,
            "timestamp": {"$gte": today_start},
        }
    )

    if existing_visitor:
        return {"status": "already_logged", "id": existing_visitor["id"]}

    new_id = str(uuid4())
    doc = {
        "id": new_id,
        "company_id": company_id,
        "visitor_id": ip_address,
        "timestamp": now,
        "duration_seconds": 0,
    }
    await db.visitors.insert_one(doc)
    return {"status": "logged", "id": new_id}
