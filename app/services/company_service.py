from datetime import datetime, timezone
from uuid import uuid4
from app.core.database import db
from app.core.cache import company_cache
from typing import Optional


async def create_company(user_id: str, data: dict) -> dict:
    doc = {
        "id": str(uuid4()),
        "user_id": user_id,
        "name": data["name"],
        "website": data.get("website"),
        "industry": data.get("industry"),
        "company_size": data.get("company_size"),
        "country": data.get("country"),
        "timezone": data.get("timezone"),
        "contact_email": data["contact_email"],
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
