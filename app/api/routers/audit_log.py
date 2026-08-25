"""Company-scoped audit log APIs (admin only)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.auth import get_current_user
from app.services import audit_service, company_service

router = APIRouter(tags=["Audit Log"])

CurrentUser = Annotated[dict, Depends(get_current_user)]


async def _require_admin(company_id: str, current_user: dict) -> dict:
    company = await company_service.get_company(
        company_id, current_user["user_id"], admin_only=True
    )
    if not company:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _clean(entry: dict) -> dict:
    cleaned = dict(entry)
    cleaned.pop("_id", None)
    return cleaned


@router.get("/companies/{company_id}/audit-log")
async def list_audit_events(
    company_id: str,
    current_user: CurrentUser,
    actor_id: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    resource_type: Annotated[str | None, Query()] = None,
    outcome: Annotated[str | None, Query(pattern="^(success|failure)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    skip: Annotated[int, Query(ge=0)] = 0,
):
    await _require_admin(company_id, current_user)
    result = await audit_service.list_events(
        company_id,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        outcome=outcome,
        limit=limit,
        skip=skip,
    )
    return result


@router.get(
    "/companies/{company_id}/audit-log/{event_id}",
    status_code=status.HTTP_200_OK,
)
async def get_audit_event(
    company_id: str, event_id: str, current_user: CurrentUser
):
    await _require_admin(company_id, current_user)
    event = await audit_service.get_event(company_id, event_id)
    if not event:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Audit event not found")
    return event
