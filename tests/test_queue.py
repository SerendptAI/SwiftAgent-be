"""Tests for the ARQ queue wrapper: graceful degradation, idempotency,
worker registration, and pool lifecycle."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import queue
from app.core.queue import JOB_NAMES, EnqueueResult


@pytest.fixture(autouse=True)
def reset_pool():
    queue._arq_pool = None
    yield
    queue._arq_pool = None


def test_job_names_registry_is_complete():
    assert JOB_NAMES.KNOWLEDGE_CRAWL == "knowledge_crawl"
    assert JOB_NAMES.GDPR_EXPORT == "gdpr_export"
    assert JOB_NAMES.GDPR_DELETE == "gdpr_delete"
    assert JOB_NAMES.RETENTION_SWEEP == "retention_sweep"


@pytest.mark.asyncio
async def test_get_pool_returns_none_without_redis_url(monkeypatch):
    monkeypatch.setattr(queue.settings, "REDIS_URL", None)
    assert await queue.get_pool() is None


@pytest.mark.asyncio
async def test_get_pool_returns_none_when_connection_fails(monkeypatch):
    monkeypatch.setattr(queue.settings, "REDIS_URL", "redis://localhost:6379/0")

    async def failing_create_pool(_settings):
        raise ConnectionError("redis down")

    import arq

    monkeypatch.setattr(arq, "create_pool", failing_create_pool)

    assert await queue.get_pool() is None


@pytest.mark.asyncio
async def test_get_pool_creates_and_reuses_singleton(monkeypatch):
    monkeypatch.setattr(queue.settings, "REDIS_URL", "redis://localhost:6379/0")
    fake_pool = MagicMock()

    calls = {"count": 0}

    async def fake_create_pool(_settings):
        calls["count"] += 1
        return fake_pool

    import arq

    monkeypatch.setattr(arq, "create_pool", fake_create_pool)

    first = await queue.get_pool()
    second = await queue.get_pool()

    assert first is fake_pool
    assert second is fake_pool
    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_close_pool_closes_and_resets():
    fake_pool = MagicMock()
    fake_pool.aclose = AsyncMock()
    queue._arq_pool = fake_pool

    await queue.close_pool()

    fake_pool.aclose.assert_awaited_once()
    assert queue._arq_pool is None


@pytest.mark.asyncio
async def test_enqueue_job_reports_unavailable_without_redis(monkeypatch):
    monkeypatch.setattr(queue.settings, "REDIS_URL", None)

    result = await queue.enqueue_job(JOB_NAMES.KNOWLEDGE_CRAWL, "comp-1")

    assert result.queued is False
    assert result.reason == "queue_unavailable"


@pytest.mark.asyncio
async def test_enqueue_job_queues_through_pool(monkeypatch):
    job = MagicMock()
    job.job_id = "job-123"
    fake_pool = MagicMock()
    fake_pool.enqueue_job = AsyncMock(return_value=job)

    monkeypatch.setattr(queue, "get_pool", AsyncMock(return_value=fake_pool))

    result = await queue.enqueue_job(JOB_NAMES.KNOWLEDGE_CRAWL, "comp-1", job_id="job-123")

    assert result.queued is True
    assert result.job_id == "job-123"
    fake_pool.enqueue_job.assert_awaited_once_with(
        JOB_NAMES.KNOWLEDGE_CRAWL, "comp-1", _job_id="job-123"
    )


@pytest.mark.asyncio
async def test_enqueue_job_detects_duplicate_ids(monkeypatch):
    fake_pool = MagicMock()
    # ARQ returns None when a job with the same id already exists.
    fake_pool.enqueue_job = AsyncMock(return_value=None)

    monkeypatch.setattr(queue, "get_pool", AsyncMock(return_value=fake_pool))

    result = await queue.enqueue_job(JOB_NAMES.KNOWLEDGE_CRAWL, "comp-1", job_id="dup-1")

    assert result.queued is False
    assert result.reason == "duplicate_job_id"


@pytest.mark.asyncio
async def test_enqueue_or_run_falls_back_to_inline_execution(monkeypatch):
    executed = []

    async def crawl(company_id):
        executed.append(company_id)
        return {"status": "completed"}

    monkeypatch.setattr(queue.settings, "REDIS_URL", None)

    result = await queue.enqueue_or_run(JOB_NAMES.KNOWLEDGE_CRAWL, crawl, "comp-1")

    assert result.queued is False
    assert result.reason == "ran_inline"

    import asyncio

    current = asyncio.current_task()
    pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    if pending:
        await asyncio.gather(*pending)
    assert executed == ["comp-1"]


@pytest.mark.asyncio
async def test_enqueue_or_run_does_not_double_run_duplicates(monkeypatch):
    executed = []

    async def crawl(company_id):
        executed.append(company_id)

    fake_result = EnqueueResult(queued=False, job_id="dup", reason="duplicate_job_id")
    monkeypatch.setattr(queue, "enqueue_job", AsyncMock(return_value=fake_result))

    await queue.enqueue_or_run(JOB_NAMES.KNOWLEDGE_CRAWL, crawl, "comp-1")

    assert executed == []


def test_worker_functions_registers_all_tasks():
    functions = queue.worker_functions()
    names = {fn.__name__ for fn in functions}

    assert names == {
        "knowledge_crawl",
        "gdpr_export",
        "gdpr_delete",
        "retention_sweep",
    }


@pytest.mark.asyncio
async def test_worker_settings_startup_handles_index_failure():
    settings_cls = queue.WorkerSettings

    async def failing_indexes():
        raise RuntimeError("mongo down")

    from unittest.mock import patch

    with patch("app.core.database.create_indexes", failing_indexes):
        await settings_cls.startup({})
