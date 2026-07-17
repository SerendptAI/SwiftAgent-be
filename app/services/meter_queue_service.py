"""
Meter Queue Service — reliable, retry-backed queue for Polar usage meter events.

Events are written to a `pending_meter_events` MongoDB collection and processed
by a background worker every 5 minutes.  Failed deliveries are retried with
exponential backoff (up to 5 attempts) so that transient Polar outages never
result in lost revenue.
"""

import httpx
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

from app.core.config import settings
from app.core.database import db

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 60  # 1m → 2m → 4m → 8m → 16m


async def enqueue_meter_event(
    company_id: str,
    event_name: str,
) -> None:
    """Insert a meter event into the pending queue for reliable delivery."""
    doc = {
        "company_id": company_id,
        "event_name": event_name,
        "status": "pending",
        "retry_count": 0,
        "next_retry_at": datetime.now(tz=timezone.utc),
        "created_at": datetime.now(tz=timezone.utc),
        "last_error": None,
    }
    await db.pending_meter_events.insert_one(doc)
    logger.debug(
        f"Enqueued meter event '{event_name}' for company {company_id}"
    )


async def process_meter_queue() -> None:
    """
    Process all pending meter events that are ready for delivery.

    Called by APScheduler every 5 minutes.  Sends events to Polar in batches
    of up to 50 per company, marks them as delivered, and applies exponential
    backoff on failures.
    """
    if not settings.POLAR_ACCESS_TOKEN:
        return

    now = datetime.now(tz=timezone.utc)

    # Fetch events that are due for (re)delivery
    cursor = db.pending_meter_events.find({
        "status": "pending",
        "next_retry_at": {"$lte": now},
    }).limit(200)

    events: List[Dict[str, Any]] = await cursor.to_list(length=200)
    if not events:
        return

    logger.info(f"Processing {len(events)} pending meter events")

    headers = {
        "Authorization": f"Bearer {settings.POLAR_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    polar_api_url = getattr(
        settings, "POLAR_API_URL", "https://api.polar.sh/v1"
    )

    # Group by company for efficient batching
    by_company: Dict[str, List[Dict]] = {}
    for ev in events:
        cid = ev["company_id"]
        by_company.setdefault(cid, []).append(ev)

    async with httpx.AsyncClient(timeout=15.0) as client:
        for company_id, company_events in by_company.items():
            payload = {
                "events": [
                    {
                        "name": ev["event_name"],
                        "external_customer_id": company_id,
                    }
                    for ev in company_events
                ]
            }

            try:
                resp = await client.post(
                    f"{polar_api_url}/events/ingest",
                    headers=headers,
                    json=payload,
                )

                if resp.status_code in (200, 201, 202, 204):
                    # Mark all as delivered
                    event_ids = [ev["_id"] for ev in company_events]
                    await db.pending_meter_events.update_many(
                        {"_id": {"$in": event_ids}},
                        {"$set": {
                            "status": "delivered",
                            "delivered_at": datetime.now(tz=timezone.utc),
                        }},
                    )
                    logger.info(
                        f"Delivered {len(company_events)} meter events "
                        f"for company {company_id}"
                    )
                else:
                    await _mark_failed(
                        company_events,
                        f"Polar responded {resp.status_code}: {resp.text[:200]}",
                    )

            except Exception as e:
                await _mark_failed(company_events, str(e))


async def _mark_failed(
    events: List[Dict[str, Any]], error_msg: str
) -> None:
    """Increment retry counts and schedule next attempt with backoff."""
    now = datetime.now(tz=timezone.utc)

    for ev in events:
        new_retry = ev["retry_count"] + 1

        if new_retry >= MAX_RETRIES:
            # Permanently failed — flag for manual review
            await db.pending_meter_events.update_one(
                {"_id": ev["_id"]},
                {"$set": {
                    "status": "failed",
                    "retry_count": new_retry,
                    "last_error": error_msg,
                    "failed_at": now,
                }},
            )
            logger.error(
                f"BILLING CRITICAL: Meter event '{ev['event_name']}' for "
                f"company {ev['company_id']} permanently failed after "
                f"{MAX_RETRIES} retries: {error_msg}"
            )
        else:
            backoff = timedelta(
                seconds=BASE_BACKOFF_SECONDS * (2 ** (new_retry - 1))
            )
            await db.pending_meter_events.update_one(
                {"_id": ev["_id"]},
                {"$set": {
                    "retry_count": new_retry,
                    "next_retry_at": now + backoff,
                    "last_error": error_msg,
                }},
            )
            logger.warning(
                f"Meter event '{ev['event_name']}' for company "
                f"{ev['company_id']} failed (attempt {new_retry}/"
                f"{MAX_RETRIES}), next retry at {now + backoff}: "
                f"{error_msg}"
            )
