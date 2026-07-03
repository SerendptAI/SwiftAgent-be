"""
SDK Business Logic Service.

Handles: SDK initialization (user upsert), conversation listing, and detail fetching.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.database import db

def _ensure_dt(val: Any) -> datetime:
    if isinstance(val, datetime):
        # Ensure it's timezone-aware (assume UTC if naive)
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            pass
    # fallback
    return datetime.min.replace(tzinfo=timezone.utc)

def format_chat_session_dict(c: dict) -> dict:
    """Format a chat session document into the SdkConversationItem dictionary format."""
    messages = c.get("messages", [])
    last_msg = messages[-1].get("content") if messages and isinstance(messages[-1], dict) else None
    
    subject = c.get("subject")
    if not subject:
        first_user_msg = next((m.get("content") for m in messages if m.get("role") == "user"), "")
        subject = (first_user_msg[:50] + "...") if len(first_user_msg) > 50 else first_user_msg
    if not subject:
        subject = "New Chat"

    return {
        "id": c.get("session_id"),
        "type": "chat",
        "subject": subject,
        "last_message": last_msg,
        "resolved": False,
        "resolved_at": None,
        "message_count": len(messages),
        "created_at": _ensure_dt(c.get("created_at")),
        "updated_at": _ensure_dt(c.get("updated_at")),
    }

async def init_sdk_user(company_id: str, email: str) -> dict:
    """
    Find or create an SDK user record.
    Returns: {"email": str, "is_new": bool}
    """
    now = datetime.now(tz=timezone.utc)
    
    # Try to find existing
    existing = await db.sdk_users.find_one({"company_id": company_id, "email": email})
    
    if existing:
        # Update last_seen
        await db.sdk_users.update_one(
            {"_id": existing["_id"]},
            {"$set": {"last_seen_at": now}}
        )
        return {"email": email, "is_new": False}
    
    # Create new
    doc = {
        "company_id": company_id,
        "email": email,
        "created_at": now,
        "last_seen_at": now,
    }
    await db.sdk_users.insert_one(doc)
    return {"email": email, "is_new": True}


async def get_conversation_history(
    company_id: str, email: str, limit: int = 20, cursor: Optional[str] = None
) -> dict:
    """
    Unified list of a user's interactions: chats (widget_conversations) + tickets (email_tickets).
    """
    cursor_filter = {}
    if cursor:
        try:
            # Parse cursor as ISO datetime string for updated_at filtering
            cursor_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
            cursor_filter = {"updated_at": {"$lt": cursor_dt}}
        except ValueError:
            pass # Invalid cursor, ignore

    # 1. Fetch Chat Sessions
    chat_query = {
        "company_id": company_id, 
        "sdk_user_email": email,
        "escalated": {"$ne": True}, # exclude chats that became tickets
        **cursor_filter
    }
    chats_cursor = db.widget_conversations.find(chat_query).sort("updated_at", -1).limit(limit)
    raw_chats = await chats_cursor.to_list(length=limit)

    # 2. Fetch Tickets
    ticket_query = {
        "company_id": company_id,
        "customer_email": email,
        **cursor_filter
    }
    tickets_cursor = db.email_tickets.find(ticket_query).sort("updated_at", -1).limit(limit)
    raw_tickets = await tickets_cursor.to_list(length=limit)

    # 3. Normalize & Merge
    items: List[Dict[str, Any]] = []

    for c in raw_chats:
        items.append(format_chat_session_dict(c))

    # Fetch linked chat subjects to preserve original titles
    session_ids = [t.get("chat_session_id") for t in raw_tickets if t.get("chat_session_id")]
    linked_chats = {}
    if session_ids:
        chats = await db.widget_conversations.find({"session_id": {"$in": session_ids}}).to_list(None)
        for c in chats:
            linked_chats[c.get("session_id")] = c.get("subject")

    for t in raw_tickets:
        messages = t.get("messages", [])
        last_msg = messages[-1].get("body_text") if messages and isinstance(messages[-1], dict) else None
        
        chat_session_id = t.get("chat_session_id")
        original_subject = linked_chats.get(chat_session_id) if chat_session_id else None

        items.append({
            "id": chat_session_id or t.get("id"),
            "type": "ticket",
            "subject": original_subject or t.get("subject"),
            "last_message": last_msg,
            "resolved": t.get("status") == "resolved",
            "resolved_at": t.get("resolved_at"),
            "message_count": len(messages),
            "created_at": _ensure_dt(t.get("created_at")),
            "updated_at": _ensure_dt(t.get("updated_at")),
        })

    # Sort merged list descending by updated_at
    items.sort(key=lambda x: x["updated_at"], reverse=True)

    # Truncate to limit
    items = items[:limit]

    # Generate next_cursor
    next_cursor = None
    if items:
        next_cursor = items[-1]["updated_at"].isoformat()

    # Determine has_next (approximate: if we fetched limit items, there MIGHT be more)
    # A true has_next requires checking count > limit, but this is standard for cursor pagination
    has_next = len(items) == limit

    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_next": has_next
    }


async def get_conversation_detail(company_id: str, email: str, conversation_id: str) -> Optional[dict]:
    """
    Fetch the full detail for a conversation, resolving against both chats and tickets.
    """
    # 1. Try Chat Session first
    chat = await db.widget_conversations.find_one({
        "company_id": company_id,
        "sdk_user_email": email,
        "session_id": conversation_id
    })
    
    if chat and not chat.get("escalated"):
        company = await db.companies.find_one({"id": company_id})
        ai_name = company.get("name") if company else "AI Assistant"
        ai_avatar_url = company.get("logo_url") if company else None

        messages = chat.get("messages", [])
        formatted_messages = []
        for m in messages:
            role = m.get("role", "user")
            is_human_agent = bool(m.get("agent_name"))
            formatted_messages.append({
                "id": m.get("id"),
                "role": role,
                "content": m.get("content", ""),
                "timestamp": m.get("timestamp"),
                "attachments": m.get("attachments"),
                "author_name": m.get("agent_name") if is_human_agent else (ai_name if role == "assistant" else None),
                "avatar_url": m.get("agent_avatar_url") if is_human_agent else (ai_avatar_url if role == "assistant" else None),
                "author_type": ("agent" if is_human_agent else "ai") if role == "assistant" else "user",
            })
        
        subject = chat.get("subject")
        if not subject:
            first_user_msg = next((m.get("content") for m in messages if m.get("role") == "user"), "")
            subject = (first_user_msg[:50] + "...") if len(first_user_msg) > 50 else first_user_msg
        if not subject:
            subject = "New Chat"

        return {
            "id": chat.get("session_id"),
            "type": "chat",
            "subject": subject,
            "resolved": False,
            "resolved_at": None,
            "messages": formatted_messages,
            "created_at": chat.get("created_at"),
            "updated_at": chat.get("updated_at"),
        }

    # 2. Try Ticket Session
    ticket = await db.email_tickets.find_one({
        "company_id": company_id,
        "customer_email": email,
        "$or": [
            {"id": conversation_id},
            {"chat_session_id": conversation_id}
        ]
    })

    if ticket:
        messages = ticket.get("messages", [])
        formatted_messages = [
            {
                "id": m.get("id"),
                "role": m.get("direction", "user"), # inbound/outbound/system
                "content": m.get("body_text", ""),
                "timestamp": m.get("timestamp").isoformat() if isinstance(m.get("timestamp"), datetime) else m.get("timestamp"),
                "attachments": m.get("attachments"),
                "author_name": m.get("agent_name"),
                "avatar_url": m.get("agent_avatar_url"),
                "author_type": "agent" if m.get("direction") == "outbound" else "user",
            }
            for m in messages
        ]
        
        # Attach attributed chat if this ticket was escalated
        attributed_chat = []
        original_subject = None
        if ticket.get("chat_session_id"):
             linked_chat = await db.widget_conversations.find_one({
                 "company_id": company_id,
                 "session_id": ticket.get("chat_session_id")
             })
             if linked_chat:
                 company = await db.companies.find_one({"id": company_id})
                 ai_name = company.get("name") if company else "AI Assistant"
                 ai_avatar_url = company.get("logo_url") if company else None
                 original_subject = linked_chat.get("subject")

                 for m in linked_chat.get("messages", []):
                     role = m.get("role", "user")
                     is_human_agent = bool(m.get("agent_name"))
                     attributed_chat.append({
                         "id": m.get("id"),
                         "role": role,
                         "content": m.get("content", ""),
                         "timestamp": m.get("timestamp").isoformat() if isinstance(m.get("timestamp"), datetime) else m.get("timestamp"),
                         "attachments": m.get("attachments"),
                         "author_name": m.get("agent_name") if is_human_agent else (ai_name if role == "assistant" else None),
                         "avatar_url": m.get("agent_avatar_url") if is_human_agent else (ai_avatar_url if role == "assistant" else None),
                         "author_type": ("agent" if is_human_agent else "ai") if role == "assistant" else "user",
                     })

        # Merge attributed_chat (AI/widget messages) with formatted_messages (ticket emails)
        # into a single chronological array for seamless UI rendering
        merged_messages = attributed_chat + formatted_messages
        # Ensure they are sorted chronologically
        merged_messages.sort(key=lambda x: x.get("timestamp") or "")

        return {
            "id": ticket.get("chat_session_id") or ticket.get("id"),
            "type": "ticket",
            "subject": original_subject or ticket.get("subject"),
            "resolved": ticket.get("status") == "resolved",
            "resolved_at": ticket.get("resolved_at"),
            "messages": merged_messages,
            "attributed_chat": None,
            "created_at": ticket.get("created_at"),
            "updated_at": ticket.get("updated_at"),
        }

    # Not found in either
    return None
