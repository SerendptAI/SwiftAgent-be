"""
API key service — generate, hash, verify, and revoke company API keys.

Keys are stored as SHA-256 hashes; the raw key is returned exactly once on creation.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from app.core.database import db

logger = logging.getLogger(__name__)

_KEY_PREFIX = "sk_live_"
_KEY_BYTES = 32  # 256-bit key


def _generate_raw_key() -> str:
    """Generate a raw API key string: sk_live_<random hex>."""
    return f"{_KEY_PREFIX}{secrets.token_hex(_KEY_BYTES)}"


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the raw key for storage."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _key_display_prefix(raw_key: str) -> str:
    """First 12 chars for safe display, e.g. 'sk_live_Ab3x'."""
    return raw_key[:12]


async def create_api_key(company_id: str, label: str = "Default") -> dict:
    """
    Generate a new API key for a company.

    Returns the full document WITH the raw key (shown once).
    """
    raw_key = _generate_raw_key()
    now = datetime.now(tz=timezone.utc)

    doc = {
        "id": str(uuid4()),
        "company_id": company_id,
        "key_hash": _hash_key(raw_key),
        "key_prefix": _key_display_prefix(raw_key),
        "label": label,
        "scopes": ["sdk"],
        "active": True,
        "created_at": now,
        "last_used_at": None,
    }

    await db.company_api_keys.insert_one(doc)
    logger.info("Created API key %s for company %s", doc["id"], company_id)

    # Return with raw key (only time it's exposed)
    return {**doc, "key": raw_key}


async def list_api_keys(company_id: str) -> list[dict]:
    """List all API keys for a company (prefix + metadata only, never the hash)."""
    cursor = db.company_api_keys.find(
        {"company_id": company_id},
        {
            "_id": 0,
            "key_hash": 0,  # never expose
        },
    ).sort("created_at", -1)
    return await cursor.to_list(length=50)


async def revoke_api_key(company_id: str, key_id: str) -> bool:
    """Deactivate an API key."""
    result = await db.company_api_keys.update_one(
        {"company_id": company_id, "id": key_id, "active": True},
        {"$set": {"active": False}},
    )
    if result.modified_count > 0:
        logger.info("Revoked API key %s for company %s", key_id, company_id)
        return True
    return False


async def verify_api_key(raw_key: str) -> dict | None:
    """
    Verify a raw API key and return the company_id if valid.

    Updates `last_used_at` on success.
    Returns the key document (minus hash) or None.
    """
    key_hash = _hash_key(raw_key)
    doc = await db.company_api_keys.find_one(
        {"key_hash": key_hash, "active": True},
        {"_id": 0},
    )
    if not doc:
        return None

    # Update last_used_at (fire-and-forget)
    await db.company_api_keys.update_one(
        {"key_hash": key_hash},
        {"$set": {"last_used_at": datetime.now(tz=timezone.utc)}},
    )

    return doc
