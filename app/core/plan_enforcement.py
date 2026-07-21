"""
Plan enforcement helpers.

Each enforce_* function checks whether the current usage for a company or user
exceeds the limit defined by their subscription tier.  When the limit is
exceeded an HTTPException(402) is raised with a user-friendly message that uses
the frontend tier names (Basic / Pro / Enterprise).

Subscription expiry is also checked: a company whose subscription has lapsed
(>30 days since subscription_started_at) is treated as having no active plan.
"""

from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from app.core.database import db
from app.core.billing_limits import (
    TIER_LIMITS,
    SUBSCRIPTION_DURATION_DAYS,
    get_tier_limits,
    get_display_name,
    is_unlimited,
)
import logging

logger = logging.getLogger(__name__)


def _is_subscription_active(company: dict) -> bool:
    """
    Check if the company has a currently-valid subscription.
    A subscription is active when:
      - subscription_status == "active" (or "canceled" but unexpired)
      - now < subscription_expires_at
    """
    status = company.get("subscription_status", "inactive")
    if status not in ("active", "canceled"):
        return False

    expiry = get_subscription_expiry(company)
    if not expiry:
        # Legacy companies migrated without a start date are assumed active
        if not company.get("subscription_started_at"):
            return True
        return False

    now = datetime.now(tz=timezone.utc)
    return now < expiry


def get_active_tier(company: dict) -> str:
    """Return the effective tier for a company, falling back to none if expired."""
    if _is_subscription_active(company):
        return company.get("subscription_tier", "none") or "none"
    # Expired subscription — treat as none
    return "none"


def get_subscription_expiry(company: dict) -> datetime | None:
    """Return the subscription expiry datetime, or None if no start date."""
    expires_at = company.get("subscription_expires_at")
    if expires_at:
        if isinstance(expires_at, str):
            if expires_at.endswith("Z"):
                expires_at = expires_at[:-1] + "+00:00"
            expires_at = datetime.fromisoformat(expires_at)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at

    started_at = company.get("subscription_started_at")
    if not started_at:
        return None
    if isinstance(started_at, str):
        if started_at.endswith("Z"):
            started_at = started_at[:-1] + "+00:00"
        started_at = datetime.fromisoformat(started_at)
    
    # Ensure started_at is timezone-aware (assume UTC if naive)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    
    return started_at + timedelta(days=SUBSCRIPTION_DURATION_DAYS)


def _upgrade_message(tier: str, resource: str, limit: int | float | None = None) -> str:
    """Build a user-friendly upgrade prompt."""
    display = get_display_name(tier)
    next_tiers = {
        "none": "a paid plan",
        "basic": "Pro or Enterprise",
        "pro": "Enterprise",
        "enterprise": "",
    }
    upgrade_hint = next_tiers.get(tier, "a higher plan")
    limit_text = f" (Limit: {limit})" if limit is not None else ""
    if upgrade_hint:
        return f"Your {display} plan limit for {resource} has been reached. Upgrade to {upgrade_hint} for more.{limit_text}"
    return f"Your {display} plan limit for {resource} has been reached.{limit_text}"


async def enforce_company_limit(user_id: str) -> None:
    """
    Ensure the user hasn't exceeded companies_per_user for their highest-tier
    owned company.  Only counts companies where user_id is the owner (admin).
    """
    owned = await db.companies.find({"user_id": user_id}).to_list(length=100)
    if not owned:
        # First company — always allowed (defaults to basic)
        return

    # Determine the user's effective tier from their highest-tier company
    tier_order = {"enterprise": 3, "pro": 2, "basic": 1, "none": 0}
    best_tier = "none"
    for c in owned:
        t = get_active_tier(c)
        if tier_order.get(t, 0) > tier_order.get(best_tier, 0):
            best_tier = t

    limits = get_tier_limits(best_tier)
    max_companies = limits["companies_per_user"]

    if is_unlimited(max_companies):
        return

    if len(owned) >= max_companies:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "company_limit_reached",
                "message": _upgrade_message(best_tier, "companies", max_companies)
            },
        )


async def enforce_agent_limit(company: dict) -> None:
    """
    Ensure the company hasn't exceeded agents_limit (stroll configs count).
    Called when creating a NEW stroll config, not when updating an existing one.
    """
    tier = get_active_tier(company)
    limits = get_tier_limits(tier)
    max_agents = limits["agents_limit"]

    if is_unlimited(max_agents):
        return

    company_id = company["id"]
    current_count = await db.stroll_configs.count_documents({"company_id": company_id})

    if current_count >= max_agents:
        raise HTTPException(
            status_code=402,
            detail=_upgrade_message(tier, "deployed agents", max_agents),
        )


async def enforce_document_limit(company: dict, is_onboarding: bool = False) -> None:
    """
    Ensure the company hasn't exceeded documents_limit.
    Counts entries in knowledge_sources for the company.
    """
    # During onboarding, allow uploads even on the "none" plan
    if is_onboarding and not company.get("setup_complete", False):
        return

    tier = get_active_tier(company)
    limits = get_tier_limits(tier)
    max_docs = limits["documents_limit"]

    if is_unlimited(max_docs):
        return

    company_id = company["id"]
    current_count = await db.knowledge_sources.count_documents({"company_id": company_id})

    if current_count >= max_docs:
        raise HTTPException(
            status_code=402,
            detail=_upgrade_message(tier, "document uploads", max_docs),
        )


