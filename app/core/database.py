from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from app.core.config import settings

# mongodb setup
mongo_client = AsyncIOMotorClient(settings.MONGO_URI)
db = mongo_client[settings.MONGO_DB_NAME]

# qdrant setup
qdrant_client = AsyncQdrantClient(
    url=settings.QDRANT_URL,
    api_key=settings.QDRANT_API_KEY,
)

async def get_database():
    return db

async def get_qdrant_client():
    return qdrant_client
