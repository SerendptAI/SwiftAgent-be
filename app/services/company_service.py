from datetime import datetime, timezone
from uuid import uuid4
from app.core.database import db
from app.core.cache import company_cache
from app.core.config import settings
from typing import Optional
from pymongo import ReturnDocument
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
    if not email_slug:
        email_slug = await suggest_slug(data["name"])

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
        "support_emails": (
            [data["support_email"].strip().lower()]
            if data.get("support_email") and str(data.get("support_email")).strip()
            else []
        ),
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
        # subscription — default to basic active
        "subscription_tier": "basic",
        "subscription_status": "active",
        "subscription_started_at": datetime.now(tz=timezone.utc),
        "billing_provider": None,
        "subscription_id": None,
        "customer_id": None,
        "members": [],
        "pending_invites": [],
        "created_at": datetime.now(tz=timezone.utc),
        "updated_at": datetime.now(tz=timezone.utc),
    }
    await db.companies.insert_one(doc)
    await company_cache.set(f"company:{doc['id']}", doc)
    return doc


async def get_company(company_id: str, user_id: Optional[str] = None, admin_only: bool = False) -> Optional[dict]:
    cache_key = f"company:{company_id}:{user_id or 'public'}:{admin_only}"
    cached = await company_cache.get(cache_key)
    if cached is not None:
        return cached

    query: dict = {"id": company_id}
    if user_id:
        if admin_only:
            query["user_id"] = user_id
        else:
            query["$or"] = [{"user_id": user_id}, {"members.user_id": user_id}]

    company = await db.companies.find_one(query)
    if company:
        await company_cache.set(cache_key, company)
    return company


async def get_company_by_slug(slug: str) -> Optional[dict]:
    """Look up a company by its email slug."""
    return await db.companies.find_one({"email_slug": slug})


async def invalidate_company_cache(company_id: str):
    """Invalidate all cached entries for a company using thread-safe prefix deletion."""
    await company_cache.delete_by_prefix(f"company:{company_id}:")


async def list_companies(user_id: str) -> list:
    cursor = db.companies.find(
        {"$or": [{"user_id": user_id}, {"members.user_id": user_id}]},
        {"description": 0, "customer_value": 0, "enabled_sources": 0, "custom_info": 0},
    )
    return await cursor.to_list(length=50)


async def _update_and_return(
    company_id: str, user_id: str, update_fields: dict, admin_only: bool = False
) -> dict:
    update_fields["updated_at"] = datetime.now(tz=timezone.utc)
    
    query: dict = {"id": company_id}
    if admin_only:
        query["user_id"] = user_id
    else:
        query["$or"] = [{"user_id": user_id}, {"members.user_id": user_id}]

    result = await db.companies.find_one_and_update(
        query,
        {"$set": update_fields},
        return_document=ReturnDocument.AFTER,
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
        {"company_type": company_type, "onboarding_step": 3, "setup_complete": True},
        admin_only=True
    )


async def update_company_info(company_id: str, user_id: str, data: dict) -> dict:
    update = {k: v for k, v in data.items() if v is not None}
    return await _update_and_return(company_id, user_id, update)


async def update_security(company_id: str, user_id: str, data: dict) -> dict:
    update = {k: v for k, v in data.items() if v is not None}
    return await _update_and_return(company_id, user_id, update, admin_only=True)





async def update_logo(company_id: str, user_id: str, logo_url: str) -> dict:
    return await _update_and_return(company_id, user_id, {"logo_url": logo_url})


async def update_email_slug(company_id: str, user_id: str, slug: str) -> dict:
    """Set or update the company's email slug."""
    email_address = _compute_email_address(slug)
    return await _update_and_return(
        company_id, user_id, {"email_slug": slug, "email_address": email_address}
    )

from datetime import timedelta
from app.services import invite_email_service
import secrets

