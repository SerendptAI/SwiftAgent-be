import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pymongo import ReturnDocument

from app.core.auth import get_current_user
from app.core.database import db
from app.models.knowledge_crawl_models import (
    CrawlConfigCreate,
    CrawlConfigUpdate,
    KnowledgeGapUpdate,
)
from app.services import company_service, knowledge_crawl_scheduler
from app.services.knowledge_crawl_service import (
    CrawlAlreadyRunningError,
    create_queued_run,
    get_freshness_summary,
    run_company_crawl,
)
from app.services.knowledge_gap_service import verify_gap

router = APIRouter(tags=["Knowledge Auto-Crawl"])
CurrentUser = Annotated[dict, Depends(get_current_user)]


def _clean(document: dict | None) -> dict | None:
    if document:
        document.pop("_id", None)
    return document


async def _authorize(company_id: str, current_user: dict, *, admin_only: bool = False) -> dict:
    company = await company_service.get_company(
        company_id, current_user["user_id"], admin_only=admin_only
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.put("/companies/{company_id}/config")
async def upsert_crawl_config(
    company_id: str,
    payload: CrawlConfigCreate,
    current_user: CurrentUser,
):
    await _authorize(company_id, current_user, admin_only=True)
    if payload.company_id != company_id:
        raise HTTPException(status_code=400, detail="company_id does not match route")
    now = datetime.now(UTC)
    data = payload.model_dump(mode="json")
    data["root_url"] = str(payload.root_url)
    existing = await db.knowledge_crawl_configs.find_one({"company_id": company_id})
    data.update(
        {
            "id": existing.get("id", str(uuid.uuid4())) if existing else str(uuid.uuid4()),
            "created_at": existing.get("created_at", now) if existing else now,
            "updated_at": now,
        }
    )
    result = await db.knowledge_crawl_configs.find_one_and_update(
        {"company_id": company_id},
        {"$set": data},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if payload.enabled:
        next_run_at = knowledge_crawl_scheduler.schedule_crawl_job(
            company_id, payload.schedule, payload.timezone
        )
        if next_run_at:
            await db.knowledge_crawl_configs.update_one(
                {"company_id": company_id},
                {"$set": {"next_run_at": next_run_at}},
            )
            result["next_run_at"] = next_run_at
    else:
        knowledge_crawl_scheduler.remove_crawl_job(company_id)
    return _clean(result)


@router.get("/companies/{company_id}/config")
async def get_crawl_config(company_id: str, current_user: CurrentUser):
    await _authorize(company_id, current_user)
    config = await db.knowledge_crawl_configs.find_one({"company_id": company_id})
    if not config:
        raise HTTPException(status_code=404, detail="Crawl configuration not found")
    return _clean(config)


@router.patch("/companies/{company_id}/config")
async def update_crawl_config(
    company_id: str,
    payload: CrawlConfigUpdate,
    current_user: CurrentUser,
):
    await _authorize(company_id, current_user, admin_only=True)
    changes = payload.model_dump(exclude_none=True, mode="json")
    if "root_url" in changes:
        changes["root_url"] = str(payload.root_url)
    changes["updated_at"] = datetime.now(UTC)
    result = await db.knowledge_crawl_configs.find_one_and_update(
        {"company_id": company_id},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Crawl configuration not found")
    if result.get("enabled", True):
        next_run_at = knowledge_crawl_scheduler.schedule_crawl_job(
            company_id, result.get("schedule", "0 2 * * *"), result.get("timezone", "UTC")
        )
        if next_run_at:
            await db.knowledge_crawl_configs.update_one(
                {"company_id": company_id},
                {"$set": {"next_run_at": next_run_at}},
            )
            result["next_run_at"] = next_run_at
    else:
        knowledge_crawl_scheduler.remove_crawl_job(company_id)
    return _clean(result)


@router.delete("/companies/{company_id}/config", status_code=status.HTTP_204_NO_CONTENT)
async def delete_crawl_config(company_id: str, current_user: CurrentUser):
    await _authorize(company_id, current_user, admin_only=True)
    config = await db.knowledge_crawl_configs.find_one({"company_id": company_id})
    if not config:
        raise HTTPException(status_code=404, detail="Crawl configuration not found")
    active = await db.knowledge_crawl_configs.find_one(
        {"company_id": company_id, "lock_expires_at": {"$gt": datetime.now(UTC)}}
    )
    if active:
        raise HTTPException(status_code=409, detail="A crawl is currently running")
    result = await db.knowledge_crawl_configs.delete_one(
        {
            "company_id": company_id,
            "$or": [
                {"lock_expires_at": {"$exists": False}},
                {"lock_expires_at": None},
                {"lock_expires_at": {"$lte": datetime.now(UTC)}},
            ],
        }
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=409, detail="A crawl started before deletion")
    knowledge_crawl_scheduler.remove_crawl_job(company_id)


@router.post("/companies/{company_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def start_crawl(
    company_id: str,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser,
):
    await _authorize(company_id, current_user, admin_only=True)
    config = await db.knowledge_crawl_configs.find_one({"company_id": company_id})
    if not config:
        raise HTTPException(status_code=404, detail="Crawl configuration not found")
    try:
        run = await create_queued_run(company_id, "manual")
        background_tasks.add_task(run_company_crawl, company_id, "manual", run["id"])
    except CrawlAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@router.get("/companies/{company_id}/runs")
async def list_crawl_runs(
    company_id: str,
    current_user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    skip: Annotated[int, Query(ge=0)] = 0,
):
    await _authorize(company_id, current_user)
    cursor = (
        db.knowledge_crawl_runs.find({"company_id": company_id})
        .sort("started_at", -1)
        .skip(skip)
        .limit(limit)
    )
    items = await cursor.to_list(length=limit)
    return {
        "items": [_clean(item) for item in items],
        "total": await db.knowledge_crawl_runs.count_documents({"company_id": company_id}),
    }


@router.get("/runs/{run_id}")
async def get_crawl_run(run_id: str, current_user: CurrentUser):
    run = await db.knowledge_crawl_runs.find_one({"id": run_id})
    if not run:
        raise HTTPException(status_code=404, detail="Crawl run not found")
    await _authorize(run["company_id"], current_user)
    return _clean(run)


@router.get("/companies/{company_id}/freshness")
async def get_freshness(company_id: str, current_user: CurrentUser):
    await _authorize(company_id, current_user)
    try:
        return await get_freshness_summary(company_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/companies/{company_id}/pages")
async def list_crawled_pages(
    company_id: str,
    current_user: CurrentUser,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    skip: Annotated[int, Query(ge=0)] = 0,
):
    await _authorize(company_id, current_user)
    query = {"company_id": company_id}
    if status_filter:
        query["status"] = status_filter
    cursor = db.knowledge_pages.find(query).sort("last_seen_at", -1).skip(skip).limit(limit)
    items = await cursor.to_list(length=limit)
    return {
        "items": [_clean(item) for item in items],
        "total": await db.knowledge_pages.count_documents(query),
    }


@router.get("/companies/{company_id}/gaps")
async def list_knowledge_gaps(
    company_id: str,
    current_user: CurrentUser,
    gap_status: Annotated[str, Query(alias="status")] = "open",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    await _authorize(company_id, current_user)
    query = {"company_id": company_id, "status": gap_status}
    cursor = db.knowledge_gaps.find(query).sort("priority_score", -1).limit(limit)
    items = await cursor.to_list(length=limit)
    return {
        "items": [_clean(item) for item in items],
        "total": await db.knowledge_gaps.count_documents(query),
    }


@router.patch("/companies/{company_id}/gaps/{gap_id}")
async def update_knowledge_gap(
    company_id: str,
    gap_id: str,
    payload: KnowledgeGapUpdate,
    current_user: CurrentUser,
):
    await _authorize(company_id, current_user, admin_only=True)
    changes = payload.model_dump(exclude_none=True, mode="json")
    changes["updated_at"] = datetime.now(UTC)
    result = await db.knowledge_gaps.find_one_and_update(
        {"id": gap_id, "company_id": company_id},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Knowledge gap not found")
    return _clean(result)


@router.post("/companies/{company_id}/gaps/{gap_id}/verify")
async def verify_knowledge_gap(
    company_id: str,
    gap_id: str,
    current_user: CurrentUser,
    threshold: Annotated[float, Query(ge=0.0, le=1.0)] = 0.7,
):
    await _authorize(company_id, current_user, admin_only=True)
    try:
        return _clean(await verify_gap(company_id, gap_id, threshold))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
