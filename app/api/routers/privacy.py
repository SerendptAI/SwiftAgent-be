"""Company retention policy APIs (admin only)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth import get_current_user
from app.services import company_service, conversation_privacy_service

router = APIRouter(tags=["Conversation Privacy"])

CurrentUser = Annotated[dict, Depends(get_current_user)]


async def _require_admin(company_id: str, current_user: dict) -> dict:
    company = await company_service.get_company(
        company_id, current_user["user_id"], admin_only=True
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


class RetentionPolicyUpdate(BaseModel):
    retention_days: int = Field(ge=1, le=3650)
    expired_action: str = Field(pattern="^(delete|anonymize|retain)$")
    redact_pii: bool = True


@router.get("/companies/{company_id}/privacy/retention")
async def get_retention_policy(company_id: str, current_user: CurrentUser):
    await _require_admin(company_id, current_user)
    return await conversation_privacy_service.get_retention_config(company_id)


@router.put("/companies/{company_id}/privacy/retention")
async def update_retention_policy(
    company_id: str,
    payload: RetentionPolicyUpdate,
    current_user: CurrentUser,
):
    await _require_admin(company_id, current_user)
    await company_service._update_and_return(
        company_id,
        current_user["user_id"],
        {"retention_policy": payload.model_dump()},
        admin_only=True,
    )
    return await conversation_privacy_service.get_retention_config(company_id)


@router.post("/companies/{company_id}/privacy/retention/sweep")
async def run_retention_sweep(company_id: str, current_user: CurrentUser):
    await _require_admin(company_id, current_user)
    totals = await conversation_privacy_service.apply_retention_sweep(company_id)
    return {"company_id": company_id, **totals}
