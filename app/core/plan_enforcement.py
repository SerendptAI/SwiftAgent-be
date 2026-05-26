"""
Plan enforcement helpers.

Each enforce_* function checks whether the current usage for a company or user
exceeds the limit defined by their subscription tier.  When the limit is
exceeded an HTTPException(403) is raised with a user-friendly message that uses
the frontend tier names (Yellow Pill / Purple Pill / Orange Pill).

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
      - subscription_status == "active"
      - subscription_started_at exists and is within the last SUBSCRIPTION_DURATION_DAYS
    """
    status = company.get("subscription_status", "inactive")
    if status not in ("active", "canceled"):
        return False

    started_at = company.get("subscription_started_at")
    if not started_at:
        # Legacy companies migrated without a start date are assumed active
        return True

    if isinstance(started_at, str):
        started_at = datetime.fromisoformat(started_at)

    # Ensure started_at is timezone-aware (assume UTC if naive)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)

    now = datetime.now(tz=timezone.utc)
    expiry = started_at + timedelta(days=SUBSCRIPTION_DURATION_DAYS)
    return now < expiry


def get_active_tier(company: dict) -> str:
    """Return the effective tier for a company, falling back to basic if expired."""
    if _is_subscription_active(company):
        return company.get("subscription_tier", "basic") or "basic"
    # Expired subscription — treat as basic
    return "basic"


def get_subscription_expiry(company: dict) -> datetime | None:
    """Return the subscription expiry datetime, or None if no start date."""
    started_at = company.get("subscription_started_at")
    if not started_at:
        return None
    if isinstance(started_at, str):
        started_at = datetime.fromisoformat(started_at)
    
    # Ensure started_at is timezone-aware (assume UTC if naive)
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    
    return started_at + timedelta(days=SUBSCRIPTION_DURATION_DAYS)


def _upgrade_message(tier: str, resource: str) -> str:
    """Build a user-friendly upgrade prompt."""
    display = get_display_name(tier)
    next_tiers = {
        "basic": "Pro or Enterprise",
        "pro": "Enterprise",
        "enterprise": "",
    }
    upgrade_hint = next_tiers.get(tier, "a higher plan")
    if upgrade_hint:
        return f"Your {display} plan limit for {resource} has been reached. Upgrade to {upgrade_hint} for more."
    return f"Your {display} plan limit for {resource} has been reached."


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
    tier_order = {"enterprise": 3, "pro": 2, "basic": 1}
    best_tier = "basic"
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
            status_code=400,
            detail=_upgrade_message(best_tier, "companies"),
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
            status_code=400,
            detail=_upgrade_message(tier, "deployed agents"),
        )


async def enforce_document_limit(company: dict) -> None:
    """
    Ensure the company hasn't exceeded documents_limit.
    Counts entries in knowledge_sources for the company.
    """
    tier = get_active_tier(company)
    limits = get_tier_limits(tier)
    max_docs = limits["documents_limit"]

    if is_unlimited(max_docs):
        return

    company_id = company["id"]
    current_count = await db.knowledge_sources.count_documents({"company_id": company_id})

    if current_count >= max_docs:
        raise HTTPException(
            status_code=400,
            detail=_upgrade_message(tier, "document uploads"),
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
        if (now - invited_at).days < 10:
            active_invites += 1

    total = active_members + active_invites

    if total >= max_members:
        raise HTTPException(
            status_code=400,
            detail=_upgrade_message(tier, "team members"),
        )


async def enforce_voice_minutes(company: dict) -> None:
    """
    Ensure the company hasn't exceeded voice_minutes_per_month.
    Tallies duration_seconds from the calls collection for the current billing month.
    """
    tier = get_active_tier(company)
    limits = get_tier_limits(tier)
    max_minutes = limits["voice_minutes_per_month"]

    if is_unlimited(max_minutes):
        return

    company_id = company["id"]

    # Current billing month window
    now = datetime.now(tz=timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    pipeline = [
        {
            "$match": {
                "company_id": company_id,
                "timestamp": {"$gte": month_start},
                "duration_seconds": {"$exists": True},
            }
        },
        {
            "$group": {
                "_id": None,
                "total_seconds": {"$sum": "$duration_seconds"},
            }
        },
    ]

    result = await db.calls.aggregate(pipeline).to_list(length=1)
    total_seconds = result[0]["total_seconds"] if result else 0
    total_minutes = total_seconds / 60

    if total_minutes >= max_minutes:
        raise HTTPException(
            status_code=400,
            detail=_upgrade_message(tier, "voice minutes"),
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
    active_invites = sum(
        1 for inv in company.get("pending_invites", [])
        if (now - inv.get("invited_at", now)).days < 10
    )
    members_count = active_members + active_invites

    # voice minutes this month
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    pipeline = [
        {
            "$match": {
                "company_id": company_id,
                "timestamp": {"$gte": month_start},
                "duration_seconds": {"$exists": True},
            }
        },
        {
            "$group": {
                "_id": None,
                "total_seconds": {"$sum": "$duration_seconds"},
            }
        },
    ]
    result = await db.calls.aggregate(pipeline).to_list(length=1)
    voice_seconds = result[0]["total_seconds"] if result else 0

    return {
        "tier": tier,
        "display_name": get_display_name(tier),
        "subscription_status": company.get("subscription_status", "inactive"),
        "subscription_started_at": company.get("subscription_started_at"),
        "subscription_expires_at": get_subscription_expiry(company),
        "usage": {
            "agents": {"used": agents_count, "limit": limits["agents_limit"]},
            "documents": {"used": docs_count, "limit": limits["documents_limit"]},
            "members": {"used": members_count, "limit": limits["members_per_company"]},
            "voice_minutes": {
                "used": round(voice_seconds / 60, 1),
                "limit": limits["voice_minutes_per_month"],
            },
        },
        "features": {
            "answer_boundaries": limits["answer_boundaries"],
            "analytics": limits["analytics"],
            "compute_tier": limits["compute_tier"],
        },
    }
