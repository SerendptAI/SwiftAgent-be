import pytest
from pydantic import ValidationError


def test_crawl_config_rejects_non_http_root_url():
    from app.models.knowledge_crawl_models import CrawlConfigCreate

    with pytest.raises(ValidationError):
        CrawlConfigCreate(company_id="comp_1", root_url="file:///etc/passwd")


def test_crawl_config_applies_safe_defaults():
    from app.models.knowledge_crawl_models import CrawlConfigCreate

    config = CrawlConfigCreate(company_id="comp_1", root_url="https://example.com/docs")

    assert config.enabled is True
    assert config.schedule == "0 2 * * *"
    assert config.max_pages == 100
    assert config.max_depth == 3
    assert config.respect_robots_txt is True
    assert config.follow_sitemap is True
    assert config.remove_deleted_pages is True


def test_crawl_config_rejects_invalid_cron_and_timezone():
    from app.models.knowledge_crawl_models import CrawlConfigCreate

    with pytest.raises(ValidationError):
        CrawlConfigCreate(
            company_id="comp_1",
            root_url="https://example.com",
            schedule="99 * * * *",
        )

    with pytest.raises(ValidationError):
        CrawlConfigCreate(
            company_id="comp_1",
            root_url="https://example.com",
            timezone="Mars/Olympus_Mons",
        )


def test_normalize_url_removes_fragment_tracking_and_trailing_slash():
    from app.services.knowledge_crawl_service import normalize_url

    assert (
        normalize_url("https://EXAMPLE.com/docs/?utm_source=email&lang=en#install")
        == "https://example.com/docs?lang=en"
    )


def test_normalize_content_produces_stable_hash_for_whitespace_changes():
    from app.services.knowledge_crawl_service import calculate_content_hash

    first = calculate_content_hash("Billing\n\n  Refunds are processed in 5 days. ")
    second = calculate_content_hash(" Billing   Refunds are processed in 5 days.\n")

    assert first == second


def test_classify_page_change():
    from app.services.knowledge_crawl_service import classify_page_change

    assert classify_page_change(None, "new-hash") == "new"
    assert classify_page_change({"content_hash": "old-hash"}, "new-hash") == "changed"
    assert classify_page_change({"content_hash": "same"}, "same") == "unchanged"
