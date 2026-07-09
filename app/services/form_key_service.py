"""
Form key service — generate, hash, verify, and revoke SDPK-prefixed key pairs for forms.

Each form gets an API key (server-side) and a public key (embedded in the widget).
Keys are stored as SHA-256 hashes; the raw keys are returned exactly once on creation.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timezone

from app.core.database import db

logger = logging.getLogger(__name__)

_KEY_PREFIX = "SDPK-"
_KEY_BYTES = 32  # 256-bit key


def _generate_sdpk_key() -> str:
    """Generate a raw SDPK key string: SDPK-<3-digit><hex>.

    Format: SDPK- + 3-digit random number + hex random (32 bytes total).
    """
    three_digits = f"{secrets.randbelow(900) + 100:03d}"
    hex_part = secrets.token_hex(_KEY_BYTES)[: (_KEY_BYTES * 2) - 3]
    return f"{_KEY_PREFIX}{three_digits}{hex_part}".upper()


def _hash_key(raw_key: str) -> str:
    """SHA-256 hash of the raw key for storage."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _key_display_prefix(raw_key: str) -> str:
    """First 16 chars for safe display, e.g. 'SDPK-272A8F3B1C2D'."""
    return raw_key[:16]


async def generate_form_keys(form_id: str, company_id: str) -> dict:
    """
    Generate a new API key and public key pair for a form.

    Returns both raw keys (shown once) as ``{api_key, public_key}``.
    """
    raw_api_key = _generate_sdpk_key()
    raw_public_key = _generate_sdpk_key()
    now = datetime.now(tz=timezone.utc)

    doc = {
        "form_id": form_id,
        "company_id": company_id,
        "api_key_hash": _hash_key(raw_api_key),
        "api_key_prefix": _key_display_prefix(raw_api_key),
        "public_key_hash": _hash_key(raw_public_key),
        "public_key_prefix": _key_display_prefix(raw_public_key),
        "active": True,
        "created_at": now,
        "last_used_at": None,
    }

    await db.form_keys.insert_one(doc)
    logger.info("Created form keys for form %s (company %s)", form_id, company_id)

    return {"api_key": raw_api_key, "public_key": raw_public_key}


async def verify_public_key(public_key: str) -> dict | None:
    """
    Verify a raw public key and return the associated form/company info.

    Updates ``last_used_at`` on success.
    Returns ``{form_id, company_id}`` or None.
    """
    key_hash = _hash_key(public_key)
    doc = await db.form_keys.find_one(
        {"public_key_hash": key_hash, "active": True},
        {"_id": 0},
    )
    if not doc:
        return None

    # Update last_used_at (fire-and-forget)
    await db.form_keys.update_one(
        {"public_key_hash": key_hash},
        {"$set": {"last_used_at": datetime.now(tz=timezone.utc)}},
    )

    return {"form_id": doc["form_id"], "company_id": doc["company_id"]}


async def get_keys_for_form(form_id: str) -> dict | None:
    """Return key prefixes for dashboard display, or None if not found."""
    doc = await db.form_keys.find_one(
        {"form_id": form_id, "active": True},
        {"_id": 0, "api_key_prefix": 1, "public_key_prefix": 1},
    )
    if not doc:
        return None

    return {
        "api_key_prefix": doc["api_key_prefix"],
        "public_key_prefix": doc["public_key_prefix"],
    }


async def revoke_keys(form_id: str) -> bool:
    """Deactivate all active keys for a form."""
    result = await db.form_keys.update_many(
        {"form_id": form_id, "active": True},
        {"$set": {"active": False}},
    )
    if result.modified_count > 0:
        logger.info("Revoked keys for form %s", form_id)
        return True
    return False


async def regenerate_keys(form_id: str, company_id: str) -> dict:
    """
    Revoke existing keys and generate fresh ones.

    Returns the new raw keys as ``{api_key, public_key}``.
    """
    await revoke_keys(form_id)
    return await generate_form_keys(form_id, company_id)


def generate_snippet(public_key: str) -> str:
    """Generate the embeddable HTML snippet for a form's public key."""
    return (
        '<script src="https://swiftagents.org/api/v1/public/forms/widget.js"></script>\n'
        f'<script>SwiftForms.init({{ publicKey: "{public_key}" }});</script>'
    )
