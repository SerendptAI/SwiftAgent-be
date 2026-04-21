"""
Health check endpoint for container orchestration.
"""

from fastapi import APIRouter
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient

from app.core.database import db, qdrant_client, redis_client

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check():
    """Basic health check."""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness_check():
    """Deep health check for container orchestration."""
    checks = {
        "mongodb": {"status": "unhealthy"},
        "qdrant": {"status": "unhealthy"},
        "redis": {"status": "unhealthy"},
    }

    # Check MongoDB
    try:
        await db.command("ping")
        checks["mongodb"] = {"status": "healthy"}
    except Exception as e:
        checks["mongodb"] = {"status": "unhealthy", "error": str(e)}

    # Check Qdrant
    try:
        collections = await qdrant_client.get_collections()
        checks["qdrant"] = {"status": "healthy", "collections": len(collections.collections)}
    except Exception as e:
        checks["qdrant"] = {"status": "unhealthy", "error": str(e)}

    # Check Redis
    try:
        await redis_client.ping()
        checks["redis"] = {"status": "healthy"}
    except Exception as e:
        checks["redis"] = {"status": "unhealthy", "error": str(e)}

    all_healthy = all(c["status"] == "healthy" for c in checks.values())

    return {
        "status": "ready" if all_healthy else "not_ready",
        "checks": checks,
    }
