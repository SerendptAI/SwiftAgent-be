"""Immutable audit log for enterprise compliance.

Every entry records who did what to which resource, when, from where, and
whether the action succeeded. Entries are append-only: updates and deletes on
the collection are rejected at the service layer, and MongoDB is configured
with a validator that rejects ``$set``/``$unset`` style mutations.
"""

import json
from datetime import UTC, datetime
from typing import Any

from app.core.database import db

COLLECTION = "audit_logs"

# Actions that must always be audited regardless of caller configuration.
SENSITIVE_ACTIONS = {
    "company.update",
    "company.member_invited",
    "company.member_removed",
    "company.api_key_created",
    "company.api_key_revoked",
    "knowledge.ingested",
    "knowledge.deleted",
    "crawl.config_updated",
    "crawl.config_deleted",
    "crawl.triggered",
    "ticket.status_changed",
    "ticket.assigned",
    "conversation.deleted",
}

_RETENTION_DAYS = 365


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def ensure_audit_indexes() -> None:
    """Create indexes; TTL keeps entries for one year."""
    await db[COLLECTION].create_index([("company_id", 1), ("created_at", -1)])
    await db[COLLECTION].create_index([("actor_id", 1), ("created_at", -1)])
    await db[COLLECTION].create_index(
        "created_at", expireAfterSeconds=_RETENTION_DAYS * 24 * 3600
    )


async def record_event(
    *,
    company_id: str | None,
    actor_id: str | None,
    actor_type: str = "user",
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    outcome: str = "success",
    ip_address: str | None = None,
    user_agent: str | None = None,
    changes: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Append one immutable audit entry.

    ``changes`` should hold a compact before/after diff — never raw secrets or
    full document bodies.
    """
    now = _utcnow()
    entry = {
        "id": f"{now.strftime('%Y%m%d%H%M%S%f')}-{action}",
        "company_id": company_id,
        "actor_id": actor_id,
        "actor_type": actor_type,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "outcome": outcome,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "changes": _safe_changes(changes),
        "metadata": metadata or {},
        "created_at": now,
        "expires_at": datetime.fromtimestamp(
            now.timestamp() + _RETENTION_DAYS * 24 * 3600, tz=UTC
        ),
    }
    await db[COLLECTION].insert_one(dict(entry))
    return entry


def _safe_changes(changes: dict[str, Any] | None) -> dict[str, Any]:
    """Redact secret-looking fields so they never land in the audit trail."""
    if not changes:
        return {}
    redacted_keys = {"password", "token", "secret", "api_key", "authorization"}
    safe: dict[str, Any] = {}
    for key, value in changes.items():
        if any(marker in key.lower() for marker in redacted_keys):
            safe[key] = "[REDACTED]"
        elif isinstance(value, dict):
            safe[key] = _safe_changes(value)
        else:
            safe[key] = value
    return safe


async def list_events(
    company_id: str,
    *,
    actor_id: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    outcome: str | None = None,
    limit: int = 50,
    skip: int = 0,
) -> dict:
    """Query audit entries for a company, newest first."""
    query: dict[str, Any] = {"company_id": company_id}
    if actor_id:
        query["actor_id"] = actor_id
    if action:
        query["action"] = action
    if resource_type:
        query["resource_type"] = resource_type
    if outcome:
        query["outcome"] = outcome
    cursor = (
        db[COLLECTION]
        .find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )
    items = await cursor.to_list(length=limit)
    total = await db[COLLECTION].count_documents(query)
    return {"items": [_clean(item) for item in items], "total": total}


async def get_event(company_id: str, event_id: str) -> dict | None:
    entry = await db[COLLECTION].find_one({"id": event_id, "company_id": company_id})
    return _clean(entry) if entry else None


def export_events_jsonl(entries: list[dict]) -> str:
    """Serialize entries as JSON Lines for external SIEM ingestion."""
    lines = []
    for entry in entries:
        record = {k: v for k, v in entry.items() if k != "_id"}
        lines.append(json.dumps(record, default=str))
    return "\n".join(lines)


def _clean(entry: dict) -> dict:
    cleaned = dict(entry)
    cleaned.pop("_id", None)
    return cleaned
