# Timezone prefix used to detect African users via browser timezone
AFRICAN_TIMEZONE_PREFIX = "Africa/"

def is_african_timezone(tz: str | None) -> bool:
    """Check if a timezone string belongs to an African region."""
    if not tz:
        return False
    return tz.startswith(AFRICAN_TIMEZONE_PREFIX)

TIER_LIMITS = {
    "basic": {
        "price_usd_af": 18,
        "price_usd_intl": 200,
        "price_usd_intl_discounted": 100,
        "trial_months_af": 6,
        "display_name": "Basic",
        "agents_limit": 1,
        "documents_limit": 10,
        "languages_limit": 1,
        "answer_boundaries": "basic",
        "analytics": "basic",
        "voice_minutes_per_month": 800,
        "compute_tier": "standard_shared",
        "members_per_company": 3,
        "companies_per_user": 1,
    },
    "pro": {
        "price_usd_af": 34,
        "price_usd_intl": 700,
        "display_name": "Pro",
        "agents_limit": 3,
        "documents_limit": 50,
        "languages_limit": 3,
        "answer_boundaries": "advanced",
        "analytics": "advanced",
        "voice_minutes_per_month": 3000,
        "compute_tier": "priority",
        "members_per_company": 10,
        "companies_per_user": 3,
    },
    "enterprise": {
        "price_usd_af": 60,
        "price_usd_intl": 1700,
        "display_name": "Enterprise",
        "agents_limit": -1, # -1 signifies unlimited
        "documents_limit": -1,
        "languages_limit": -1,
        "answer_boundaries": "custom",
        "analytics": "custom",
        "voice_minutes_per_month": -1, 
        "compute_tier": "dedicated",
        "members_per_company": -1,
        "companies_per_user": -1,
    }
}

# Duration of a subscription cycle in days
SUBSCRIPTION_DURATION_DAYS = 30


def get_tier_limits(tier: str) -> dict:
    """Get limits for a given tier, defaulting to basic."""
    return TIER_LIMITS.get(tier, TIER_LIMITS["basic"])


def get_display_name(tier: str) -> str:
    """Get the frontend-facing display name for a tier."""
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["basic"])
    return limits["display_name"]


def is_unlimited(value: int) -> bool:
    """Check if a limit value represents unlimited (-1)."""
    return value == -1
