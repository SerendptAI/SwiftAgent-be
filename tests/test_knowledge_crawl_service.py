from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class AsyncCursor:
    def __init__(self, items):
        self.items = items

    def __aiter__(self):
        self._iterator = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


@pytest.mark.asyncio
async def test_run_company_crawl_embeds_new_page_and_persists_run():
    from app.services import knowledge_crawl_service as service

    db = MagicMock()
    db.companies.find_one = AsyncMock(return_value={"id": "comp", "user_id": "owner"})
    db.knowledge_crawl_configs.find_one = AsyncMock(
        return_value={
            "id": "cfg",
            "company_id": "comp",
            "root_url": "https://example.com",
            "enabled": True,
            "schedule": "0 2 * * *",
            "timezone": "UTC",
            "max_pages": 10,
            "max_depth": 2,
            "include_patterns": [],
            "exclude_patterns": [],
            "respect_robots_txt": False,
            "follow_sitemap": False,
            "remove_deleted_pages": True,
            "missing_threshold": 2,
            "request_delay_ms": 0,
        }
    )
    db.knowledge_crawl_configs.find_one_and_update = AsyncMock(
        return_value=db.knowledge_crawl_configs.find_one.return_value
    )
    db.knowledge_crawl_runs.insert_one = AsyncMock()
    db.knowledge_crawl_runs.update_one = AsyncMock()
    db.knowledge_crawl_configs.update_one = AsyncMock()
    db.knowledge_pages.find_one = AsyncMock(return_value=None)
    db.knowledge_pages.update_one = AsyncMock()
    db.knowledge_pages.find = MagicMock(return_value=AsyncCursor([]))

    page = {
        "url": "https://example.com",
        "title": "Example",
        "content": "Useful billing documentation for customers",
        "links": [],
    }

    with (
        patch.object(service, "db", db),
        patch.object(service, "discover_pages", AsyncMock(return_value=["https://example.com"])),
        patch.object(service, "crawl_page", AsyncMock(return_value=page)),
        patch.object(service, "replace_page_vectors", AsyncMock(return_value=1)) as replace,
    ):
        result = await service.run_company_crawl("comp", trigger="manual")

    assert result["status"] == "completed"
    assert result["pages_new"] == 1
    assert result["chunks_embedded"] == 1
    replace.assert_awaited_once()
    persisted = db.knowledge_pages.update_one.await_args.args[1]["$set"]
    assert persisted["status"] == "active"
    assert persisted["content_hash"]


