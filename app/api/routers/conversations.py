from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from app.core.auth import get_current_user
from app.core.rbac import require_permission
from app.models.conversation_models import (
    ConversationCreate,
    ConversationResponse,
    ConversationMessage,
)
from app.core.database import get_database
from app.core.config import settings

router = APIRouter(tags=["Conversations"])


@router.post("/", response_model=ConversationResponse, status_code=201)
async def create_conversation(
    conversation: ConversationCreate,
    current_user: dict = Depends(require_permission("tickets:read")),
    db=Depends(get_database),
):
    """Save a new conversation."""
    user_id = current_user["user_id"]

    doc = {
        "id": str(uuid4()),
        "user_id": user_id,
        "messages": [m.model_dump() for m in conversation.messages],
        "created_at": datetime.now(tz=timezone.utc),
        "updated_at": datetime.now(tz=timezone.utc),
    }

    await db.conversations.insert_one(doc)
    return doc


@router.get("/")
async def list_conversations(
    current_user: dict = Depends(require_permission("conversations:read")),
    db=Depends(get_database),
    limit: int = Query(
        default=settings.DEFAULT_CONVERSATION_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT
    ),
    skip: int = Query(default=0, ge=0),
):
    """List all conversations for the current user."""
    user_id = current_user["user_id"]
    cursor = (
        db.conversations.find({"user_id": user_id})
        .sort("updated_at", -1)
        .skip(skip)
        .limit(limit)
    )
    conversations = await cursor.to_list(length=limit)

    total = await db.conversations.count_documents({"user_id": user_id})
    return {
        "items": conversations,
        "total": total,
        "limit": limit,
        "skip": skip,
        "has_next": (skip + len(conversations)) < total,
    }


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    current_user: dict = Depends(require_permission("tickets:read")),
    db=Depends(get_database),
):
    """Retrieve a specific conversation by ID."""
    user_id = current_user["user_id"]
    conversation = await db.conversations.find_one(
        {"id": conversation_id, "user_id": user_id}
    )

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation
