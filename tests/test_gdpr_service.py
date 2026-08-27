import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import gdpr_service


class _FakeDb:
    """Duck-typed Motor database returning per-name mock collections."""

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


def _fake_db():
    return _FakeDb()


def _make_collection():
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    collection.find_one = AsyncMock()
    collection.delete_many = AsyncMock(return_value=MagicMock(deleted_count=2))
    collection.delete_one = AsyncMock()
    collection.update_one = AsyncMock()
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[])
    collection.find = MagicMock(return_value=cursor)
    return collection


@pytest.mark.asyncio
async def test_create_export_job_persists_pending_record():
    db = _fake_db()
    with patch_db(db):
        job = await gdpr_service.create_export_job("comp", "user-1")

    assert job["status"] == "pending"
    assert job["company_id"] == "comp"
    assert job["requested_by"] == "user-1"
    db.gdpr_export_jobs.insert_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_export_company_data_collects_company_scoped_collections():
    db = _fake_db()

    def _cursor(items):
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=items)
        return cursor

    db.companies.find.return_value = _cursor([{"id": "comp", "name": "Acme"}])
    db.widget_conversations.find.return_value = _cursor([])
    for name in gdpr_service.EXPORT_COLLECTIONS:
        if name != "companies":
            getattr(db, name).find.return_value = _cursor([])

    with patch_db(db), patch_qdrant_scroll([]):
        bundle = await gdpr_service.export_company_data("comp")

    assert bundle["company_id"] == "comp"
    assert bundle["collections"]["companies"] == [{"id": "comp", "name": "Acme"}]
    assert bundle["qdrant_vectors"] == []


@pytest.mark.asyncio
async def test_run_deletion_completes_and_counts_documents():
    db = _fake_db()
    request_id = "delete_1"
    db.gdpr_deletion_requests.find_one = AsyncMock(
        side_effect=[
            {"id": request_id, "company_id": "comp", "status": "pending"},
            None,
        ]
    )
    db.companies.find_one = AsyncMock(return_value={"id": "comp", "user_id": "owner"})

    with patch_db(db), patch_vector_delete(True):
        result = await gdpr_service.run_deletion(request_id, "comp")

    assert result["status"] == "completed"
    assert result["deleted_counts"]["companies"] == 1
    assert result["deleted_counts"]["widget_conversations"] == 2
    assert result["vectors_deleted"] is True


@pytest.mark.asyncio
async def test_run_deletion_marks_failed_when_vector_delete_errors(monkeypatch):
    db = _fake_db()
    request_id = "delete_2"
    db.gdpr_deletion_requests.find_one = AsyncMock(
        return_value={"id": request_id, "company_id": "comp", "status": "pending"}
    )
    db.companies.find_one = AsyncMock(return_value=None)

    async def boom(*args, **kwargs):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(gdpr_service, "delete_company_vectors", boom)

    with patch_db(db):
        result = await gdpr_service.run_deletion(request_id, "comp")

    assert result["status"] == "failed"
    assert "qdrant down" in (result["error"] or "")


def test_bundle_json_serializes_datetimes():
    payload = {
        "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "items": [{"created_at": datetime(2026, 1, 2, tzinfo=UTC)}],
    }
    text = json.dumps(payload, default=lambda x: x.isoformat() if isinstance(x, datetime) else None)
    assert "2026-01-01" in text


def patch_db(fake_db):
    from unittest.mock import patch

    return patch.object(gdpr_service, "db", fake_db)


def patch_qdrant_scroll(points):
    from unittest.mock import patch

    client = MagicMock()
    client.collection_exists = AsyncMock(return_value=True)
    client.scroll = AsyncMock(return_value=(points, None))
    return patch.object(gdpr_service, "qdrant_client", client)


def patch_vector_delete(success: bool):
    from unittest.mock import patch

    client = MagicMock()
    client.collection_exists = AsyncMock(return_value=True)
    client.delete = AsyncMock(return_value=success)
    return patch.object(gdpr_service, "qdrant_client", client)
