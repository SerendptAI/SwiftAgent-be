"""
API router for Company API Integrations.

Allows company admins to register, manage, and test their internal APIs.
These are then available to the agent as read-only verification tools.
"""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks

from app.core.auth import get_current_user
from app.core.rbac import require_permission
from app.models.integration_models import (
    IntegrationCreate,
    IntegrationUpdate,
    IntegrationResponse,
)
from app.services import company_service, integration_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Integrations"])


async def _require_company_admin(company_id: str, user_id: str) -> dict:
    """Verify the user is the company admin."""
    company = await company_service.get_company(company_id, user_id, admin_only=True)
    if not company:
        raise HTTPException(
            status_code=404,
            detail="Company not found or you don't have admin access.",
        )
    return company


@router.post("/", response_model=IntegrationResponse, status_code=201)
async def create_integration(
    company_id: str,
    data: IntegrationCreate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_permission("tickets:read")),
):
    """Register a new API integration. The API key is encrypted before storage."""
    await _require_company_admin(company_id, current_user["user_id"])

    # Check for duplicate name
    existing = await integration_service.get_integration_summaries(company_id)
    if any(i["name"].lower() == data.name.lower() for i in existing):
        raise HTTPException(
            status_code=409,
            detail=f"An integration named '{data.name}' already exists. Please use a different name.",
        )

    result = await integration_service.create_integration(
        company_id, data.model_dump()
    )

    if data.documentation_url:
        background_tasks.add_task(
            integration_service.scrape_and_update_documentation,
            company_id=company_id,
            integration_id=result["id"],
            url=data.documentation_url
        )

    return result


@router.get("/", response_model=List[IntegrationResponse])
async def list_integrations(
    company_id: str,
    current_user: dict = Depends(require_permission("tickets:read")),
):
    """List all active API integrations for the company."""
    await _require_company_admin(company_id, current_user["user_id"])
    return await integration_service.list_integrations(company_id)


@router.get("/{integration_id}", response_model=IntegrationResponse)
async def get_integration(
    company_id: str,
    integration_id: str,
    current_user: dict = Depends(require_permission("tickets:read")),
):
    """Get details for a single integration."""
    await _require_company_admin(company_id, current_user["user_id"])
    doc = await integration_service.get_integration(company_id, integration_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Integration not found.")
    return doc


@router.patch("/{integration_id}", response_model=IntegrationResponse)
async def update_integration(
    company_id: str,
    integration_id: str,
    data: IntegrationUpdate,
    background_tasks: BackgroundTasks,
    current_user: dict = Depends(require_permission("tickets:read")),
):
    """Update an integration. If a new API key is provided, it will be re-encrypted."""
    await _require_company_admin(company_id, current_user["user_id"])

    result = await integration_service.update_integration(
        company_id, integration_id, data.model_dump(exclude_none=True)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Integration not found.")

    if data.documentation_url:
        background_tasks.add_task(
            integration_service.scrape_and_update_documentation,
            company_id=company_id,
            integration_id=result["id"],
            url=data.documentation_url
        )

    return result


@router.delete("/{integration_id}")
async def delete_integration(
    company_id: str,
    integration_id: str,
    current_user: dict = Depends(require_permission("integrations:write")),
):
    """Deactivate an integration (soft delete)."""
    await _require_company_admin(company_id, current_user["user_id"])

    success = await integration_service.delete_integration(company_id, integration_id)
    if not success:
        raise HTTPException(status_code=404, detail="Integration not found or already deactivated.")
    return {"status": "deactivated"}


@router.post("/{integration_id}/test")
async def test_integration(
    company_id: str,
    integration_id: str,
    endpoint_name: str,
    current_user: dict = Depends(require_permission("tickets:read")),
):
    """
    Test-fire a GET endpoint to verify the integration works.

    Returns the raw API response for the company admin to inspect.
    """
    await _require_company_admin(company_id, current_user["user_id"])

    doc = await integration_service.get_integration(company_id, integration_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Integration not found.")

    result = await integration_service.execute_get_request(
        company_id=company_id,
        integration_name=doc["name"],
        endpoint_name=endpoint_name,
    )

    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    return result
