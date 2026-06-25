"""
Stroll Router — API endpoints for managing dashboard strolls.

Endpoints:
- GET    /{company_id}/config          — get stroll configuration
- PUT    /{company_id}/config          — create/update stroll config
- POST   /{company_id}/run             — trigger a manual stroll
- GET    /{company_id}/versions        — list stroll versions
- GET    /{company_id}/versions/{vid}  — get specific version
- GET    /{company_id}/status          — get latest stroll job status
- POST   /{company_id}/find            — query: "where is X?"
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.core.auth import get_current_user
from app.core.database import db
from app.services import stroll_service, company_service
from app.core.plan_enforcement import enforce_agent_limit, enforce_stroll_limit
from app.services.stroll_scheduler import (
    schedule_stroll_job,
    is_stroll_running,
    mark_stroll_running,
    mark_stroll_done,
)
from app.models.stroll_models import StrollConfigCreate, WidgetStrollReport

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Stroll"])


async def _verify_company_access(user_id: str, company_id: str):
    """Verify user has access to the company."""
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or unauthorized")
    return company


async def _run_stroll_background(company_id: str):
    """Run a full stroll cycle in the background: crawl → diff → commit → index."""
    if not mark_stroll_running(company_id):
        logger.warning(f"Stroll already running for company {company_id}, skipping manual run")
        return

    try:
        config = await stroll_service.get_stroll_config(company_id)
        if not config:
            logger.error(f"No stroll config found for company {company_id}")
            return

        # run the crawl
        version = await stroll_service.run_stroll(company_id, config)

        if version.status != "success":
            logger.error(f"Stroll failed for company {company_id}: {version.status}")
            return

        # diff against previous
        prev = await stroll_service.get_latest_version(company_id)
        diff = stroll_service.diff_stroll(version.graph, prev)

        # commit
        committed = await stroll_service.commit_stroll(company_id, version, diff)
        if committed:
            logger.info(f"Stroll completed for company {company_id}: {committed.id}")
        else:
            logger.info(f"Stroll found no changes for company {company_id}")

    except Exception as e:
        logger.exception(f"Background stroll failed for company {company_id}: {e}")
    finally:
        mark_stroll_done(company_id)

@router.get("/{company_id}/config")
async def get_config(company_id: str, user: dict = Depends(get_current_user)):
    """Get the stroll configuration for a company."""
    await _verify_company_access(user["user_id"], company_id)
    config = await stroll_service.get_stroll_config(company_id)
    if not config:
        raise HTTPException(status_code=404, detail="No stroll configuration found. Set up your dashboard URL and credentials first.")
    data = config.model_dump()
    if data.get("credentials") and data["credentials"].get("password"):
        data["credentials"]["password"] = "********"
    return data


@router.put("/{company_id}/config")
async def update_config(
    company_id: str,
    data: StrollConfigCreate,
    user: dict = Depends(get_current_user),
):
    """Create or update the stroll configuration for a company."""
    user_id = user["user_id"]
    
    # Check if creating a new config
    existing_config = await stroll_service.get_stroll_config(company_id)
    if not existing_config:
        company = await company_service.get_company(company_id, user_id)
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")
        await enforce_agent_limit(company)

    config = await stroll_service.save_stroll_config(company_id, data)
    
    schedule_stroll_job(company_id, data.schedule)
    
    data_out = config.model_dump()
    if data_out.get("credentials") and data_out["credentials"].get("password"):
        data_out["credentials"]["password"] = "********"
    return data_out


@router.post("/{company_id}/run")
async def trigger_stroll(
    company_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Trigger a manual stroll (runs in the background)."""
    company = await _verify_company_access(user["user_id"], company_id)
    await enforce_stroll_limit(company)
    
    config = await stroll_service.get_stroll_config(company_id)
    if not config:
        raise HTTPException(
            status_code=400,
            detail="Stroll configuration not set. Use PUT /config first.",
        )

    if is_stroll_running(company_id):
        raise HTTPException(
            status_code=409,
            detail="A stroll is already in progress for this company.",
        )

    background_tasks.add_task(_run_stroll_background, company_id)
    return {"status": "started", "message": "Stroll triggered. Check /status for progress."}


@router.get("/{company_id}/versions")
async def list_versions(
    company_id: str,
    limit: int = 10,
    user: dict = Depends(get_current_user),
):
    """List stroll versions for a company."""
    await _verify_company_access(user["user_id"], company_id)
    versions = await stroll_service.list_versions(company_id, limit=limit)
    return {"versions": versions}


@router.get("/{company_id}/versions/latest")
async def get_latest_stroll_version(
    company_id: str,
    user: dict = Depends(get_current_user),
):
    """Get the latest successful stroll version with full graph data."""
    await _verify_company_access(user["user_id"], company_id)
    version = await stroll_service.get_latest_version(company_id)
    if not version:
        raise HTTPException(status_code=404, detail="No successful stroll version found.")
    return version.model_dump()


@router.get("/{company_id}/versions/{version_id}")
async def get_version(
    company_id: str,
    version_id: str,
    user: dict = Depends(get_current_user),
):
    """Get a specific stroll version with full graph data."""
    await _verify_company_access(user["user_id"], company_id)
    doc = await db.stroll_versions.find_one({
        "company_id": company_id,
        "id": version_id,
    })
    if not doc:
        raise HTTPException(status_code=404, detail="Version not found")
    doc.pop("_id", None)
    return doc


@router.get("/{company_id}/status")
async def get_status(company_id: str, user: dict = Depends(get_current_user)):
    """Get the latest stroll status for a company."""
    await _verify_company_access(user["user_id"], company_id)
    version = await stroll_service.get_latest_version(company_id)
    if not version:
        return {
            "company_id": company_id,
            "status": "idle",
            "last_run": None,
            "last_version_id": None,
        }
    return {
        "company_id": company_id,
        "status": version.status,
        "last_run": version.timestamp.isoformat(),
        "last_version_id": version.id,
    }

@router.get("/{company_id}/documentation")
async def get_documentation(
    company_id: str,
    user: dict = Depends(get_current_user),
):
    """Get the full dashboard documentation derived from the latest stroll graph."""
    await _verify_company_access(user["user_id"], company_id)
    from app.services.stroll_index_service import get_all_navigation_steps
    docs = await get_all_navigation_steps(company_id)
    if not docs:
        raise HTTPException(
            status_code=404,
            detail="Documentation not available. A successful stroll must be run first."
        )
    return docs.model_dump()
