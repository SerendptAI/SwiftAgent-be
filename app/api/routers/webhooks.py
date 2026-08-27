"""Webhook management APIs (admin only)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth import get_current_user
from app.models.webhook_models import WebhookEndpointCreate, WebhookEndpointUpdate
from app.services import company_service, webhook_service

router = APIRouter(tags=["Webhooks"])

CurrentUser = Annotated[dict, Depends(get_current_user)]


async def _require_admin(company_id: str, current_user: dict) -> dict:
    company = await company_service.get_company(
        company_id, current_user["user_id"], admin_only=True
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/companies/{company_id}/webhooks")
async def list_webhooks(company_id: str, current_user: CurrentUser):
    await _require_admin(company_id, current_user)
    return {"items": await webhook_service.list_endpoints(company_id)}


@router.post(
    "/companies/{company_id}/webhooks",
    status_code=status.HTTP_201_CREATED,
)
async def create_webhook(
    company_id: str,
    payload: WebhookEndpointCreate,
    current_user: CurrentUser,
):
    await _require_admin(company_id, current_user)
    endpoint = await webhook_service.create_endpoint(
        company_id,
        str(payload.url),
        payload.events,
        payload.description,
    )
    return endpoint


@router.patch("/companies/{company_id}/webhooks/{endpoint_id}")
async def update_webhook(
    company_id: str,
    endpoint_id: str,
    payload: WebhookEndpointUpdate,
    current_user: CurrentUser,
):
    await _require_admin(company_id, current_user)
    changes = payload.model_dump(exclude_none=True)
    if "url" in changes:
        changes["url"] = str(payload.url)
    endpoint = await webhook_service.update_endpoint(company_id, endpoint_id, changes)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return endpoint


@router.delete("/companies/{company_id}/webhooks/{endpoint_id}", status_code=204)
async def delete_webhook(company_id: str, endpoint_id: str, current_user: CurrentUser):
    await _require_admin(company_id, current_user)
    deleted = await webhook_service.delete_endpoint(company_id, endpoint_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Webhook not found")


@router.post("/companies/{company_id}/webhooks/{endpoint_id}/rotate-secret")
async def rotate_webhook_secret(company_id: str, endpoint_id: str, current_user: CurrentUser):
    await _require_admin(company_id, current_user)
    endpoint = await webhook_service.rotate_secret(company_id, endpoint_id)
    if not endpoint:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return endpoint


@router.get("/companies/{company_id}/webhooks/deliveries")
async def list_webhook_deliveries(
    company_id: str,
    current_user: CurrentUser,
    endpoint_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    skip: Annotated[int, Query(ge=0)] = 0,
):
    await _require_admin(company_id, current_user)
    return await webhook_service.list_deliveries(company_id, endpoint_id, limit, skip)
