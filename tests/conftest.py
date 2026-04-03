import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Set required env vars BEFORE any app imports
REQUIRED_ENV = {
    "MONGO_URI": "mongodb://localhost:27017/test_db",
    "QDRANT_URL": "http://localhost:6333",
    "QDRANT_API_KEY": "test-qdrant-key",
    "GOOGLE_CLIENT_ID": "test-google-client-id",
    "GOOGLE_CLIENT_SECRET": "test-google-client-secret",
    "SECRET_KEY": "test-secret-key-for-testing-only",
    "ALGORITHM": "HS256",
    "ACCESS_TOKEN_EXPIRE_MINUTES": "30",
    "API_BASE_URL": "http://localhost:8000",
    "ENVIRONMENT": "development",
    "LOG_LEVEL": "DEBUG",
    "REFRESH_TOKEN_EXPIRE_DAYS": "7",
    "REFERRAL_CODE": "",
    "GEMINI_API_KEY": "test-gemini-key",
    "ANTHROPIC_API_KEY": "test-anthropic-key",
    "CLOUDINARY_CLOUD_NAME": "test-cloud",
    "CLOUDINARY_API_KEY": "test-key",
    "CLOUDINARY_API_SECRET": "test-secret",
    "FISH_AUDIO_API_KEY": "test-fish-key",
    "FISH_AUDIO_VOICE_ID": "test-voice",
    "ETHERSCAN_API_KEY": "test-etherscan-key",
}

for key, value in REQUIRED_ENV.items():
    if key not in os.environ:
        os.environ[key] = value


@pytest.fixture
def mock_db():
    """Create a mock database with async methods."""
    db = MagicMock()
    db.companies = AsyncMock()
    db.users = AsyncMock()
    db.conversations = AsyncMock()
    db.widget_conversations = AsyncMock()
    db.visitors = AsyncMock()
    db.calls = AsyncMock()
    db.knowledge_sources = AsyncMock()
    db.documents = AsyncMock()
    db.stroll_versions = AsyncMock()
    db.stroll_configs = AsyncMock()
    db.scrapes = AsyncMock()
    return db


@pytest.fixture
def sample_company():
    return {
        "_id": "507f1f77bcf86cd799439011",
        "id": "comp_123",
        "user_id": "user_456",
        "name": "Test Company",
        "website": "https://test.com",
        "industry": "crypto",
        "company_type": "crypto",
        "contact_email": "test@test.com",
        "logo_url": None,
        "brand_tone": "professional",
        "description": "A test company",
        "onboarding_step": 5,
        "setup_complete": True,
        "primary_language": "English",
        "voice_style": "professional",
        "enabled_sources": [],
        "custom_info": [],
    }


@pytest.fixture
def sample_user():
    return {
        "_id": "507f1f77bcf86cd799439012",
        "user_id": "user_456",
        "email": "test@test.com",
        "name": "Test User",
    }
