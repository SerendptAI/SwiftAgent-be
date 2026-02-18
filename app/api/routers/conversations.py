from fastapi import APIRouter, Depends, HTTPException
from typing import List
from app.core.auth import get_current_user
from app.models.conversation_models import ConversationCreate, ConversationResponse, ConversationMessage
from app.core.database import get_database
from datetime import datetime
from uuid import uuid4

router = APIRouter(tags=["Conversations"])

@router.post("/", response_model=ConversationResponse)
async def create_conversation(
    conversation: ConversationCreate,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_database)
):
    """Save a new conversation."""
    user_id = current_user["user_id"]

    doc = {
        "id": str(uuid4()),
        "user_id": user_id,
        "messages": [m.model_dump() for m in conversation.messages],
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    await db.conversations.insert_one(doc)
    return doc

@router.get("/", response_model=List[ConversationResponse])
async def list_conversations(
    current_user: dict = Depends(get_current_user),
    db = Depends(get_database)
):
    """List all conversations for the current user."""
    user_id = current_user["user_id"]
    cursor = db.conversations.find({"user_id": user_id}).sort("updated_at", -1)
    conversations = await cursor.to_list(length=50)
    return conversations

@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
    db = Depends(get_database)
):
    """Retrieve a specific conversation by ID."""
    user_id = current_user["user_id"]
    conversation = await db.conversations.find_one({"id": conversation_id, "user_id": user_id})

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return conversation
