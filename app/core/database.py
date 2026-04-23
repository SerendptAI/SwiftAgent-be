from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
import redis.asyncio as redis

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

redis_client = redis.from_url(
    settings.REDIS_URL,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    decode_responses=True,
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

    await db.visitors.create_index("company_id")
    await db.visitors.create_index([("company_id", 1), ("visitor_id", 1), ("timestamp", 1)])

    await db.calls.create_index("company_id")
    await db.calls.create_index([("company_id", 1), ("timestamp", -1)])

    await db.knowledge_sources.create_index("company_id")
    await db.knowledge_sources.create_index([("company_id", 1), ("uploaded_at", -1)])

    await db.stroll_versions.create_index("company_id")
    await db.stroll_versions.create_index([("company_id", 1), ("status", 1), ("timestamp", -1)])

    await db.stroll_configs.create_index("company_id", unique=True)

    await db.conversations.create_index("user_id")
    await db.conversations.create_index([("user_id", 1), ("updated_at", -1)])

    await db.documents.create_index("user_id")
    await db.users.create_index("user_id", unique=True)

    # credential auth — query performance indexes (always safe)
    await db.users.create_index([("email", 1), ("is_verified", 1)])
    await db.users.create_index("otp_expires", expireAfterSeconds=0, sparse=True)

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
