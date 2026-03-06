from fastapi import APIRouter, Depends, HTTPException
from typing import List
from app.core.auth import get_current_user
from app.models.dashboard_models import DashboardStats, VisitorRecord, ChatSession, ChatSessionSummary
from app.services import dashboard_service, company_service

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

@router.get("/{company_id}/visitors", response_model=List[VisitorRecord])
async def get_visitors(
    company_id: str,
    limit: int = 20,
    current_user: dict = Depends(get_current_user),
):
    """Get recent visitor records for a company."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await dashboard_service.get_visitors(company_id, limit)

@router.get("/{company_id}/chats", response_model=List[ChatSessionSummary])
async def get_chats(
    company_id: str,
    limit: int = 50,
    skip: int = 0,
    current_user: dict = Depends(get_current_user),
):
    """Get a list of chat sessions (without messages) for a company."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await dashboard_service.get_chats(company_id, limit, skip)

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


