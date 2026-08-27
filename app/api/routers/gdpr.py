"""GDPR data portability APIs (admin only).

Exports download as a JSON bundle. Deletion requires a two-step flow:
request first, then explicit execution, so an accidental click cannot wipe
a company.
"""

import json
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response

from app.core.auth import get_current_user
from app.models.gdpr_models import ExportRequest
from app.services import company_service, gdpr_service

router = APIRouter(tags=["GDPR"])

CurrentUser = Annotated[dict, Depends(get_current_user)]


async def _require_admin(company_id: str, current_user: dict) -> dict:
    company = await company_service.get_company(
        company_id, current_user["user_id"], admin_only=True
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.post("/companies/{company_id}/gdpr/export", status_code=status.HTTP_202_ACCEPTED)
async def start_export(
    company_id: str,
    current_user: CurrentUser,
    payload: ExportRequest | None = None,
):
    await _require_admin(company_id, current_user)
    payload = payload or ExportRequest()
    job = await gdpr_service.create_export_job(
        company_id,
        current_user["user_id"],
        include_conversations=payload.include_conversations,
        include_knowledge=payload.include_knowledge,
    )
    return job


@router.get("/companies/{company_id}/gdpr/export/download")
async def download_export(
    company_id: str,
    current_user: CurrentUser,
):
    await _require_admin(company_id, current_user)
    bundle = await gdpr_service.export_company_data(company_id)

    def _serialize(item):
        if isinstance(item, datetime):
            return item.astimezone(UTC).isoformat()
        raise TypeError(f"unserializable type {type(item)}")

    content = json.dumps(bundle, default=_serialize, indent=2)
    filename = f"{company_id}-data-export-{datetime.now(UTC).date()}.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/companies/{company_id}/gdpr/delete", status_code=status.HTTP_202_ACCEPTED)
async def request_deletion(company_id: str, current_user: CurrentUser):
    await _require_admin(company_id, current_user)
    return await gdpr_service.request_company_deletion(company_id, current_user["user_id"])


@router.post("/companies/{company_id}/gdpr/delete/{request_id}/execute")
async def execute_deletion(company_id: str, request_id: str, current_user: CurrentUser):
    await _require_admin(company_id, current_user)
    try:
        return await gdpr_service.run_deletion(request_id, company_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/companies/{company_id}/gdpr/requests")
async def list_gdpr_requests(
    company_id: str,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
):
    await _require_admin(company_id, current_user)
    exports = await db_list("gdpr_export_jobs", company_id, limit)
    deletions = await db_list("gdpr_deletion_requests", company_id, limit)
    return {"exports": exports, "deletions": deletions}


async def db_list(collection: str, company_id: str, limit: int) -> list[dict]:
    from app.core.database import db

    cursor = db[collection].find({"company_id": company_id}).sort("created_at", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    for item in items:
        item.pop("_id", None)
    return items
