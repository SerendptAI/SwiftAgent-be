from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_schedule_crawl_job_registers_company_cron():
    from app.services import knowledge_crawl_scheduler as scheduler

    fake = MagicMock()
    fake.running = True
    fake.get_job.return_value = None

    with patch.object(scheduler, "_scheduler", fake):
        scheduler.schedule_crawl_job("comp", "15 3 * * *", "Africa/Lagos")

    fake.add_job.assert_called_once()
    kwargs = fake.add_job.call_args.kwargs
    assert kwargs["id"] == "knowledge_crawl_comp"
    assert kwargs["args"] == ["comp"]
    assert kwargs["replace_existing"] is True


@pytest.mark.asyncio
async def test_reconcile_orphaned_runs_marks_queued_and_stale_runs_failed():
    from app.services import knowledge_crawl_scheduler as scheduler

    fake_db = MagicMock()
    fake_db.knowledge_crawl_runs.update_many = AsyncMock()

    with patch.object(scheduler, "db", fake_db):
        await scheduler.reconcile_orphaned_runs()

    assert fake_db.knowledge_crawl_runs.update_many.await_count == 2
    queued_filter = fake_db.knowledge_crawl_runs.update_many.await_args_list[0].args[0]
    active_filter = fake_db.knowledge_crawl_runs.update_many.await_args_list[1].args[0]
    assert queued_filter["status"] == "queued"
    assert set(active_filter["status"]["$in"]) == {
        "discovering",
        "crawling",
        "embedding",
    }
