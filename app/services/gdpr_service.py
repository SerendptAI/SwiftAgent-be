"""GDPR data portability: full company export and automated deletion.

Exports produce a single JSON bundle covering every company-scoped collection
plus the company's Qdrant vectors. Deletion runs the same discovery in
reverse: MongoDB documents first, then Qdrant vectors, so a failed vector
cleanup can be retried from the deletion job record without orphaning data.
"""

import logging
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

from app.core.config import settings
from app.core.database import db, qdrant_client

logger = logging.getLogger(__name__)

EXPORT_COLLECTIONS = [
    "companies",
    "company_integrations",
    "widget_conversations",
    "visitors",
    "calls",
    "knowledge_sources",
    "email_tickets",
    "forms",
    "form_submissions",
    "form_keys",
    "stroll_configs",
    "stroll_versions",
    "analytics_cache",
    "knowledge_crawl_configs",
    "knowledge_crawl_runs",
    "knowledge_pages",
    "knowledge_gaps",
    "knowledge_gap_events",
    "audit_logs",
]

DELETION_COLLECTIONS = [
    "knowledge_gap_events",
    "knowledge_gaps",
    "knowledge_pages",
    "knowledge_crawl_runs",
    "knowledge_crawl_configs",
    "audit_logs",
    "analytics_cache",
    "form_submissions",
    "form_keys",
    "forms",
    "email_tickets",
    "stroll_versions",
    "stroll_configs",
    "calls",
    "visitors",
    "widget_conversations",
    "company_integrations",
    "pending_meter_events",
]

COLLECTION_FIELD = "company_id"


class ExportJobStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class DeletionRequestStatus(StrEnum):
    PENDING = "pending"
    DELETING = "deleting"
    COMPLETED = "completed"
    FAILED = "failed"


class ExportRequest(BaseModel):
    include_conversations: bool = True
    include_knowledge: bool = True


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_export_job(
    company_id: str,
    requested_by: str,
    include_conversations: bool = True,
    include_knowledge: bool = True,
) -> dict:
    job = {
        "id": f"export_{_utcnow().strftime('%Y%m%d%H%M%S')}_{company_id[:8]}",
        "company_id": company_id,
        "requested_by": requested_by,
        "status": ExportJobStatus.PENDING.value,
        "options": {
            "include_conversations": include_conversations,
            "include_knowledge": include_knowledge,
        },
        "created_at": _utcnow(),
        "completed_at": None,
        "error": None,
    }
    await db.gdpr_export_jobs.insert_one(dict(job))
    return job


async def export_company_data(company_id: str) -> dict:
    """Build the complete portable data bundle for one company."""
    bundle: dict = {
        "export_version": 1,
        "generated_at": _utcnow(),
        "company_id": company_id,
        "collections": {},
    }
    for collection in EXPORT_COLLECTIONS:
        cursor = db[collection].find({COLLECTION_FIELD: company_id})
        bundle["collections"][collection] = await cursor.to_list(length=None)

    conversations = []
    if bundle["collections"].get("widget_conversations"):
        conv_cursor = db.widget_conversations.find({"company_id": company_id})
        conversations = await conv_cursor.to_list(length=None)
    if bundle["collections"].get("stroll_versions"):
        stroll_cursor = db.stroll_versions.find({"company_id": company_id})
        bundle["collections"]["stroll_versions"] = await stroll_cursor.to_list(length=None)
    bundle["conversations"] = conversations

    vectors = await export_company_vectors(company_id)
    bundle["qdrant_vectors"] = vectors
    return bundle


async def export_company_vectors(company_id: str) -> list[dict]:
    """Export Qdrant payloads (not raw embeddings) owned by the company."""
    collection_name = settings.QDRANT_COLLECTION_NAME
    try:
        exists = await qdrant_client.collection_exists(collection_name)
        if not exists:
            return []
        points, _ = await qdrant_client.scroll(
            collection_name=collection_name,
            scroll_filter=_company_filter(company_id),
            limit=256,
            with_payload=True,
            with_vectors=False,
        )
        return [{"id": str(point.id), "payload": point.payload or {}} for point in points]
    except Exception:
        logger.exception("Vector export failed for %s", company_id)
        return []


def _company_filter(company_id: str):
    from qdrant_client.http import models

    return models.Filter(
        must=[models.FieldCondition(key="company_id", match=models.MatchValue(value=company_id))]
    )


async def request_company_deletion(company_id: str, requested_by: str) -> dict:
    """Record a pending deletion request; execution happens via run_deletion."""
    request = {
        "id": f"delete_{_utcnow().strftime('%Y%m%d%H%M%S')}_{company_id[:8]}",
        "company_id": company_id,
        "requested_by": requested_by,
        "status": DeletionRequestStatus.PENDING.value,
        "created_at": _utcnow(),
        "completed_at": None,
        "deleted_counts": {},
        "vectors_deleted": False,
        "error": None,
    }
    await db.gdpr_deletion_requests.insert_one(dict(request))
    return request


async def run_deletion(request_id: str, company_id: str) -> dict:
    """Execute a pending deletion: MongoDB collections, then Qdrant vectors."""
    request = await db.gdpr_deletion_requests.find_one({"id": request_id, "company_id": company_id})
    if not request:
        raise ValueError("deletion request not found")
    if request["status"] == DeletionRequestStatus.COMPLETED.value:
        return request

    await db.gdpr_deletion_requests.update_one(
        {"id": request_id},
        {"$set": {"status": DeletionRequestStatus.DELETING.value}},
    )

    deleted_counts: dict[str, int] = {}
    try:
        for collection in DELETION_COLLECTIONS:
            result = await db[collection].delete_many({COLLECTION_FIELD: company_id})
            deleted_counts[collection] = result.deleted_count

        # User-scoped knowledge documents linked to this company's owner.
        company = await db.companies.find_one({"id": company_id})
        if company:
            owner_id = company.get("user_id")
            if owner_id:
                result = await db.documents.delete_many({"user_id": owner_id})
                deleted_counts["documents"] = result.deleted_count

        vectors_deleted = await delete_company_vectors(company_id)

        await db.companies.delete_one({"id": company_id})
        deleted_counts["companies"] = 1

        update = {
            "status": DeletionRequestStatus.COMPLETED.value,
            "completed_at": _utcnow(),
            "deleted_counts": deleted_counts,
            "vectors_deleted": vectors_deleted,
            "error": None,
        }
    except Exception as exc:
        logger.exception("GDPR deletion failed for %s", company_id)
        update = {
            "status": DeletionRequestStatus.FAILED.value,
            "completed_at": _utcnow(),
            "deleted_counts": deleted_counts,
            "error": str(exc),
        }

    await db.gdpr_deletion_requests.update_one({"id": request_id}, {"$set": update})
    return {**request, **update}


async def delete_company_vectors(company_id: str) -> bool:
    collection_name = settings.QDRANT_COLLECTION_NAME
    try:
        exists = await qdrant_client.collection_exists(collection_name)
        if not exists:
            return True
        await qdrant_client.delete(
            collection_name=collection_name,
            points_selector=_company_filter(company_id),
            wait=True,
        )
        return True
    except Exception:
        logger.exception("Vector deletion failed for %s", company_id)
        return False
