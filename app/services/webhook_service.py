"""Outbound webhook delivery with HMAC signing, retries, and delivery logs.

Endpoints subscribe to event types per company. When an event fires, every
matching enabled endpoint receives a POST with:

- ``X-SwiftAgent-Event``: the event type
- ``X-SwiftAgent-Delivery``: unique delivery id
- ``X-SwiftAgent-Signature``: ``sha256=<hex hmac of the raw body>``

Delivery is at-least-once with exponential backoff; receivers must treat
replays as idempotent using the delivery id.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from app.core.database import db

logger = logging.getLogger(__name__)

COLLECTION_ENDPOINTS = "webhook_endpoints"
COLLECTION_DELIVERIES = "webhook_deliveries"

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2
DELIVERY_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BODY_CHARS = 2000


def _utcnow() -> datetime:
    return datetime.now(UTC)


def generate_secret() -> str:
    return secrets.token_hex(32)


def sign_payload(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def ensure_webhook_indexes() -> None:
    await db[COLLECTION_ENDPOINTS].create_index([("company_id", 1), ("enabled", 1)])
    await db[COLLECTION_DELIVERIES].create_index([("company_id", 1), ("created_at", -1)])
    await db[COLLECTION_DELIVERIES].create_index("endpoint_id")


def _clean(doc: dict) -> dict:
    cleaned = dict(doc)
    cleaned.pop("_id", None)
    return cleaned


async def create_endpoint(
    company_id: str,
    url: str,
    events: list[str],
    description: str | None = None,
) -> dict:
    now = _utcnow()
    endpoint = {
        "id": str(uuid4()),
        "company_id": company_id,
        "url": str(url),
        "events": events,
        "description": description,
        "secret": generate_secret(),
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }
    await db[COLLECTION_ENDPOINTS].insert_one(dict(endpoint))
    return _clean(endpoint)


async def list_endpoints(company_id: str) -> list[dict]:
    cursor = db[COLLECTION_ENDPOINTS].find({"company_id": company_id}).sort("created_at", -1)
    items = await cursor.to_list(length=100)
    return [_clean(item) for item in items]


async def get_endpoint(company_id: str, endpoint_id: str) -> dict | None:
    doc = await db[COLLECTION_ENDPOINTS].find_one({"id": endpoint_id, "company_id": company_id})
    return _clean(doc) if doc else None


async def update_endpoint(company_id: str, endpoint_id: str, changes: dict) -> dict | None:
    changes.pop("secret", None)  # secret rotation is explicit, not PATCHed
    changes["updated_at"] = _utcnow()
    doc = await db[COLLECTION_ENDPOINTS].find_one_and_update(
        {"id": endpoint_id, "company_id": company_id},
        {"$set": changes},
        return_document=True,
    )
    return _clean(doc) if doc else None


async def delete_endpoint(company_id: str, endpoint_id: str) -> bool:
    result = await db[COLLECTION_ENDPOINTS].delete_one(
        {"id": endpoint_id, "company_id": company_id}
    )
    return result.deleted_count > 0


async def rotate_secret(company_id: str, endpoint_id: str) -> dict | None:
    doc = await db[COLLECTION_ENDPOINTS].find_one_and_update(
        {"id": endpoint_id, "company_id": company_id},
        {"$set": {"secret": generate_secret(), "updated_at": _utcnow()}},
        return_document=True,
    )
    return _clean(doc) if doc else None


async def list_deliveries(
    company_id: str,
    endpoint_id: str | None = None,
    limit: int = 50,
    skip: int = 0,
) -> dict:
    query: dict = {"company_id": company_id}
    if endpoint_id:
        query["endpoint_id"] = endpoint_id
    cursor = db[COLLECTION_DELIVERIES].find(query).sort("created_at", -1).skip(skip).limit(limit)
    items = await cursor.to_list(length=limit)
    total = await db[COLLECTION_DELIVERIES].count_documents(query)
    return {
        "items": [_clean(item) for item in items],
        "total": total,
    }


async def emit_event(company_id: str, event_type: str, payload: dict) -> int:
    """Fan out one event to every matching enabled endpoint.

    Never raises: webhook failures must not break the triggering workflow.
    Returns the number of deliveries attempted.
    """
    try:
        endpoints = (
            await db[COLLECTION_ENDPOINTS]
            .find({"company_id": company_id, "enabled": True, "events": event_type})
            .to_list(length=100)
        )
    except Exception:
        logger.exception("Failed to load endpoints for %s", company_id)
        return 0

    attempted = 0
    for endpoint in endpoints:
        asyncio.create_task(_deliver_with_retries(endpoint, event_type, payload))
        attempted += 1
    return attempted


async def _deliver_with_retries(endpoint: dict, event_type: str, payload: dict):
    body_doc = {
        "id": str(uuid4()),
        "event": event_type,
        "created_at": _utcnow().isoformat(),
        "data": payload,
    }
    body = json.dumps(body_doc, default=str).encode()
    signature = sign_payload(endpoint["secret"], body)

    last_status: int | None = None
    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    endpoint["url"],
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-SwiftAgent-Event": event_type,
                        "X-SwiftAgent-Delivery": body_doc["id"],
                        "X-SwiftAgent-Signature": signature,
                    },
                )
            last_status = response.status_code
            if response.status_code < 400:
                await _record_delivery(
                    endpoint,
                    event_type,
                    body_doc["id"],
                    attempt,
                    status="delivered",
                    status_code=response.status_code,
                    response_body=response.text[:MAX_RESPONSE_BODY_CHARS],
                )
                return
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(BACKOFF_BASE_SECONDS ** (attempt - 1))

    await _record_delivery(
        endpoint,
        event_type,
        body_doc["id"],
        MAX_ATTEMPTS,
        status="failed",
        status_code=last_status,
        error=last_error,
    )


async def _record_delivery(
    endpoint: dict,
    event_type: str,
    delivery_id: str,
    attempt: int,
    *,
    status: str,
    status_code: int | None = None,
    response_body: str | None = None,
    error: str | None = None,
) -> None:
    try:
        await db[COLLECTION_DELIVERIES].insert_one(
            {
                "id": delivery_id,
                "endpoint_id": endpoint["id"],
                "company_id": endpoint["company_id"],
                "event_type": event_type,
                "status": status,
                "status_code": status_code,
                "attempt": attempt,
                "response_body": response_body,
                "error": error,
                "created_at": _utcnow(),
            }
        )
    except Exception:
        logger.exception("Failed to record webhook delivery")
