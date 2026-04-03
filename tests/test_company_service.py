"""Tests for the company service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestCompanyService:
    @pytest.mark.asyncio
    async def test_create_company(self, mock_db, sample_user):
        from app.services import company_service
        from app.core import cache as cache_module

        mock_db.companies.insert_one = AsyncMock(return_value=None)
        mock_cache = AsyncMock()
        mock_cache.set = AsyncMock()

        company_data = {
            "name": "Test Co",
            "contact_email": "test@test.com",
            "website": "https://test.com",
        }

        with patch.object(company_service, "db", mock_db):
            with patch.object(company_service, "company_cache", mock_cache):
                result = await company_service.create_company(sample_user["user_id"], company_data)

        assert result["name"] == "Test Co"
        assert result["user_id"] == "user_456"
        assert result["onboarding_step"] == 1
        assert result["setup_complete"] is False
        assert "id" in result
        mock_cache.set.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_company_returns_cached(self, mock_db, sample_company):
        from app.services import company_service
        from app.core.cache import TTLCache

        cache = TTLCache(default_ttl=300)
        await cache.set(f"company:{sample_company['id']}:user_456", sample_company)

        with patch.object(company_service, "db", mock_db):
            with patch.object(company_service, "company_cache", cache):
                result = await company_service.get_company(sample_company["id"], "user_456")

        assert result == sample_company
        mock_db.companies.find_one.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_company_falls_back_to_db(self, mock_db, sample_company):
        from app.services import company_service
        from app.core.cache import TTLCache

        cache = TTLCache(default_ttl=300)
        mock_db.companies.find_one = AsyncMock(return_value=sample_company)

        with patch.object(company_service, "db", mock_db):
            with patch.object(company_service, "company_cache", cache):
                result = await company_service.get_company(sample_company["id"], "user_456")

        assert result == sample_company
        mock_db.companies.find_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_company_public(self, mock_db, sample_company):
        from app.services import company_service
        from app.core.cache import TTLCache

        cache = TTLCache(default_ttl=300)
        mock_db.companies.find_one = AsyncMock(return_value=sample_company)

        with patch.object(company_service, "db", mock_db):
            with patch.object(company_service, "company_cache", cache):
                result = await company_service.get_company(sample_company["id"])

        assert result == sample_company
        mock_db.companies.find_one.assert_called_once_with({"id": "comp_123"})

    @pytest.mark.asyncio
    async def test_update_and_return_single_query(self, mock_db, sample_company):
        from app.services import company_service
        from app.core.cache import TTLCache

        cache = TTLCache(default_ttl=300)
        updated_company = {**sample_company, "name": "Updated Co"}
        mock_db.companies.find_one_and_update = AsyncMock(return_value=updated_company)

        with patch.object(company_service, "db", mock_db):
            with patch.object(company_service, "company_cache", cache):
                result = await company_service.update_identity(
                    "comp_123", "user_456", {"name": "Updated Co"}
                )

        assert result["name"] == "Updated Co"
        mock_db.companies.find_one_and_update.assert_called_once()
