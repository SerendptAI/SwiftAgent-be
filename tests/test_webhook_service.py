import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import webhook_service


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


def _make_collection():
    collection = MagicMock()
    collection.insert_one = AsyncMock()
    collection.find_one = AsyncMock()
    collection.find_one_and_update = AsyncMock()
    collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
    collection.count_documents = AsyncMock(return_value=0)
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.skip.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=[])
    collection.find = MagicMock(return_value=cursor)
    return collection


@pytest.fixture
def fake_db():
    return _FakeDb()


def patch_db(fake_db):
    from unittest.mock import patch

    return patch.object(webhook_service, "db", fake_db)


def test_generate_secret_is_random_and_long():
    first = webhook_service.generate_secret()
    second = webhook_service.generate_secret()

    assert first != second
    assert len(first) >= 64


def test_sign_payload_matches_hmac_sha256():
    secret = "test-secret"
    body = b'{"event": "ticket.created"}'

    signature = webhook_service.sign_payload(secret, body)

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert signature == expected
    assert signature.startswith("sha256=")


@pytest.mark.asyncio
async def test_create_endpoint_generates_secret_and_persists(fake_db):
    with patch_db(fake_db):
        endpoint = await webhook_service.create_endpoint(
            "comp", "https://hooks.example.com/x", ["ticket.created"]
        )

    assert endpoint["company_id"] == "comp"
    assert endpoint["enabled"] is True
    assert len(endpoint["secret"]) >= 64
    assert endpoint["events"] == ["ticket.created"]
    assert "_id" not in endpoint


@pytest.mark.asyncio
async def test_emit_event_fans_out_to_matching_endpoints_only(fake_db):
    matching = {
        "id": "ep-1",
        "company_id": "comp",
        "url": "https://hooks.example.com/a",
        "events": ["crawl.completed"],
        "secret": "s1",
        "enabled": True,
    }
    fake_db.webhook_endpoints.find.return_value.to_list = AsyncMock(return_value=[matching])
    deliveries = []

    async def fake_deliver(endpoint, event_type, payload):
        deliveries.append((endpoint["id"], event_type))

    import unittest.mock

    with (
        patch_db(fake_db),
        unittest.mock.patch.object(webhook_service, "_deliver_with_retries", fake_deliver),
    ):
        count = await webhook_service.emit_event("comp", "crawl.completed", {"run_id": "r-1"})
        await _drain_tasks()

    assert count == 1
    assert deliveries == [("ep-1", "crawl.completed")]


@pytest.mark.asyncio
async def test_emit_event_never_raises_on_db_failure():
    broken = MagicMock()
    broken.webhook_endpoints.find.side_effect = RuntimeError("mongo down")

    from unittest.mock import patch

    with patch.object(webhook_service, "db", broken):
        count = await webhook_service.emit_event("comp", "crawl.completed", {})

    assert count == 0


@pytest.mark.asyncio
async def test_delivery_records_success_and_signing(monkeypatch, fake_db):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = "ok"

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def fake_post(url, *, content, headers):
        captured["url"] = url
        captured["body"] = content
        captured["headers"] = headers
        return FakeResponse()

    client.post = fake_post


    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    endpoint = {
        "id": "ep-1",
        "company_id": "comp",
        "url": "https://hooks.example.com/a",
        "events": [],
        "secret": "shhh",
        "enabled": True,
    }

    with patch_db(fake_db):
        await webhook_service._deliver_with_retries(endpoint, "gap.detected", {"topic": "billing"})

    recorded = fake_db.webhook_deliveries.insert_one.await_args.args[0]
    assert recorded["status"] == "delivered"
    assert recorded["status_code"] == 200
    assert recorded["endpoint_id"] == "ep-1"

    signature = captured["headers"]["X-SwiftAgent-Signature"]
    expected_sig = "sha256=" + hmac.new(b"shhh", captured["body"], hashlib.sha256).hexdigest()
    assert signature == expected_sig
    assert captured["headers"]["X-SwiftAgent-Event"] == "gap.detected"


@pytest.mark.asyncio
async def test_delivery_retries_then_records_failure(monkeypatch, fake_db):
    attempts = {"count": 0}

    class FakeResponse:
        status_code = 500
        text = "boom"

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def fake_post(url, *, content, headers):
        attempts["count"] += 1
        return FakeResponse()

    client.post = fake_post

    import httpx

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    monkeypatch.setattr(webhook_service.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(webhook_service, "MAX_ATTEMPTS", 3)

    endpoint = {
        "id": "ep-9",
        "company_id": "comp",
        "url": "https://hooks.example.com/fail",
        "events": [],
        "secret": "s",
        "enabled": True,
    }

    with patch_db(fake_db):
        await webhook_service._deliver_with_retries(endpoint, "handoff.initiated", {})

    assert attempts["count"] == 3
    recorded = fake_db.webhook_deliveries.insert_one.await_args.args[0]
    assert recorded["status"] == "failed"
    assert recorded["error"] == "HTTP 500"
    # exponential backoff: base**(attempt-1) for each pre-final attempt
    assert sleeps == [1, 2]


async def _drain_tasks():
    import asyncio

    current = asyncio.current_task()
    pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
