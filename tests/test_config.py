"""Tests for the config module."""

import pytest
import os
from pydantic import ValidationError


class TestConfig:
    def test_required_fields_validation(self):
        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                MONGO_URI="",
                QDRANT_URL="http://localhost:6333",
                QDRANT_API_KEY="test",
                GOOGLE_CLIENT_ID="test",
                GOOGLE_CLIENT_SECRET="test",
                SECRET_KEY="test",
                API_BASE_URL="http://localhost:8000",
            )

    def test_is_development_property(self):
        from app.core.config import Settings

        settings = Settings(
            MONGO_URI="mongodb://localhost:27017/test",
            QDRANT_URL="http://localhost:6333",
            QDRANT_API_KEY="test",
            GOOGLE_CLIENT_ID="test",
            GOOGLE_CLIENT_SECRET="test",
            SECRET_KEY="test",
            API_BASE_URL="http://localhost:8000",
            ENVIRONMENT="development",
        )
        assert settings.is_development is True

    def test_is_production_property(self):
        from app.core.config import Settings

        settings = Settings(
            MONGO_URI="mongodb://localhost:27017/test",
            QDRANT_URL="http://localhost:6333",
            QDRANT_API_KEY="test",
            GOOGLE_CLIENT_ID="test",
            GOOGLE_CLIENT_SECRET="test",
            SECRET_KEY="test",
            API_BASE_URL="http://localhost:8000",
            ENVIRONMENT="production",
        )
        assert settings.is_development is False

    def test_allowed_hosts_list(self):
        from app.core.config import Settings

        settings = Settings(
            MONGO_URI="mongodb://localhost:27017/test",
            QDRANT_URL="http://localhost:6333",
            QDRANT_API_KEY="test",
            GOOGLE_CLIENT_ID="test",
            GOOGLE_CLIENT_SECRET="test",
            SECRET_KEY="test",
            API_BASE_URL="http://localhost:8000",
            ALLOWED_HOSTS="http://localhost:3000, https://example.com",
        )
        assert settings.allowed_hosts_list == ["http://localhost:3000", "https://example.com"]

    def test_log_level_validation(self):
        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                MONGO_URI="mongodb://localhost:27017/test",
                QDRANT_URL="http://localhost:6333",
                QDRANT_API_KEY="test",
                GOOGLE_CLIENT_ID="test",
                GOOGLE_CLIENT_SECRET="test",
                SECRET_KEY="test",
                API_BASE_URL="http://localhost:8000",
                LOG_LEVEL="INVALID",
            )
