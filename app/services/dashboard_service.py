from datetime import datetime, timedelta, timezone
from uuid import uuid4
from app.core.database import db


def _parse_ts(value):
    """Parse a message timestamp (ISO string or datetime) into an aware datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


# A gap larger than this between consecutive messages is treated as the visitor
# leaving and coming back — the idle time is excluded from communication duration.
_IDLE_GAP_SECONDS = 15 * 60


def _duration_from_timestamps(timestamps) -> int:
    """Communication duration in seconds (sessionized).

    Messages are split into sessions whenever the gap between two consecutive
    messages exceeds ``_IDLE_GAP_SECONDS``; the duration is the sum of each
    session's span. This excludes long idle gaps (e.g. a visitor leaving for an
    hour and returning) so the number reflects active talk-time, not wall-clock.

    Returns 0 when there are fewer than two parseable timestamps.
    """
    parsed = sorted(p for p in (_parse_ts(t) for t in (timestamps or [])) if p)
    if len(parsed) < 2:
        return 0

    total = 0
    session_start = parsed[0]
    prev = parsed[0]
    for ts in parsed[1:]:
        if (ts - prev).total_seconds() > _IDLE_GAP_SECONDS:
            # Idle gap — close the current session, start a new one.
            total += int((prev - session_start).total_seconds())
            session_start = ts
        prev = ts
    total += int((prev - session_start).total_seconds())
    return max(0, total)


async def get_stats(company_id: str) -> dict:
    now = datetime.now(tz=timezone.utc)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    last_7_days_start = now - timedelta(days=7)
    previous_7_days_start = last_7_days_start - timedelta(days=7)

    async def get_stat_group(collection, date_field, extra_match=None):
        match_stage = {"company_id": company_id}
        if extra_match:
            match_stage.update(extra_match)

        pipeline = [
            {"$match": match_stage},
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

    chats_stat = await get_stat_group(db.widget_conversations, "created_at", extra_match={"messages.0": {"$exists": True}})
    pending_chats = await db.widget_conversations.count_documents(
        {
            "company_id": company_id,
            "seen": {"$ne": True},
            "messages.0": {"$exists": True},
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
    visitors = await cursor.to_list(length=limit)
    if not visitors:
        return visitors

    # Populate communication duration per visitor: total span of all
    # conversations linked to that visitor's IP (widget_conversations.visitor_ip).
    ips = list({v.get("visitor_id") for v in visitors if v.get("visitor_id")})
    duration_by_ip: dict[str, int] = {}
    if ips:
        convo_cursor = db.widget_conversations.find(
            {"company_id": company_id, "visitor_ip": {"$in": ips}},
            {"_id": 0, "visitor_ip": 1, "messages.timestamp": 1},
        )
        async for convo in convo_cursor:
            ip = convo.get("visitor_ip")
            if not ip:
                continue
            span = _duration_from_timestamps(
                [m.get("timestamp") for m in convo.get("messages", [])]
            )
            duration_by_ip[ip] = duration_by_ip.get(ip, 0) + span

    for v in visitors:
        v["duration_seconds"] = duration_by_ip.get(
            v.get("visitor_id"), v.get("duration_seconds", 0)
        )
    return visitors


async def get_chats(company_id: str, limit: int = 50, skip: int = 0) -> list:
    """Return resolved tickets only (Resolved section).
    
    Note: Escalated chats (now tickets) are excluded; admins view them via ticket listing.
    """

    # NOTE: escalated chats excluded intentionally—they now have corresponding tickets
    # 1. Non-escalated chats ONLY
    chat_pipeline = [
        {"$match": {"company_id": company_id, "escalated": {"$ne": True}, "messages.0": {"$exists": True}}},
        {"$sort": {"updated_at": -1}},
        {
            "$project": {
                "_id": 0,
                "id": {"$ifNull": ["$id", "$session_id"]},
                "company_id": 1,
                "session_id": 1,
                "created_at": 1,
                "updated_at": 1,
                "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                "message_timestamps": "$messages.timestamp",
                "preview_message": {
                    "$let": {
                        "vars": {
                            "user_msgs": {
                                "$filter": {
                                    "input": {"$ifNull": ["$messages", []]},
                                    "as": "msg",
                                    "cond": {"$eq": ["$$msg.role", "user"]}
                                }
                            }
                        },
                        "in": {"$arrayElemAt": ["$$user_msgs.content", -1]}
                    }
                },
                "seen": {"$ifNull": ["$seen", False]},
                "avatar": {"$ifNull": ["$avatar", "/chat-avatars/newimg.svg"]},
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
            "$lookup": {
                "from": "widget_conversations",
                "localField": "chat_session_id",
                "foreignField": "session_id",
                "as": "attributed_chat_docs"
            }
        },
        {
            "$addFields": {
                "last_ticket_msg": {"$arrayElemAt": ["$messages", -1]},
                "attr_chat_doc": {"$arrayElemAt": ["$attributed_chat_docs", 0]}
            }
        },
        {
            "$project": {
                "_id": 0,
                "id": 1,
                "company_id": 1,
                "session_id": {"$ifNull": ["$customer_name", {"$ifNull": ["$customer_email", "$id"]}]},
                "customer_email": 1,
                "customer_name": 1,
                "subject": 1,
                "status": 1,
                "unseen_count": 1,
                "created_at": 1,
                "updated_at": 1,
                "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                "message_timestamps": "$messages.timestamp",
                "preview_message": {
                    "$cond": {
                        "if": {"$eq": ["$last_ticket_msg.direction", "system"]},
                        "then": {
                            "$let": {
                                "vars": {
                                    "user_msgs": {
                                        "$filter": {
                                            "input": {"$ifNull": ["$attr_chat_doc.messages", []]},
                                            "as": "msg",
                                            "cond": {"$eq": ["$$msg.role", "user"]}
                                        }
                                    }
                                },
                                "in": {
                                    "$ifNull": [
                                        {"$arrayElemAt": ["$$user_msgs.content", -1]},
                                        "$last_ticket_msg.body_text"
                                    ]
                                }
                            }
                        },
                        "else": "$last_ticket_msg.body_text"
                    }
                },
                "seen": {"$literal": True},
                "avatar": {"$ifNull": ["$avatar", "/chat-avatars/newimg.svg"]},
                "type": {"$literal": "ticket"},
            }
        },
    ]
    tickets = await db.email_tickets.aggregate(ticket_pipeline).to_list(length=None)

    # 3. Merge and sort by updated_at descending
    merged = chats + tickets

    def _normalize_dt(dt):
        if not dt:
            return datetime.min.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    merged.sort(key=lambda x: _normalize_dt(x.get("updated_at")), reverse=True)

    # 4. Apply pagination
    page = merged[skip : skip + limit]

    # 5. Compute communication duration (span of message timestamps) per item
    for item in page:
        item["duration_seconds"] = _duration_from_timestamps(
            item.pop("message_timestamps", [])
        )
    return page


async def count_resolved_items(company_id: str) -> int:
    """Count total resolved items (non-escalated chats + resolved tickets)."""
    chat_count = await db.widget_conversations.count_documents(
        {"company_id": company_id, "escalated": {"$ne": True}, "messages.0": {"$exists": True}}
    )
    ticket_count = await db.email_tickets.count_documents(
        {"company_id": company_id, "status": "resolved"}
    )
    return chat_count + ticket_count


def _normalize_ticket_to_chat_session(ticket: dict, company_id: str, attributed_chat: dict | None = None) -> dict:
    """Convert a ticket document (+ optional attributed chat) into ChatSession shape.

    Maps ticket messages and attributed chat messages into the unified
    ``{role, content, timestamp}`` format so the frontend renders them
    identically to regular widget chats.
    """
    messages: list[dict] = []

    # 1. Prepend attributed chat messages (the original widget conversation)
    if attributed_chat:
        for msg in attributed_chat.get("messages", []):
            messages.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
                "timestamp": msg.get("timestamp"),
            })

    # 2. Append ticket email messages
    _DIRECTION_TO_ROLE = {
        "inbound": "user",
        "outbound": "assistant",
        "system": "assistant",
    }
    for msg in ticket.get("messages", []):
        role = _DIRECTION_TO_ROLE.get(msg.get("direction", ""), "user")
        content = msg.get("body_text", "")
        ts = msg.get("timestamp")
        messages.append({
            "role": role,
            "content": content,
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else ts,
        })

    return {
        "id": ticket["id"],
        "company_id": company_id,
        "session_id": ticket.get("customer_name") or ticket.get("customer_email") or ticket["id"],
        "customer_name": ticket.get("customer_name"),
        "customer_email": ticket.get("customer_email"),
        "subject": ticket.get("subject"),
        "created_at": ticket.get("created_at", datetime.now(tz=timezone.utc)),
        "updated_at": ticket.get("updated_at", datetime.now(tz=timezone.utc)),
        "messages": messages,
        "avatar": ticket.get("avatar") or "/chat-avatars/newimg.svg",
        "seen": True,
    }


async def get_chat_by_id(company_id: str, chat_id: str) -> dict:
    # Try widget_conversations first
    result = await db.widget_conversations.find_one(
        {
            "company_id": company_id,
            "$or": [{"id": chat_id}, {"session_id": chat_id}]
        }, 
        {"_id": 0}
    )
    if result:
        result["duration_seconds"] = _duration_from_timestamps(
            [m.get("timestamp") for m in result.get("messages", [])]
        )
        return result

    # Fall back to resolved ticket
    ticket = await db.email_tickets.find_one(
        {"company_id": company_id, "id": chat_id}, {"_id": 0}
    )
    if not ticket:
        return None

    # If the ticket was escalated from a chat, fetch the attributed chat
    attributed_chat = None
    if ticket.get("chat_session_id"):
        attributed_chat = await db.widget_conversations.find_one(
            {"company_id": company_id, "session_id": ticket["chat_session_id"]},
            {"_id": 0},
        )

    session = _normalize_ticket_to_chat_session(ticket, company_id, attributed_chat)
    session["duration_seconds"] = _duration_from_timestamps(
        [m.get("timestamp") for m in session.get("messages", [])]
    )
    return session


async def mark_chat_seen(company_id: str, chat_id: str) -> bool:
    result = await db.widget_conversations.update_one(
        {"company_id": company_id, "id": chat_id},
        {"$set": {"seen": True}},
    )
    return result.matched_count > 0


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
