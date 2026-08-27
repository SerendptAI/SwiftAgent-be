"""
Company user management service — invite, accept, suspend, role changes.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from app.core.database import db
from app.core.config import settings
from app.core.rbac import get_role_permissions, VALID_ROLES
from app.core.audit import AuditLogger
import logging
import secrets

logger = logging.getLogger(__name__)

INVITE_TOKEN_COLLECTION = "invite_tokens"
COMPANY_USERS_COLLECTION = "company_users"


async def ensure_company_user_indexes():
    """Create indexes for company user collections."""
    await db[COMPANY_USERS_COLLECTION].create_index("company_id")
    await db[COMPANY_USERS_COLLECTION].create_index("user_id")
    await db[COMPANY_USERS_COLLECTION].create_index(
        [("company_id", 1), ("user_id", 1)], unique=True
    )
    await db[INVITE_TOKEN_COLLECTION].create_index("token", unique=True)
    await db[INVITE_TOKEN_COLLECTION].create_index("expires_at", expireAfterSeconds=0)


async def create_invite(
    company_id: str,
    email: str,
    role: str,
    invited_by: str,
) -> dict:
    """
    Create an invite token for a new user.
    
    Args:
        company_id: The company to invite the user to
        email: Email address of the invitee
        role: Role to assign upon acceptance
        invited_by: User ID of the admin creating the invite
    
    Returns:
        dict with invite details including the token
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role: {role}")
    
    # Get company details for email
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise ValueError("Company not found")
    
    # Check if user already exists at this company
    existing = await db[COMPANY_USERS_COLLECTION].find_one({
        "company_id": company_id,
        "email": email.lower(),
    })
    if existing:
        raise ValueError("User already exists at this company")
    
    # Check for existing pending invite
    existing_invite = await db[INVITE_TOKEN_COLLECTION].find_one({
        "company_id": company_id,
        "email": email.lower(),
        "used_at": None,
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if existing_invite:
        raise ValueError("Pending invite already exists for this email")
    
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    
    invite_doc = {
        "token": token,
        "company_id": company_id,
        "email": email.lower(),
        "role": role,
        "invited_by": invited_by,
        "created_at": now,
        "expires_at": now + timedelta(hours=72),
        "used_at": None,
    }
    
    await db[INVITE_TOKEN_COLLECTION].insert_one(invite_doc)
    
    # Send invite email
    from app.services import invite_email_service
    frontend_url = getattr(settings, "FRONTEND_URL", "https://swiftagents.org").rstrip("/")
    accept_link = f"{frontend_url}/accept-invite?token={token}"
    company_name = company.get("name", "A Company")
    logo_url = company.get("logo_url") or f"{settings.API_BASE_URL}/images/logo_compliant.png"
    
    try:
        await invite_email_service.send_invite_email(
            to_email=email,
            company_name=company_name,
            accept_link=accept_link,
            company_logo_url=logo_url,
        )
        logger.info(f"Invite email sent to {email} for company {company_id}")
    except Exception as e:
        # Don't fail the invite if email fails, but log it
        logger.error(f"Failed to send invite email to {email}: {e}")
    
    # Log the invite
    await AuditLogger.log_event(
        actor_id=invited_by,
        company_id=company_id,
        resource_type="user",
        action="invite",
        metadata={"invited_email": email, "role": role},
    )
    
    return {
        "token": token,
        "email": email.lower(),
        "role": role,
        "expires_at": invite_doc["expires_at"].isoformat(),
    }


async def accept_invite(
    token: str,
    user_id: str,
    user_email: str,
) -> dict:
    """
    Accept an invite and create a company_user record.
    
    Args:
        token: The invite token
        user_id: The user ID of the accepting user
        user_email: Email of the accepting user
    
    Returns:
        The created company_user document
    """
    # Find and validate the invite
    invite = await db[INVITE_TOKEN_COLLECTION].find_one({"token": token})
    
    if not invite:
        raise ValueError("Invalid invite token")
    
    if invite.get("used_at"):
        raise ValueError("Invite has already been used")
    
    if invite["expires_at"] < datetime.now(timezone.utc):
        raise ValueError("Invite has expired")
    
    if invite["email"] != user_email.lower():
        raise ValueError("Invite email does not match your account email")
    
    company_id = invite["company_id"]
    role = invite["role"]
    
    # Check if user already exists at this company
    existing = await db[COMPANY_USERS_COLLECTION].find_one({
        "company_id": company_id,
        "user_id": user_id,
    })
    if existing:
        raise ValueError("You are already a member of this company")
    
    now = datetime.now(timezone.utc)
    
    # Create the company_user record
    company_user = {
        "id": str(uuid4()),
        "company_id": company_id,
        "user_id": user_id,
        "email": user_email.lower(),
        "role": role,
        "permissions": get_role_permissions(role),
        "invited_by": invite["invited_by"],
        "invited_at": invite["created_at"],
        "joined_at": now,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
    }
    
    await db[COMPANY_USERS_COLLECTION].insert_one(company_user)
    
    # Mark invite as used
    await db[INVITE_TOKEN_COLLECTION].update_one(
        {"token": token},
        {"$set": {"used_at": now, "accepted_by": user_id}},
    )
    
    # Log the acceptance
    await AuditLogger.log_event(
        actor_id=user_id,
        company_id=company_id,
        resource_type="user",
        action="invite_accepted",
        metadata={"role": role},
    )
    
    return company_user


async def get_company_user(company_id: str, user_id: str) -> Optional[dict]:
    """Get a company_user record."""
    doc = await db[COMPANY_USERS_COLLECTION].find_one({
        "company_id": company_id,
        "user_id": user_id,
    })
    if doc:
        doc.pop("_id", None)
    return doc


async def get_company_users(company_id: str) -> list[dict]:
    """Get all users for a company."""
    cursor = db[COMPANY_USERS_COLLECTION].find({"company_id": company_id})
    users = []
    async for doc in cursor:
        doc.pop("_id", None)
        users.append(doc)
    return users


async def update_user_role(
    company_id: str,
    user_id: str,
    new_role: str,
    updated_by: str,
) -> dict:
    """
    Update a user's role within a company.
    
    Args:
        company_id: The company ID
        user_id: The user to update
        new_role: The new role to assign
        updated_by: User ID of the admin making the change
    
    Returns:
        Updated company_user document
    """
    if new_role not in VALID_ROLES:
        raise ValueError(f"Invalid role: {new_role}")
    
    # Get current user for audit
    current = await db[COMPANY_USERS_COLLECTION].find_one({
        "company_id": company_id,
        "user_id": user_id,
    })
    
    if not current:
        raise ValueError("User not found at this company")
    
    old_role = current["role"]
    permissions = get_role_permissions(new_role)
    
    result = await db[COMPANY_USERS_COLLECTION].find_one_and_update(
        {"company_id": company_id, "user_id": user_id},
        {"$set": {
            "role": new_role,
            "permissions": permissions,
            "updated_at": datetime.now(timezone.utc),
        }},
        return_document=__import__('pymongo', fromlist=['ReturnDocument']).ReturnDocument.AFTER,
    )
    
    if result:
        result.pop("_id", None)
    
    # Log the role change
    await AuditLogger.log_event(
        actor_id=updated_by,
        company_id=company_id,
        resource_type="user",
        resource_id=user_id,
        action="role_change",
        before={"role": old_role},
        after={"role": new_role},
        changes=[f"role: {old_role} → {new_role}"],
    )
    
    return result


async def suspend_user(
    company_id: str,
    user_id: str,
    suspended_by: str,
) -> dict:
    """Suspend a user (soft delete, can be reactivated)."""
    result = await db[COMPANY_USERS_COLLECTION].find_one_and_update(
        {"company_id": company_id, "user_id": user_id},
        {"$set": {
            "is_active": False,
            "updated_at": datetime.now(timezone.utc),
        }},
        return_document=__import__('pymongo', fromlist=['ReturnDocument']).ReturnDocument.AFTER,
    )
    
    if not result:
        raise ValueError("User not found")
    
    result.pop("_id", None)
    
    await AuditLogger.log_event(
        actor_id=suspended_by,
        company_id=company_id,
        resource_type="user",
        resource_id=user_id,
        action="suspend",
    )
    
    return result


async def reactivate_user(
    company_id: str,
    user_id: str,
    reactivated_by: str,
) -> dict:
    """Reactivate a suspended user."""
    result = await db[COMPANY_USERS_COLLECTION].find_one_and_update(
        {"company_id": company_id, "user_id": user_id},
        {"$set": {
            "is_active": True,
            "updated_at": datetime.now(timezone.utc),
        }},
        return_document=__import__('pymongo', fromlist=['ReturnDocument']).ReturnDocument.AFTER,
    )
    
    if not result:
        raise ValueError("User not found")
    
    result.pop("_id", None)
    
    await AuditLogger.log_event(
        actor_id=reactivated_by,
        company_id=company_id,
        resource_type="user",
        resource_id=user_id,
        action="reactivate",
    )
    
    return result


async def remove_user(
    company_id: str,
    user_id: str,
    removed_by: str,
) -> bool:
    """Permanently remove a user from a company."""
    result = await db[COMPANY_USERS_COLLECTION].delete_one({
        "company_id": company_id,
        "user_id": user_id,
    })
    
    if result.deleted_count == 0:
        raise ValueError("User not found")
    
    await AuditLogger.log_event(
        actor_id=removed_by,
        company_id=company_id,
        resource_type="user",
        resource_id=user_id,
        action="remove",
    )
    
    return True


async def get_user_companies(user_id: str) -> list[dict]:
    """Get all companies a user belongs to."""
    cursor = db[COMPANY_USERS_COLLECTION].find({
        "user_id": user_id,
        "is_active": True,
    })
    companies = []
    async for doc in cursor:
        doc.pop("_id", None)
        companies.append(doc)
    return companies


async def backfill_existing_users():
    """
    Backfill existing users into company_users collection.
    Should be called once during migration.
    """
    # Get all companies
    companies = []
    async for doc in db.companies.find({}):
        companies.append(doc)
    
    for company in companies:
        company_id = company.get("id")
        owner_id = company.get("owner_id")
        
        if not company_id or not owner_id:
            continue
        
        # Check if owner already exists
        existing = await db[COMPANY_USERS_COLLECTION].find_one({
            "company_id": company_id,
            "user_id": owner_id,
        })
        
        if not existing:
            # Get user details
            user = await db.users.find_one({"user_id": owner_id})
            
            now = datetime.now(timezone.utc)
            company_user = {
                "id": str(uuid4()),
                "company_id": company_id,
                "user_id": owner_id,
                "email": user.get("email", "") if user else "",
                "role": "owner",
                "permissions": get_role_permissions("owner"),
                "invited_by": "system",
                "invited_at": now,
                "joined_at": now,
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
            
            await db[COMPANY_USERS_COLLECTION].insert_one(company_user)
            logger.info(f"Backfilled owner {owner_id} for company {company_id}")