async def create_invite(company_id: str, admin_user_id: str, email: str) -> dict:
    company = await db.companies.find_one({"id": company_id, "user_id": admin_user_id})
    if not company:
        raise ValueError("Company not found or unauthorized")
        
    email = email.lower().strip()
    
    if any(m.get("email") == email for m in company.get("members", [])):
        raise ValueError("User is already a member")
        
    # check existing invites
    now = datetime.now(tz=timezone.utc)
    pending = company.get("pending_invites", [])
    filtered_pending = []
    
    for inv in pending:
        if inv.get("email") == email:
            # check age
            age = now - inv.get("invited_at", now)
            if age.days < 10:
                raise ValueError("An active invite already exists for this email")
        else:
            filtered_pending.append(inv)
            
    token = secrets.token_urlsafe(32)
    new_invite = {
        "email": email,
        "token": token,
        "invited_at": now
    }
    filtered_pending.append(new_invite)
    
    await db.companies.update_one(
        {"id": company_id},
        {"$set": {"pending_invites": filtered_pending}}
    )
    # Send the user to a backend GET endpoint that handles acceptance and redirects
    accept_link = f"{settings.API_BASE_URL}/api/v1/companies/invites/accept?token={token}"
    await invite_email_service.send_invite_email(email, company.get("name", "A Company"), accept_link)
    
    await invalidate_company_cache(company_id)
    return new_invite

async def accept_invite(token: str) -> dict:
    company = await db.companies.find_one({"pending_invites.token": token})
    if not company:
        raise ValueError("Invalid or expired invite token")
        
    invite = next((i for i in company.get("pending_invites", []) if i.get("token") == token), None)
    if not invite:
        raise ValueError("Invite not found")
        
    now = datetime.now(tz=timezone.utc)
    invited_at = invite.get("invited_at")
    if invited_at is None:
        raise ValueError("Invite has expired")
    if invited_at.tzinfo is None:
        invited_at = invited_at.replace(tzinfo=timezone.utc)
    if (now - invited_at).days >= 10:
        raise ValueError("Invite has expired")
        
    email = invite.get("email")
    
    # resolve or create user
    user = await db.users.find_one({"email": email})
    user_id = ""
    if not user:
        from app.services.credential_auth_service import build_new_passwordless_user
        user_id = str(uuid4())
        new_user = build_new_passwordless_user(None, email, None, 0)
        new_user["user_id"] = user_id
        await db.users.insert_one(new_user)
    else:
        user_id = user["user_id"]
        
    # remove from pending, add to members
    members = company.get("members", [])
    if not any(m.get("user_id") == user_id for m in members):
        members.append({
            "user_id": user_id,
            "email": email,
            "role": "member",
            "added_at": now
        })
        
    remaining_invites = [i for i in company.get("pending_invites", []) if i.get("token") != token]
    
    await db.companies.update_one(
        {"id": company["id"]},
        {"$set": {"members": members, "pending_invites": remaining_invites}}
    )
    
    await invalidate_company_cache(company["id"])
    return {"company_id": company["id"], "user_id": user_id}

async def remove_member_or_invite(company_id: str, admin_user_id: str, email: str) -> bool:
    email = email.lower().strip()
    company = await db.companies.find_one({"id": company_id, "user_id": admin_user_id})
    if not company:
        raise ValueError("Company not found or unauthorized")
        
    members = [m for m in company.get("members", []) if m.get("email") != email]
    pending = [i for i in company.get("pending_invites", []) if i.get("email") != email]
    
    await db.companies.update_one(
        {"id": company_id},
        {"$set": {"members": members, "pending_invites": pending}}
    )
    
    await invalidate_company_cache(company_id)
    return True

async def get_unified_members(company_id: str, admin_user_id: str) -> list:
    company = await db.companies.find_one({"id": company_id, "$or": [{"user_id": admin_user_id}, {"members.user_id": admin_user_id}]})
    if not company:
        raise ValueError("Company not found or unauthorized")
        
    owner = await db.users.find_one({"user_id": company["user_id"]})
    owner_email = owner.get("email") if owner else ""
    
    unified = []
    unified.append({
        "email": owner_email,
        "role": "Admin",
        "status": "Active"
    })
    
    for m in company.get("members", []):
        unified.append({
            "email": m.get("email"),
            "role": "Member",
            "status": "Active"
        })
        
    now = datetime.now(tz=timezone.utc)
    for i in company.get("pending_invites", []):
        age = now - i.get("invited_at", now)
        status = "Pending" if age.days < 10 else "Expired"
        unified.append({
            "email": i.get("email"),
            "role": "Member",
            "status": status
        })
        
    return unified
