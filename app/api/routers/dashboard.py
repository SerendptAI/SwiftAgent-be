from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from app.core.auth import get_current_user
from app.core.database import db
from app.models.dashboard_models import (
    DashboardStats,
    VisitorRecord,
    ChatSession,
    ChatSessionSummary,
    VisitorEventCreate,
)
from app.services import dashboard_service, company_service
from app.core.config import settings

router = APIRouter(tags=["Dashboard"])


@router.get("/{company_id}/stats", response_model=DashboardStats)
async def get_dashboard_stats(
    company_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get dashboard stat cards for a company."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await dashboard_service.get_stats(company_id)


@router.get("/{company_id}/visitors")
async def get_visitors(
    company_id: str,
    limit: int = Query(
        default=settings.DEFAULT_PAGE_LIMIT, ge=1, le=settings.MAX_PAGE_LIMIT
    ),
    skip: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """Get recent visitor records for a company."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    visitors = await dashboard_service.get_visitors(company_id, limit)
    total = await db.visitors.count_documents({"company_id": company_id})
    return {
        "items": visitors,
        "total": total,
        "limit": limit,
        "skip": skip,
        "has_next": (skip + len(visitors)) < total,
    }


@router.get("/{company_id}/chats")
async def get_chats(
    company_id: str,
    limit: int = Query(default=50, ge=1, le=settings.MAX_PAGE_LIMIT),
    skip: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """Get resolved items: non-escalated chats + resolved tickets (Resolved section)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    chats = await dashboard_service.get_chats(company_id, limit, skip)
    total = await dashboard_service.count_resolved_items(company_id)
    return {
        "items": chats,
        "total": total,
        "limit": limit,
        "skip": skip,
        "has_next": (skip + len(chats)) < total,
    }


@router.get("/{company_id}/chats/{chat_id}", response_model=ChatSession)
async def get_chat_by_id(
    company_id: str,
    chat_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get the full history of a specific chat session."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    chat = await dashboard_service.get_chat_by_id(company_id, chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat session not found")
    return chat


@router.patch("/{company_id}/chats/{chat_id}/seen")
async def mark_chat_seen(
    company_id: str,
    chat_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Mark a chat session as seen."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    success = await dashboard_service.mark_chat_seen(company_id, chat_id)
    if not success:
        chat = await dashboard_service.get_chat_by_id(company_id, chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Chat session not found")
    return {"status": "success"}


@router.post("/{company_id}/visitors/log")
async def log_visitor(
    company_id: str,
    payload: VisitorEventCreate,
):
    """Log a unique visitor by IP address (unprotected public endpoint)."""
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    result = await dashboard_service.log_visitor(company_id, payload.ip_address)
    return result
