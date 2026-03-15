from datetime import datetime
from uuid import uuid4
from app.core.database import db
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
        # step 2
        "description": None,
        "customer_value": None,
        "brand_tone": None,
        "primary_language": "English",
        # step 4
        "enabled_sources": [],
        "custom_info": [],
        # step 5
        "voice_style": "professional",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    await db.companies.insert_one(doc)
    return doc

async def get_company(company_id: str, user_id: Optional[str] = None) -> dict:
    if user_id:
        return await db.companies.find_one({"id": company_id, "user_id": user_id})
    return await db.companies.find_one({"id": company_id})

async def list_companies(user_id: str) -> list:
    cursor = db.companies.find(
        {"user_id": user_id},
        {"description": 0, "customer_value": 0, "enabled_sources": 0, "custom_info": 0},
    )
    return await cursor.to_list(length=50)

async def update_identity(company_id: str, user_id: str, data: dict) -> dict:
    update = {
        "$set": {
            **{k: v for k, v in data.items() if v is not None},
            "onboarding_step": 2,
            "updated_at": datetime.utcnow(),
        }
    }
    await db.companies.update_one({"id": company_id, "user_id": user_id}, update)
    return await get_company(company_id, user_id)

async def update_company_type(company_id: str, user_id: str, company_type: str) -> dict:
    await db.companies.update_one(
        {"id": company_id, "user_id": user_id},
        {"$set": {"company_type": company_type, "onboarding_step": 3, "updated_at": datetime.utcnow()}},
    )
    return await get_company(company_id, user_id)

async def update_company_info(company_id: str, user_id: str, data: dict) -> dict:
    update = {
        "$set": {
            **{k: v for k, v in data.items() if v is not None},
            "updated_at": datetime.utcnow(),
        }
    }
    await db.companies.update_one({"id": company_id, "user_id": user_id}, update)
    return await get_company(company_id, user_id)

async def update_security(company_id: str, user_id: str, data: dict) -> dict:
    update = {
        "$set": {
            **{k: v for k, v in data.items() if v is not None},
            "updated_at": datetime.utcnow(),
        }
    }
    await db.companies.update_one({"id": company_id, "user_id": user_id}, update)
    return await get_company(company_id, user_id)

async def update_boundaries(company_id: str, user_id: str, data: dict) -> dict:
    await db.companies.update_one(
        {"id": company_id, "user_id": user_id},
        {"$set": {**data, "onboarding_step": 4, "updated_at": datetime.utcnow()}},
    )
    return await get_company(company_id, user_id)

async def update_voice(company_id: str, user_id: str, data: dict) -> dict:
    await db.companies.update_one(
        {"id": company_id, "user_id": user_id},
        {"$set": {**data, "onboarding_step": 5, "setup_complete": True, "updated_at": datetime.utcnow()}},
    )
    return await get_company(company_id, user_id)

async def update_logo(company_id: str, user_id: str, logo_url: str) -> dict:
    await db.companies.update_one(
        {"id": company_id, "user_id": user_id},
        {"$set": {"logo_url": logo_url, "updated_at": datetime.utcnow()}},
    )
    return await get_company(company_id, user_id)