async def enforce_member_limit(company: dict) -> None:
    """
    Ensure the company hasn't exceeded members_per_company.
    Counts active members + non-expired pending invites.
    """
    tier = get_active_tier(company)
    limits = get_tier_limits(tier)
    max_members = limits["members_per_company"]

    if is_unlimited(max_members):
        return

    active_members = len(company.get("members", []))

    now = datetime.now(tz=timezone.utc)
    active_invites = 0
    for inv in company.get("pending_invites", []):
        invited_at = inv.get("invited_at", now)
        if invited_at.tzinfo is None:
            invited_at = invited_at.replace(tzinfo=timezone.utc)
        if (now - invited_at).days < 10:
            active_invites += 1

    total = active_members + active_invites

    if total >= max_members:
        raise HTTPException(
            status_code=402,
            detail=_upgrade_message(tier, "team members", max_members),
        )


async def enforce_chat_limit(company: dict) -> None:
    """
    Ensure the company hasn't exceeded agent_chats_per_month.
    Counts the total number of messages across all conversations in the current billing month.
    """
    tier = get_active_tier(company)
    limits = get_tier_limits(tier)
    max_chats = limits.get("agent_chats_per_month", 0)

    if is_unlimited(max_chats):
        return

    company_id = company["id"]
    now = datetime.now(tz=timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    pipeline = [
        {
            "$match": {
                "company_id": company_id,
                "created_at": {"$gte": month_start}
            }
        },
        {
            "$project": {
                "message_count": {"$size": {"$ifNull": ["$messages", []]}}
            }
        },
        {
            "$group": {
                "_id": None,
                "total_messages": {"$sum": "$message_count"}
            }
        }
    ]

    result = await db.widget_conversations.aggregate(pipeline).to_list(length=1)
    total_chats = result[0]["total_messages"] if result else 0

    if total_chats >= max_chats:
        raise HTTPException(
            status_code=402,
            detail=_upgrade_message(tier, "agent chats", max_chats),
        )


async def enforce_stroll_limit(company: dict) -> None:
    """
    Ensure the company hasn't exceeded strolls_per_month.
    Counts the number of stroll versions executed in the current billing month.
    """
    tier = get_active_tier(company)
    limits = get_tier_limits(tier)
    max_strolls = limits.get("strolls_per_month", 0)

    if is_unlimited(max_strolls):
        return

    company_id = company["id"]
    now = datetime.now(tz=timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    current_count = await db.stroll_versions.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": month_start},
        "status": "success"
    })

    if current_count >= max_strolls:
        raise HTTPException(
            status_code=402,
            detail=_upgrade_message(tier, "automated strolls", max_strolls),
        )


async def get_usage_summary(company: dict) -> dict:
    """
    Return a dict of current usage counts for the company,
    alongside matching limits for comparison.
    """
    company_id = company["id"]
    tier = get_active_tier(company)
    limits = get_tier_limits(tier)

    # agents (stroll configs)
    agents_count = await db.stroll_configs.count_documents({"company_id": company_id})

    # documents
    docs_count = await db.knowledge_sources.count_documents({"company_id": company_id})

    # members
    active_members = len(company.get("members", []))
    now = datetime.now(tz=timezone.utc)
    active_invites = 0
    for inv in company.get("pending_invites", []):
        invited_at = inv.get("invited_at", now)
        if invited_at.tzinfo is None:
            invited_at = invited_at.replace(tzinfo=timezone.utc)
        if (now - invited_at).days < 10:
            active_invites += 1
    members_count = active_members + active_invites

    # agent chats this month
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    chat_pipeline = [
        {
            "$match": {
                "company_id": company_id,
                "created_at": {"$gte": month_start}
            }
        },
        {
            "$project": {
                "message_count": {"$size": {"$ifNull": ["$messages", []]}}
            }
        },
        {
            "$group": {
                "_id": None,
                "total_messages": {"$sum": "$message_count"}
            }
        }
    ]
    chat_result = await db.widget_conversations.aggregate(chat_pipeline).to_list(length=1)
    chats_used = chat_result[0]["total_messages"] if chat_result else 0

    # strolls this month
    strolls_used = await db.stroll_versions.count_documents({
        "company_id": company_id,
        "timestamp": {"$gte": month_start},
        "status": "success"
    })

    return {
        "tier": tier,
        "display_name": get_display_name(tier),
        "subscription_status": company.get("subscription_status", "inactive"),
        "subscription_started_at": company.get("subscription_started_at"),
        "subscription_expires_at": get_subscription_expiry(company),
        "usage": {
            "agents": {"used": agents_count, "limit": limits["agents_limit"]},
            "documents": {
                "used": docs_count, 
                "limit": -1 if not company.get("setup_complete", False) else limits["documents_limit"]
            },
            "members": {"used": members_count, "limit": limits["members_per_company"]},
            "agent_chats": {
                "used": chats_used,
                "limit": limits.get("agent_chats_per_month", 0),
            },
            "strolls": {
                "used": strolls_used,
                "limit": limits.get("strolls_per_month", 0),
            },
        },
        "features": {
            "answer_boundaries": limits["answer_boundaries"],
            "analytics": limits["analytics"],
            "compute_tier": limits["compute_tier"],
        },
    }
