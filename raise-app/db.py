"""
Database module — MongoDB connection for tracking email sends.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from config import config

logger = logging.getLogger(__name__)

COLLECTION_EMAILS = "raise_email_log"
COLLECTION_BATCHES = "raise_batch_log"


class Database:
    """Async MongoDB wrapper for raise-app."""

    _client: Optional[AsyncIOMotorClient] = None
    _db = None

    @classmethod
    async def connect(cls):
        """Establish MongoDB connection."""
        if not config.MONGO_URI:
            logger.warning("No MONGO_URI — email tracking disabled")
            return
        cls._client = AsyncIOMotorClient(config.MONGO_URI)
        cls._db = cls._client[config.MONGO_DB_NAME]
        # Create indexes
        try:
            await cls._db[COLLECTION_EMAILS].create_index("to_email")
            await cls._db[COLLECTION_EMAILS].create_index("sent_at")
            await cls._db[COLLECTION_EMAILS].create_index("status")
        except Exception as e:
            logger.warning("Index creation failed: %s", e)

    @classmethod
    async def close(cls):
        """Close MongoDB connection."""
        if cls._client is not None:
            cls._client.close()

    @classmethod
    async def log_email_send(
        cls,
        to_email: str,
        subject: str,
        status: str,
        error: str = None,
        recipient_info: Dict = None,
    ):
        """Log an individual email send."""
        if cls._db is None:
            return
        doc = {
            "to_email": to_email,
            "subject": subject,
            "status": status,
            "sent_at": datetime.now(timezone.utc),
            "error": error,
            "recipient_name": (recipient_info or {}).get("Full Name", ""),
            "recipient_company": (recipient_info or {}).get("Company", ""),
        }
        try:
            await cls._db[COLLECTION_EMAILS].insert_one(doc)
        except Exception as e:
            logger.error("Failed to log email send: %s", e)

    @classmethod
    async def log_batch_send(cls, subject: str, total: int, sent: int, failed: int):
        """Log a batch send summary."""
        if cls._db is None:
            return
        doc = {
            "subject": subject,
            "total": total,
            "sent": sent,
            "failed": failed,
            "sent_at": datetime.now(timezone.utc),
        }
        try:
            await cls._db[COLLECTION_BATCHES].insert_one(doc)
        except Exception as e:
            logger.error("Failed to log batch send: %s", e)

    @classmethod
    async def get_email_stats(cls) -> Dict:
        """Get aggregated email stats."""
        if cls._db is None:
            return {"total_sent": 0, "total_failed": 0, "recent_sends": []}

        try:
            total_sent = await cls._db[COLLECTION_EMAILS].count_documents(
                {"status": "sent"}
            )
            total_failed = await cls._db[COLLECTION_EMAILS].count_documents(
                {"status": "failed"}
            )

            recent = (
                await cls._db[COLLECTION_EMAILS]
                .find({"status": "sent"})
                .sort("sent_at", -1)
                .limit(10)
                .to_list(10)
            )

            recent_sends = []
            for r in recent:
                recent_sends.append(
                    {
                        "to_email": r.get("to_email", ""),
                        "subject": r.get("subject", ""),
                        "sent_at": r.get("sent_at", "").isoformat()
                        if r.get("sent_at")
                        else "",
                        "recipient_name": r.get("recipient_name", ""),
                        "recipient_company": r.get("recipient_company", ""),
                    }
                )

            return {
                "total_sent": total_sent,
                "total_failed": total_failed,
                "recent_sends": recent_sends,
            }
        except Exception as e:
            logger.error("Failed to get email stats: %s", e)
            return {"total_sent": 0, "total_failed": 0, "recent_sends": []}

    @classmethod
    async def get_email_history(cls, page: int = 1, per_page: int = 50) -> Dict:
        """Get paginated email history."""
        if cls._db is None:
            return {"emails": [], "total": 0, "page": page, "per_page": per_page}

        try:
            total = await cls._db[COLLECTION_EMAILS].count_documents({})
            skip = (page - 1) * per_page

            emails = (
                await cls._db[COLLECTION_EMAILS]
                .find({})
                .sort("sent_at", -1)
                .skip(skip)
                .limit(per_page)
                .to_list(per_page)
            )

            result = []
            for e in emails:
                result.append(
                    {
                        "to_email": e.get("to_email", ""),
                        "subject": e.get("subject", ""),
                        "status": e.get("status", ""),
                        "sent_at": e.get("sent_at", "").isoformat()
                        if e.get("sent_at")
                        else "",
                        "error": e.get("error", ""),
                        "recipient_name": e.get("recipient_name", ""),
                        "recipient_company": e.get("recipient_company", ""),
                    }
                )

            return {
                "emails": result,
                "total": total,
                "page": page,
                "per_page": per_page,
                "total_pages": max(1, (total + per_page - 1) // per_page),
            }
        except Exception as e:
            logger.error("Failed to get email history: %s", e)
            return {"emails": [], "total": 0, "page": page, "per_page": per_page}

    @classmethod
    async def enrich_contacts_with_status(cls, contacts: List[Dict]) -> List[Dict]:
        """Add email send status to contacts."""
        if cls._db is None:
            return contacts

        try:
            # Get latest send status for all emails in this contact list
            emails = [c.get("Email", "") for c in contacts if c.get("Email")]
            if not emails:
                return contacts

            pipeline = [
                {"$match": {"to_email": {"$in": emails}}},
                {"$sort": {"sent_at": -1}},
                {
                    "$group": {
                        "_id": "$to_email",
                        "last_status": {"$first": "$status"},
                        "last_sent": {"$first": "$sent_at"},
                        "send_count": {"$sum": 1},
                    }
                },
            ]

            results = await cls._db[COLLECTION_EMAILS].aggregate(pipeline).to_list(
                None
            )

            status_map = {}
            for r in results:
                status_map[r["_id"]] = {
                    "last_status": r.get("last_status", ""),
                    "last_sent": r.get("last_sent", "").isoformat()
                    if r.get("last_sent")
                    else "",
                    "send_count": r.get("send_count", 0),
                }

            enriched = []
            for c in contacts:
                email = c.get("Email", "")
                status_info = status_map.get(email, {})
                c["last_status"] = status_info.get("last_status", "never")
                c["last_sent"] = status_info.get("last_sent", "")
                c["send_count"] = status_info.get("send_count", 0)
                enriched.append(c)

            return enriched
        except Exception as e:
            logger.error("Failed to enrich contacts: %s", e)
            return contacts
