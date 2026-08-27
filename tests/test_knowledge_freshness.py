from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_freshness_reports_never_crawled_without_successful_run():
    from app.services import knowledge_crawl_service as service

    db = MagicMock()
    db.knowledge_crawl_configs.find_one = AsyncMock(
        return_value={"company_id": "comp", "schedule": "0 2 * * *"}
    )
    db.knowledge_crawl_runs.find_one = AsyncMock(return_value=None)
    db.knowledge_pages.count_documents = AsyncMock(return_value=0)
    db.knowledge_gaps.count_documents = AsyncMock(return_value=3)

    with patch.object(service, "db", db):
        result = await service.get_freshness_summary("comp")

    assert result["status"] == "never_crawled"
    assert result["knowledge_gap_count"] == 3


@pytest.mark.asyncio
async def test_freshness_reports_degraded_when_last_run_has_failures():
    from app.services import knowledge_crawl_service as service

    now = datetime.now(UTC)
    db = MagicMock()
    db.knowledge_crawl_configs.find_one = AsyncMock(
        return_value={
            "company_id": "comp",
            "last_completed_at": now,
            "next_run_at": now + timedelta(days=1),
        }
    )
    db.knowledge_crawl_runs.find_one = AsyncMock(
        return_value={
            "status": "partially_completed",
            "completed_at": now,
            "pages_failed": 2,
            "pages_new": 1,
            "pages_changed": 4,
        }
    )
    db.knowledge_pages.count_documents = AsyncMock(side_effect=[20, 1, 2])
    db.knowledge_gaps.count_documents = AsyncMock(return_value=5)

    with patch.object(service, "db", db):
        result = await service.get_freshness_summary("comp")

    assert result["status"] == "degraded"
    assert result["indexed_pages"] == 20
    assert result["failed_pages"] == 2
    assert result["knowledge_gap_count"] == 5
