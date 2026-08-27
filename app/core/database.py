from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient

from app.core.config import settings

mongo_client = AsyncIOMotorClient(
    settings.MONGO_URI,
    maxPoolSize=settings.MONGO_MAX_POOL_SIZE,
    minPoolSize=settings.MONGO_MIN_POOL_SIZE,
    serverSelectionTimeoutMS=5000,
)
db = mongo_client[settings.MONGO_DB_NAME]

qdrant_client = AsyncQdrantClient(
    url=settings.QDRANT_URL,
    api_key=settings.QDRANT_API_KEY,
)


async def get_database():
    return db


async def get_qdrant_client():
    return qdrant_client


async def create_indexes():
    """Create database indexes for optimal query performance."""
    await db.companies.create_index("user_id")
    await db.companies.create_index("id", unique=True)
    await db.companies.create_index([("user_id", 1), ("setup_complete", 1)])

    await db.widget_conversations.create_index("company_id")
    await db.widget_conversations.create_index("session_id")
    await db.widget_conversations.create_index([("company_id", 1), ("session_id", 1)], unique=True)
    await db.widget_conversations.create_index([("company_id", 1), ("created_at", -1)])
    await db.widget_conversations.create_index([("created_at", -1)])

    await db.visitors.create_index("company_id")
    await db.visitors.create_index([("company_id", 1), ("visitor_id", 1), ("timestamp", 1)])

    await db.calls.create_index("company_id")
    await db.calls.create_index([("company_id", 1), ("timestamp", -1)])

    await db.knowledge_sources.create_index("company_id")
    await db.knowledge_sources.create_index([("company_id", 1), ("uploaded_at", -1)])

    # gdpr export/deletion jobs
    await db.gdpr_export_jobs.create_index([("company_id", 1), ("created_at", -1)])
    await db.gdpr_deletion_requests.create_index(
        [("company_id", 1), ("created_at", -1)]
    )

    # knowledge base auto-crawl
    await db.knowledge_crawl_configs.create_index("company_id", unique=True)
    await db.knowledge_crawl_configs.create_index([("enabled", 1), ("next_run_at", 1)])
    await db.knowledge_crawl_runs.create_index("id", unique=True)
    await db.knowledge_crawl_runs.create_index([("company_id", 1), ("started_at", -1)])
    await db.knowledge_pages.create_index("id", unique=True)
    await db.knowledge_pages.create_index(
        [("company_id", 1), ("canonical_url", 1)], unique=True
    )
    await db.knowledge_pages.create_index([("company_id", 1), ("status", 1)])
    await db.knowledge_gap_events.create_index([("company_id", 1), ("detected_at", -1)])
    await db.knowledge_gap_events.create_index("session_id", sparse=True)
    await db.knowledge_gaps.create_index("id", unique=True)
    await db.knowledge_gaps.create_index(
        [("company_id", 1), ("topic_key", 1), ("status", 1)]
    )
    await db.knowledge_gaps.create_index(
        [("company_id", 1), ("status", 1), ("priority_score", -1)]
    )

    await db.stroll_versions.create_index("company_id")
    await db.stroll_versions.create_index([("company_id", 1), ("status", 1), ("timestamp", -1)])

    await db.stroll_configs.create_index("company_id", unique=True)

    await db.conversations.create_index("user_id")
    await db.conversations.create_index([("user_id", 1), ("updated_at", -1)])

    await db.documents.create_index("user_id")
    await db.users.create_index("user_id", unique=True)

    # credential auth — query performance indexes (always safe)
    await db.users.create_index([("email", 1), ("is_verified", 1)])
    # CRITICAL FIX: Removed TTL index on otp_expires. It was deleting entire existing user accounts 
    # if they requested an OTP but didn't enter it before it expired.

    # unique email index — skipped if duplicate data exists in the collection.
    try:
        await db.users.create_index("email", unique=True, sparse=True)
    except Exception as e:
        import logging as _log

        _log.getLogger(__name__).warning(
            "Could not create unique email index (duplicate data exists): %s. "
            "Run scripts/dedup_users.py to clean up, then restart.",
            e,
        )

    # email tickets
    await db.email_tickets.create_index("company_id")
    await db.email_tickets.create_index([("company_id", 1), ("status", 1), ("updated_at", -1)])
    await db.email_tickets.create_index([("company_id", 1), ("updated_at", -1)])
    await db.email_tickets.create_index([("status", 1), ("updated_at", -1)])
    await db.email_tickets.create_index([("updated_at", -1)])
    await db.email_tickets.create_index("customer_email")
    await db.email_tickets.create_index("resolve_token", unique=True)

    # registrations
    await db.pending_registrations.create_index("company_email", unique=True)
    await db.pending_registrations.create_index("token", unique=True)

    # email slug uniqueness
    await db.companies.create_index("email_slug", unique=True, sparse=True)
    
    # members
    await db.companies.create_index("members.user_id")
    await db.companies.create_index("members.email")
    await db.companies.create_index("pending_invites.token")

    # memory indexes
    await db.episodic_episodes.create_index("session_id")
    await db.episodic_episodes.create_index("company_id")
    await db.episodic_episodes.create_index("user_id")
    await db.episodic_episodes.create_index([("company_id", 1), ("created_at", -1)])

    await db.episodic_events.create_index("session_id")
    await db.episodic_events.create_index([("session_id", 1), ("timestamp", -1)])

    # OTP challenge relay (stroll 2FA)
    await db.device_tokens.create_index("user_id")
    await db.device_tokens.create_index([("user_id", 1), ("device_token", 1)], unique=True)

    await db.otp_challenges.create_index("company_id")
    await db.otp_challenges.create_index([("user_id", 1), ("status", 1)])
    await db.otp_challenges.create_index("expires_at", expireAfterSeconds=0)

    # Forms
    await db.forms.create_index("company_id")
    await db.forms.create_index([("company_id", 1), ("created_at", -1)])
    await db.form_submissions.create_index("form_id")
    await db.form_submissions.create_index("company_id")
    await db.form_submissions.create_index([("company_id", 1), ("is_read", 1)])
    await db.form_submissions.create_index([("company_id", 1), ("submitted_at", -1)])
    await db.form_submissions.create_index([("form_id", 1), ("submitted_at", -1)])
    await db.form_submissions.create_index([("submitted_at", -1)])

    # Form keys (SDPK key pairs for widget auth)
    try:
        await db.form_keys.drop_index("form_id_1")
    except Exception:
        pass
    await db.form_keys.create_index(
        "form_id", unique=True, partialFilterExpression={"active": True}
    )
    await db.form_keys.create_index("public_key_hash")
    await db.form_keys.create_index("api_key_hash")

    # Form group names (custom names for auto-detected forms on pages)
    await db.form_group_names.create_index(
        [("form_id", 1), ("page_path", 1), ("form_identifier", 1)],
        unique=True,
    )

    # Form submissions — page/form level queries for hierarchy view
    await db.form_submissions.create_index([("form_id", 1), ("page_url", 1), ("submitted_at", -1)])
    await db.form_submissions.create_index([("form_id", 1), ("page_url", 1), ("form_identifier", 1)])

    # Company API Integrations
    await db.company_integrations.create_index("company_id")
    await db.company_integrations.create_index(
        [("company_id", 1), ("active", 1)],
    )
    await db.company_integrations.create_index(
        [("company_id", 1), ("name", 1)],
        unique=True,
    )

    # Pending meter events (reliable billing queue)
    await db.pending_meter_events.create_index(
        [("status", 1), ("next_retry_at", 1)]
    )
    await db.pending_meter_events.create_index("company_id")

    # Conversation intelligence indexes
    await db.conversation_intelligence.create_index("session_id", unique=True)
    await db.conversation_intelligence.create_index("company_id")
    await db.conversation_intelligence.create_index([("company_id", 1), ("analyzed_at", -1)])
    await db.conversation_intelligence.create_index([("company_id", 1), ("analyzed_at", -1), ("intent.primary", 1)])
    await db.conversation_intelligence.create_index([("company_id", 1), ("tags.tag", 1)])
    await db.conversation_intelligence.create_index("requires_human_review")
    await db.conversation_intelligence.create_index([("company_id", 1), ("requires_human_review", 1)])
    await db.message_sentiment.create_index("session_id")
    await db.message_sentiment.create_index([("session_id", 1), ("turn_index", 1)])
