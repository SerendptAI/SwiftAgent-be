from datetime import datetime, timedelta
from app.core.database import db
from app.core.config import settings

async def get_stats(company_id: str) -> dict:
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    # visitor count today
    visitors_today = await db.visitors.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": today_start},
    })

    # chats answered and pending
    chats_answered = await db.conversations.count_documents({
        "company_id": company_id,
        "status": "answered",
        "created_at": {"$gte": today_start},
    })
    chats_pending = await db.conversations.count_documents({
        "company_id": company_id,
        "status": "pending",
        "created_at": {"$gte": today_start},
    })

    # calls today
    calls_today = await db.calls.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": today_start},
    })

    # documents count
    documents_today = await db.documents.count_documents({
        "company_id": company_id,
        "created_at": {"$gte": today_start},
    })

    # scrapes count
    scrapes_today = await db.scrapes.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": today_start},
    })

    return {
        "visitors": {"today": visitors_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
        "chats": {"answered": chats_answered, "pending": chats_pending, "last_7_days_up": 0, "last_7_days_down": 0},
        "calls": {"today": calls_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
        "documents": {"today": documents_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
        "scrapes": {"today": scrapes_today, "percent_change": 0.0, "last_7_days_up": 0, "last_7_days_down": 0},
    }

async def get_visitors(company_id: str, limit: int = 20) -> list:
    cursor = db.visitors.find({"company_id": company_id}).sort("timestamp", -1).limit(limit)
    return await cursor.to_list(length=limit)

async def get_widget_config(company_id: str) -> dict:
    base_url = settings.API_BASE_URL
    embed_code = (
        f'<script src="{base_url}/static/widget/widget.js" '
        f'data-company-id="{company_id}"></script>'
    )
    return {"company_id": company_id, "embed_code": embed_code}
