from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import conversation_privacy_service as privacy


class _FakeDb:
    def __init__(self):
        object.__setattr__(self, "_collections", {})

    def __getitem__(self, name):
        return self._get(name)

    def __getattr__(self, name):
        return self._get(name)

    def _get(self, name):
        collections = object.__getattribute__(self, "_collections")
        if name not in collections:
            collections[name] = _make_collection()
        return collections[name]


def _make_collection():
    collection = MagicMock()
    collection.find_one = AsyncMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[])
    collection.find = MagicMock(return_value=cursor)
    collection.delete_many = AsyncMock(return_value=MagicMock(deleted_count=3))
    collection.update_many = AsyncMock(return_value=MagicMock(modified_count=5))
    collection.create_index = AsyncMock()
    return collection


@pytest.fixture
def fake_db():
    return _FakeDb()


@pytest.mark.asyncio
async def test_default_policy_retains_when_no_company_config(fake_db):
    fake_db.companies.find_one = AsyncMock(return_value=None)

    with patch_db(fake_db):
        config = await privacy.get_retention_config("comp")

    assert config == {
        "retention_days": 365,
        "expired_action": "retain",
        "redact_pii": True,
    }


@pytest.mark.asyncio
async def test_company_policy_is_honored(fake_db):
    fake_db.companies.find_one = AsyncMock(
        return_value={
            "retention_policy": {
                "retention_days": 30,
                "expired_action": "delete",
                "redact_pii": False,
            }
        }
    )

    with patch_db(fake_db):
        config = await privacy.get_retention_config("comp")

    assert config["retention_days"] == 30
    assert config["expired_action"] == "delete"
    assert config["redact_pii"] is False


@pytest.mark.asyncio
async def test_invalid_policy_values_fall_back_to_safe_defaults(fake_db):
    fake_db.companies.find_one = AsyncMock(
        return_value={"retention_policy": {"retention_days": -5}}
    )

    with patch_db(fake_db):
        config = await privacy.get_retention_config("comp")

    # invalid days -> default; policy present but no valid action -> delete
    assert config["retention_days"] == 365


@pytest.mark.asyncio
async def test_redaction_disabled_returns_original_text(fake_db):
    fake_db.companies.find_one = AsyncMock(return_value={"retention_policy": {"redact_pii": False}})

    with patch_db(fake_db):
        result = await privacy.redact_message_on_ingest("comp", "email me at a@b.com")

    assert result == "email me at a@b.com"


@pytest.mark.asyncio
async def test_redaction_enabled_scrubs_text(fake_db):
    fake_db.companies.find_one = AsyncMock(return_value=None)

    with patch_db(fake_db):
        result = await privacy.redact_message_on_ingest(
            "comp", "email me at a@b.com or call 555-123-4567"
        )

    assert "[REDACTED]" in result
    assert "@" not in result


@pytest.mark.asyncio
async def test_sweep_deletes_expired_for_delete_mode(fake_db):
    company = {
        "id": "comp",
        "retention_policy": {"retention_days": 30, "expired_action": "delete"},
    }
    fake_db.companies.find.return_value.to_list = AsyncMock(return_value=[company])
    fake_db.companies.find_one = AsyncMock(return_value=dict(company))

    with patch_db(fake_db):
        totals = await privacy.apply_retention_sweep()

    assert totals["conversations_deleted"] == 3
    assert totals["companies"] == 1
    query = fake_db.widget_conversations.delete_many.await_args.args[0]
    assert query["company_id"] == "comp"
    assert "$lt" in query["updated_at"]


@pytest.mark.asyncio
async def test_sweep_anonymizes_for_anonymize_mode(fake_db):
    company = {
        "id": "comp",
        "retention_policy": {"retention_days": 90, "expired_action": "anonymize"},
    }
    fake_db.companies.find.return_value.to_list = AsyncMock(return_value=[company])
    fake_db.companies.find_one = AsyncMock(return_value=dict(company))

    with patch_db(fake_db):
        totals = await privacy.apply_retention_sweep("comp")

    assert totals["conversations_anonymized"] == 5
    update = fake_db.widget_conversations.update_many.await_args.args[1]
    assert update["$set"]["messages"] == []
    assert update["$set"]["sdk_user_email"] is None


@pytest.mark.asyncio
async def test_sweep_skips_retain_mode_companies(fake_db):
    company = {"id": "comp", "retention_policy": {"expired_action": "retain"}}
    fake_db.companies.find.return_value.to_list = AsyncMock(return_value=[company])
    fake_db.companies.find_one = AsyncMock(return_value=dict(company))

    with patch_db(fake_db):
        totals = await privacy.apply_retention_sweep()

    assert totals["companies"] == 0
    fake_db.widget_conversations.delete_many.assert_not_awaited()
    fake_db.widget_conversations.update_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_without_target_only_touches_companies_with_policy(fake_db):
    captured: dict = {}

    def capture_query(query):
        captured.update(query)
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        return cursor

    fake_db.companies.find.side_effect = capture_query

    with patch_db(fake_db):
        await privacy.apply_retention_sweep()

    assert "retention_policy" in captured


def patch_db(fake_db):
    from unittest.mock import patch

    return patch.object(privacy, "db", fake_db)
