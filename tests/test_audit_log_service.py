from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import audit_service


def _fake_db():
    database = MagicMock()
    database.audit_logs = MagicMock()
    database.audit_logs.insert_one = AsyncMock()
    database.audit_logs.find_one = AsyncMock()
    database.audit_logs.count_documents = AsyncMock(return_value=1)
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[{"id": "e1", "company_id": "comp"}])
    database.audit_logs.find = MagicMock(return_value=cursor)
    database.__getitem__.return_value = database.audit_logs
    return database


@pytest.mark.asyncio
async def test_record_event_appends_entry_with_expiry():
    db = _fake_db()
    with patch_db(audit_service, db):
        entry = await audit_service.record_event(
            company_id="comp",
            actor_id="user-1",
            action="company.update",
            resource_type="company",
            resource_id="comp",
            changes={"name": {"before": "Old", "after": "New"}},
        )

    assert entry["outcome"] == "success"
    assert entry["expires_at"] > entry["created_at"]
    db.audit_logs.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_event_redacts_secret_looking_fields():
    db = _fake_db()
    with patch_db(audit_service, db):
        entry = await audit_service.record_event(
            company_id="comp",
            actor_id="user-1",
            action="company.api_key_created",
            resource_type="api_key",
            changes={"api_key": "super-secret", "label": "CI key"},
        )

    assert entry["changes"]["api_key"] == "[REDACTED]"
    assert entry["changes"]["label"] == "CI key"


@pytest.mark.asyncio
async def test_list_events_filters_by_company_and_action():
    db = _fake_db()
    with patch_db(audit_service, db):
        result = await audit_service.list_events(
            "comp", action="crawl.triggered", limit=10
        )

    query = db.audit_logs.find.call_args.args[0]
    assert query["company_id"] == "comp"
    assert query["action"] == "crawl.triggered"
    assert result["total"] == 1
    assert result["items"][0]["id"] == "e1"


def test_export_events_jsonl_produces_parseable_lines():
    entries = [
        {"id": "e1", "action": "company.update", "created_at": "2026-01-01"},
        {"id": "e2", "action": "knowledge.ingested", "_id": "ignored"},
    ]
    lines = audit_service.export_events_jsonl(entries).splitlines()

    assert len(lines) == 2
    assert '"_id"' not in lines[1]


def patch_db(module, fake_db):
    from unittest.mock import patch

    return patch.object(module, "db", fake_db)
