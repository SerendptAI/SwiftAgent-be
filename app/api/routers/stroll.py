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
from app.services import stroll_service, stroll_index_service
from app.services.stroll_scheduler import schedule_stroll_job
from app.models.stroll_models import StrollConfigCreate, WidgetStrollReport

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Stroll"])

class FindFeatureRequest(BaseModel):
    query: str

async def _run_stroll_background(company_id: str):
    """Run a full stroll cycle in the background: crawl → diff → commit → index."""
    try:
        config = await stroll_service.get_stroll_config(company_id)
        if not config:
            logger.error(f"No stroll config found for company {company_id}")
            return

        # run the crawl
        version = await stroll_service.run_stroll(company_id, config)

        if version.status != "success":
            logger.error(f"Stroll failed for company {company_id}: {version.status}")
            # persist failure record
            version.diff = None
            await db.stroll_versions.insert_one(version.model_dump())
            return

        # diff against previous
        prev = await stroll_service.get_latest_version(company_id)
        diff = stroll_service.diff_stroll(version.graph, prev)

        # commit
        committed = await stroll_service.commit_stroll(company_id, version, diff)
        if committed:
            # rebuild search index
            await stroll_index_service.build_index(company_id, committed)
            logger.info(f"Stroll completed for company {company_id}: {committed.id}")
        else:
            logger.info(f"Stroll found no changes for company {company_id}")

    except Exception as e:
        logger.exception(f"Background stroll failed for company {company_id}: {e}")

@router.get("/{company_id}/config")
async def get_config(company_id: str, user: dict = Depends(get_current_user)):
    """Get the stroll configuration for a company."""
    config = await stroll_service.get_stroll_config(company_id)
    if not config:
        raise HTTPException(status_code=404, detail="Stroll configuration not found")
    return config.model_dump()


@router.put("/{company_id}/config")
async def update_config(
    company_id: str,
    data: StrollConfigCreate,
    user: dict = Depends(get_current_user),
):
    """Create or update the stroll configuration for a company."""
    config = await stroll_service.save_stroll_config(company_id, data)
    
    schedule_stroll_job(company_id, data.schedule)
    
    return config.model_dump()


@router.post("/{company_id}/run")
async def trigger_stroll(
    company_id: str,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    """Trigger a manual stroll (runs in the background)."""
    config = await stroll_service.get_stroll_config(company_id)
    if not config:
        raise HTTPException(
            status_code=400,
            detail="Stroll configuration not set. Use PUT /config first.",
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
    versions = await stroll_service.list_versions(company_id, limit=limit)
    return {"versions": versions}


@router.get("/{company_id}/versions/{version_id}")
async def get_version(
    company_id: str,
    version_id: str,
    user: dict = Depends(get_current_user),
):
    """Get a specific stroll version with full graph data."""
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


@router.post("/{company_id}/find")
async def find_feature(
    company_id: str,
    req: FindFeatureRequest,
    user: dict = Depends(get_current_user),
):
    """
    Query: "Where is X?" — returns annotated screenshot step-by-step guide.
    Bypasses the agent, hits the stroll index directly.
    """
    result = await stroll_index_service.find_feature(company_id, req.query)
    if not result:
        raise HTTPException(
            status_code=404,
            detail="Feature not found in dashboard stroll data. Try rephrasing, or wait for the next stroll.",
        )
    return result.model_dump()
