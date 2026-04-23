AFRICAN_COUNTRIES = [
    "Nigeria", "Ghana", "Kenya", "South Africa", "Egypt", 
    "Morocco", "Uganda", "Tanzania", "Ethiopia", "Rwanda", "Senegal"
]

TIER_LIMITS = {
    "basic": {
        "price_ngn": 25000,
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
        "price_ngn": 45000,
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
        "price_ngn": 80000,
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
