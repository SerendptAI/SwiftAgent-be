from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from app.core.auth import get_current_user
from app.models.billing_models import BillingDetailsResponse, SubscribeRequest
from app.services import billing_service, company_service

router = APIRouter(tags=["Billing"])


@router.get("/{company_id}", response_model=BillingDetailsResponse)
async def get_billing_details(
    company_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get the current billing subscription plan and saved cards for a company."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id, admin_only=True)
    if not company:
        raise HTTPException(status_code=403, detail="Company not found or unauthorized for billing")

    details = await billing_service.get_billing_details(company_id, user_id)
    if not details:
        raise HTTPException(status_code=404, detail="Billing details not found")

    return details


@router.post("/{company_id}/subscribe", response_model=BillingDetailsResponse)
async def subscribe_plan(
    company_id: str,
    data: SubscribeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Subscribe or update a billing plan for the company."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id, admin_only=True)
    if not company:
        raise HTTPException(status_code=403, detail="Company not found or unauthorized for billing")

    details = await billing_service.subscribe(company_id, user_id, data.plan_name)
    if not details:
        raise HTTPException(status_code=400, detail="Failed to subscribe")

    return details
