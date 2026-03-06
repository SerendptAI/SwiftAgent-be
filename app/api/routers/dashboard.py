from fastapi import APIRouter, Depends, HTTPException
from typing import List
from app.core.auth import get_current_user
from app.models.dashboard_models import DashboardStats, VisitorRecord
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