@pytest.mark.asyncio
async def test_partial_crawl_never_marks_undiscovered_pages_missing():
    from app.services import knowledge_crawl_service as service

    db = MagicMock()
    config = {
        "id": "cfg",
        "company_id": "comp",
        "root_url": "https://example.com",
        "enabled": True,
        "schedule": "0 2 * * *",
        "timezone": "UTC",
        "max_pages": 10,
        "max_depth": 2,
        "include_patterns": [],
        "exclude_patterns": [],
        "respect_robots_txt": False,
        "follow_sitemap": False,
        "remove_deleted_pages": True,
        "missing_threshold": 1,
        "request_delay_ms": 0,
    }
    db.companies.find_one = AsyncMock(return_value={"id": "comp", "user_id": "owner"})
    db.knowledge_crawl_configs.find_one = AsyncMock(return_value=config)
    db.knowledge_crawl_configs.find_one_and_update = AsyncMock(return_value=config)
    db.knowledge_crawl_runs.insert_one = AsyncMock()
    db.knowledge_crawl_runs.update_one = AsyncMock()
    db.knowledge_crawl_configs.update_one = AsyncMock()
    db.knowledge_pages.find_one = AsyncMock(return_value=None)
    db.knowledge_pages.update_one = AsyncMock()
    db.knowledge_pages.update_many = AsyncMock()
    db.knowledge_pages.find = MagicMock(return_value=AsyncCursor([]))

    with (
        patch.object(service, "db", db),
        patch.object(
            service, "discover_pages", AsyncMock(return_value=["https://example.com/broken"])
        ),
        patch.object(service, "crawl_page", AsyncMock(return_value={"error": "timeout"})),
        patch.object(service, "delete_page_vectors", AsyncMock()) as delete,
    ):
        result = await service.run_company_crawl("comp")

    assert result["status"] == "partially_completed"
    assert result["pages_failed"] == 1
    delete.assert_not_awaited()
    db.knowledge_pages.update_many.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_crawl_deletes_page_after_missing_threshold():
    from app.services import knowledge_crawl_service as service

    missing_page = {
        "id": "old-page",
        "company_id": "comp",
        "url": "https://example.com/removed",
        "status": "active",
        "missing_count": 1,
    }
    db = MagicMock()
    db.companies.find_one = AsyncMock(return_value={"id": "comp", "user_id": "owner"})
    db.knowledge_crawl_configs.find_one = AsyncMock(
        return_value={
            "id": "cfg",
            "company_id": "comp",
            "root_url": "https://example.com",
            "enabled": True,
            "schedule": "0 2 * * *",
            "timezone": "UTC",
            "max_pages": 10,
            "max_depth": 2,
            "include_patterns": [],
            "exclude_patterns": [],
            "respect_robots_txt": False,
            "follow_sitemap": False,
            "remove_deleted_pages": True,
            "missing_threshold": 2,
            "request_delay_ms": 0,
        }
    )
    db.knowledge_crawl_configs.find_one_and_update = AsyncMock(
        return_value=db.knowledge_crawl_configs.find_one.return_value
    )
    db.knowledge_crawl_runs.insert_one = AsyncMock()
    db.knowledge_crawl_runs.update_one = AsyncMock()
    db.knowledge_crawl_configs.update_one = AsyncMock()
    current_content = "Current documentation"
    db.knowledge_pages.find_one = AsyncMock(
        return_value={
            "id": "current-page",
            "company_id": "comp",
            "canonical_url": "https://example.com",
            "content_hash": service.calculate_content_hash(current_content),
        }
    )
    db.knowledge_pages.update_one = AsyncMock()
    db.knowledge_pages.find = MagicMock(return_value=AsyncCursor([missing_page]))

    with (
        patch.object(service, "db", db),
        patch.object(service, "discover_pages", AsyncMock(return_value=["https://example.com"])),
        patch.object(
            service,
            "crawl_page",
            AsyncMock(
                return_value={
                    "url": "https://example.com",
                    "title": "Example",
                    "content": current_content,
                    "links": [],
                }
            ),
        ),
        patch.object(service, "delete_page_vectors", AsyncMock()) as delete,
    ):
        result = await service.run_company_crawl("comp")

    assert result["status"] == "completed"
    assert result["pages_deleted"] == 1
    delete.assert_awaited_once_with("comp", "old-page")


@pytest.mark.asyncio
async def test_empty_discovery_fails_without_deleting_existing_pages():
    from app.services import knowledge_crawl_service as service

    db = MagicMock()
    db.companies.find_one = AsyncMock(return_value={"id": "comp", "user_id": "owner"})
    db.knowledge_crawl_configs.find_one = AsyncMock(
        return_value={"company_id": "comp", "root_url": "https://example.com"}
    )
    db.knowledge_crawl_configs.find_one_and_update = AsyncMock(
        return_value=db.knowledge_crawl_configs.find_one.return_value
    )
    db.knowledge_crawl_runs.insert_one = AsyncMock()
    db.knowledge_crawl_runs.update_one = AsyncMock()
    db.knowledge_crawl_configs.update_one = AsyncMock()
    db.knowledge_pages.find = MagicMock(return_value=AsyncCursor([]))

    with (
        patch.object(service, "db", db),
        patch.object(service, "discover_pages", AsyncMock(return_value=[])),
        patch.object(service, "delete_page_vectors", AsyncMock()) as delete,
    ):
        with pytest.raises(RuntimeError, match="no crawlable pages"):
            await service.run_company_crawl("comp")

    delete.assert_not_awaited()
