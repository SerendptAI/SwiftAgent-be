from datetime import datetime, timezone
from uuid import uuid4
from app.core.database import db
from app.core.cache import company_cache
from app.core.config import settings
from typing import Optional
import re


def _sanitize_slug(name: str) -> str:
    """Convert a company name to a valid email slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    # enforce length
    if len(slug) > 30:
        slug = slug[:30].rstrip("-")
    if len(slug) < 3:
        slug = slug + "co"
    return slug


async def check_slug_availability(slug: str) -> bool:
    """Check if an email slug is available."""
    existing = await db.companies.find_one({"email_slug": slug})
    return existing is None


async def suggest_slug(name: str) -> str:
    """Generate an available slug from a company name."""
    base = _sanitize_slug(name)
    if await check_slug_availability(base):
        return base
    # try appending numbers
    for i in range(2, 100):
        candidate = f"{base}{i}" if len(f"{base}{i}") <= 30 else f"{base[:27]}{i}"
        if await check_slug_availability(candidate):
            return candidate
    return f"{base}-{str(uuid4())[:4]}"


def _compute_email_address(slug: Optional[str]) -> Optional[str]:
    """Compute full email address from slug."""
    if not slug:
        return None
    return f"{slug}@{settings.EMAIL_DOMAIN}"


async def create_company(user_id: str, data: dict) -> dict:
    email_slug = data.get("email_slug")

    doc = {
        "id": str(uuid4()),
        "user_id": user_id,
        "name": data["name"],
        "email_slug": email_slug,
        "email_address": _compute_email_address(email_slug),
        "website": data.get("website"),
        "industry": data.get("industry"),
        "company_size": data.get("company_size"),
        "country": data.get("country"),
        "timezone": data.get("timezone"),
        "contact_email": data["contact_email"],
        "support_email": data.get("support_email"),
        "phone_number": data.get("phone_number"),
        "logo_url": None,
        "company_type": None,
        "onboarding_step": 1,
        "setup_complete": False,
        "description": None,
        "customer_value": None,
        "brand_tone": None,
        "primary_language": "English",
        "enabled_sources": [],
        "custom_info": [],
        "voice_style": "professional",
        "created_at": datetime.now(tz=timezone.utc),
        "updated_at": datetime.now(tz=timezone.utc),
    }
    await db.companies.insert_one(doc)
    await company_cache.set(f"company:{doc['id']}", doc)
    return doc


async def get_company(company_id: str, user_id: Optional[str] = None) -> Optional[dict]:
    cache_key = f"company:{company_id}:{user_id or 'public'}"
    cached = await company_cache.get(cache_key)
    if cached is not None:
        return cached

    query = {"id": company_id}
    if user_id:
        query["user_id"] = user_id

    company = await db.companies.find_one(query)
    if company:
        await company_cache.set(cache_key, company)
    return company


async def get_company_by_slug(slug: str) -> Optional[dict]:
    """Look up a company by its email slug."""
    return await db.companies.find_one({"email_slug": slug})


async def invalidate_company_cache(company_id: str):
    keys_to_invalidate = []
    async for key in _cache_keys_for_company(company_id):
        keys_to_invalidate.append(key)
    for key in keys_to_invalidate:
        await company_cache.delete(key)


async def _cache_keys_for_company(company_id: str):
    for key in list(company_cache._store.keys()):
        if key.startswith(f"company:{company_id}:"):
            yield key


async def list_companies(user_id: str) -> list:
    cursor = db.companies.find(
        {"user_id": user_id},
        {"description": 0, "customer_value": 0, "enabled_sources": 0, "custom_info": 0},
    )
    return await cursor.to_list(length=50)


async def _update_and_return(
    company_id: str, user_id: str, update_fields: dict
) -> dict:
    update_fields["updated_at"] = datetime.now(tz=timezone.utc)
    result = await db.companies.find_one_and_update(
        {"id": company_id, "user_id": user_id},
        {"$set": update_fields},
        return_document=1,
    )
    if result:
        await invalidate_company_cache(company_id)
    return result


async def update_identity(company_id: str, user_id: str, data: dict) -> dict:
    update = {
        **{k: v for k, v in data.items() if v is not None},
        "onboarding_step": 2,
    }
    return await _update_and_return(company_id, user_id, update)


async def update_company_type(company_id: str, user_id: str, company_type: str) -> dict:
    return await _update_and_return(
        company_id,
        user_id,
        {"company_type": company_type, "onboarding_step": 3},
    )


async def update_company_info(company_id: str, user_id: str, data: dict) -> dict:
    update = {k: v for k, v in data.items() if v is not None}
    return await _update_and_return(company_id, user_id, update)


async def update_security(company_id: str, user_id: str, data: dict) -> dict:
    update = {k: v for k, v in data.items() if v is not None}
    return await _update_and_return(company_id, user_id, update)


async def update_boundaries(company_id: str, user_id: str, data: dict) -> dict:
    update = {**data, "onboarding_step": 4}
    return await _update_and_return(company_id, user_id, update)


async def update_voice(company_id: str, user_id: str, data: dict) -> dict:
    update = {**data, "onboarding_step": 5, "setup_complete": True}
    return await _update_and_return(company_id, user_id, update)


async def update_logo(company_id: str, user_id: str, logo_url: str) -> dict:
    return await _update_and_return(company_id, user_id, {"logo_url": logo_url})


async def update_email_slug(company_id: str, user_id: str, slug: str) -> dict:
    """Set or update the company's email slug."""
    email_address = _compute_email_address(slug)
    return await _update_and_return(
        company_id, user_id, {"email_slug": slug, "email_address": email_address}
    )

