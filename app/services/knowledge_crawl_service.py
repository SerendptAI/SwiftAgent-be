import asyncio
import fnmatch
import hashlib
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from collections import deque
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from pymongo import ReturnDocument

from app.core.database import db
from app.models.knowledge_crawl_models import CrawlRunStatus
from app.services import page_reader_service
from app.services.knowledge_embedding_service import delete_page_vectors, replace_page_vectors

_CRAWL_NAMESPACE = uuid.UUID("eac0fd17-f0ec-42db-b7ab-d4f0316d4c23")
_RUNNING_CRAWLS: set[str] = set()
logger = logging.getLogger(__name__)
_TRACKING_PARAMS = {"fbclid", "gclid", "mc_cid", "mc_eid", "ref"}


class CrawlAlreadyRunningError(RuntimeError):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def normalize_url(url: str) -> str:
    """Return a stable crawl identity while retaining meaningful query parameters."""
    from urllib.parse import parse_qsl, urlencode, urlunsplit

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    netloc = hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{hostname}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    if path != "/":
        path = path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    query.sort()
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def normalize_content(content: str) -> str:
    return " ".join((content or "").split())


def calculate_content_hash(content: str) -> str:
    return hashlib.sha256(normalize_content(content).encode("utf-8")).hexdigest()


def classify_page_change(existing: dict | None, content_hash: str) -> str:
    if existing is None:
        return "new"
    return "unchanged" if existing.get("content_hash") == content_hash else "changed"


def _same_origin(root_url: str, candidate: str) -> bool:
    root = urlsplit(root_url)
    other = urlsplit(candidate)
    return (
        root.scheme.lower() == other.scheme.lower() and root.netloc.lower() == other.netloc.lower()
    )


def _matches_rules(url: str, include: list[str], exclude: list[str]) -> bool:
    path = urlsplit(url).path or "/"
    if include and not any(fnmatch.fnmatch(path, pattern) for pattern in include):
        return False
    return not any(fnmatch.fnmatch(path, pattern) for pattern in exclude)


async def _fetch_text_safe(url: str, max_bytes: int = 2_000_000) -> str | None:
    safe, _, final_url = await page_reader_service._resolve_final_url(url)
    if not safe:
        return None
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        response = await client.get(final_url)
        if response.status_code != 200:
            return None
        if len(response.content) > max_bytes:
            return None
        return response.text


async def _discover_sitemap_urls(root_url: str, candidates: list[str] | None = None) -> list[str]:
    candidates = candidates or [
        urljoin(root_url.rstrip("/") + "/", "sitemap.xml"),
        urljoin(root_url.rstrip("/") + "/", "sitemap_index.xml"),
    ]
    discovered: list[str] = []
    visited_sitemaps: set[str] = set()
    queue = deque(candidates)
    while queue and len(visited_sitemaps) < 20:
        sitemap_url = normalize_url(queue.popleft())
        if sitemap_url in visited_sitemaps or not _same_origin(root_url, sitemap_url):
            continue
        visited_sitemaps.add(sitemap_url)
        xml_text = await _fetch_text_safe(sitemap_url)
        if not xml_text:
            continue
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            continue
        locations = [
            element.text.strip()
            for element in root.iter()
            if element.tag.endswith("loc") and element.text
        ]
        if root.tag.endswith("sitemapindex"):
            queue.extend(locations)
        else:
            discovered.extend(locations)
    return discovered


async def _load_robots(root_url: str) -> RobotFileParser | None:
    robots_url = urljoin(root_url.rstrip("/") + "/", "robots.txt")
    text = await _fetch_text_safe(robots_url, max_bytes=500_000)
    if text is None:
        return None
    parser = RobotFileParser()
    parser.set_url(robots_url)
    parser.parse(text.splitlines())
    return parser


async def discover_pages(config: dict) -> list[str]:
    """Discover bounded same-origin pages via sitemap followed by internal-link BFS."""
    root_url = normalize_url(str(config["root_url"]))
    max_pages = int(config.get("max_pages", 100))
    max_depth = int(config.get("max_depth", 3))
    include = config.get("include_patterns") or []
    exclude = config.get("exclude_patterns") or []
    robots = await _load_robots(root_url) if config.get("respect_robots_txt", True) else None

    seeds = [root_url]
    if config.get("follow_sitemap", True):
        sitemap_candidates = list(robots.site_maps() or []) if robots else []
        sitemap_candidates.extend(
            [
                urljoin(root_url.rstrip("/") + "/", "sitemap.xml"),
                urljoin(root_url.rstrip("/") + "/", "sitemap_index.xml"),
            ]
        )
        seeds.extend(await _discover_sitemap_urls(root_url, sitemap_candidates))

    queue = deque((normalize_url(url), 0) for url in seeds)
    seen: set[str] = set()
    output: list[str] = []
    while queue and len(output) < max_pages:
        url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if not _same_origin(root_url, url) or not _matches_rules(url, include, exclude):
            continue
        if robots and not robots.can_fetch("SwiftAgentBot", url):
            continue
        output.append(url)
        if depth >= max_depth:
            continue
        page = await crawl_page(url, force_refresh=True)
        if "error" in page:
            continue
        for link in page.get("links", []):
            normalized = normalize_url(link)
            if normalized not in seen:
                queue.append((normalized, depth + 1))
        delay = int(config.get("request_delay_ms", 0))
        if delay:
            await asyncio.sleep(delay / 1000)
    return output


async def crawl_page(url: str, force_refresh: bool = False) -> dict:
    # Discovery and ingestion can request the same URL in one run. The shared
    # page-reader cache prevents a second browser navigation while preserving
    # its redirect and SSRF checks.
    result = await page_reader_service.read_website_page(url, force_refresh=force_refresh)
    if "error" in result:
        return result
    path = urlsplit(result.get("url", url)).path.rstrip("/")
    title = path.rsplit("/", 1)[-1].replace("-", " ").title() if path else urlsplit(url).hostname
    return {**result, "title": title or "Untitled page"}


def _page_id(company_id: str, canonical_url: str) -> str:
    return str(uuid.uuid5(_CRAWL_NAMESPACE, f"{company_id}:{canonical_url}"))


async def _process_missing_pages(
    company_id: str, seen_urls: set[str], config: dict, now: datetime
) -> int:
    deleted = 0
    cursor = db.knowledge_pages.find(
        {"company_id": company_id, "status": {"$in": ["active", "missing"]}}
    )
    async for page in cursor:
        if page.get("canonical_url", page.get("url")) in seen_urls:
            continue
        missing_count = int(page.get("missing_count", 0)) + 1
        if config.get("remove_deleted_pages", True) and missing_count >= int(
            config.get("missing_threshold", 2)
        ):
            await delete_page_vectors(company_id, page["id"])
            await db.knowledge_pages.update_one(
                {"id": page["id"], "company_id": company_id},
                {"$set": {"status": "deleted", "missing_count": missing_count, "updated_at": now}},
            )
            deleted += 1
        else:
            await db.knowledge_pages.update_one(
                {"id": page["id"], "company_id": company_id},
                {"$set": {"status": "missing", "missing_count": missing_count, "updated_at": now}},
            )
    return deleted


async def get_freshness_summary(company_id: str) -> dict:
    config = await db.knowledge_crawl_configs.find_one({"company_id": company_id})
    if not config:
        raise ValueError("crawl configuration not found")
    last_run = await db.knowledge_crawl_runs.find_one(
        {"company_id": company_id}, sort=[("completed_at", -1)]
    )
    indexed_pages = await db.knowledge_pages.count_documents(
        {"company_id": company_id, "status": "active"}
    )
    stale_pages = await db.knowledge_pages.count_documents(
        {"company_id": company_id, "status": "missing"}
    )
    failed_pages = await db.knowledge_pages.count_documents(
        {"company_id": company_id, "status": "failed"}
    )
    gap_count = await db.knowledge_gaps.count_documents(
        {"company_id": company_id, "status": "open"}
    )
    now = _utcnow()
    next_run_at = _as_utc(config.get("next_run_at"))
    last_completed_at = _as_utc(config.get("last_completed_at"))
    if not last_run:
        status = "never_crawled"
    elif last_run.get("status") == CrawlRunStatus.FAILED.value:
        status = "failed"
    elif last_run.get("status") == CrawlRunStatus.PARTIAL.value or last_run.get("pages_failed", 0):
        status = "degraded"
    elif next_run_at and next_run_at <= now:
        status = "due"
    elif last_completed_at and now - last_completed_at > timedelta(days=2):
        status = "stale"
    else:
        status = "fresh"
    return {
        "company_id": company_id,
        "status": status,
        "last_successful_crawl": last_completed_at,
        "next_run_at": next_run_at,
        "indexed_pages": indexed_pages,
        "stale_pages": stale_pages,
        "failed_pages": max(failed_pages, int((last_run or {}).get("pages_failed", 0))),
        "new_pages_last_run": int((last_run or {}).get("pages_new", 0)),
        "changed_pages_last_run": int((last_run or {}).get("pages_changed", 0)),
        "knowledge_gap_count": gap_count,
    }


async def create_queued_run(company_id: str, trigger: str = "manual") -> dict:
    now = _utcnow()
    run = {
        "id": str(uuid.uuid4()),
        "company_id": company_id,
        "status": CrawlRunStatus.QUEUED.value,
        "trigger": trigger,
        "started_at": now,
        "completed_at": None,
        "pages_discovered": 0,
        "pages_new": 0,
        "pages_changed": 0,
        "pages_unchanged": 0,
        "pages_deleted": 0,
        "pages_failed": 0,
        "chunks_embedded": 0,
        "discovery_truncated": False,
        "error_summary": None,
    }
    await db.knowledge_crawl_runs.insert_one(dict(run))
    return run


async def run_company_crawl(
    company_id: str, trigger: str = "manual", run_id: str | None = None
) -> dict:
    precreated = run_id is not None
    if company_id in _RUNNING_CRAWLS:
        raise CrawlAlreadyRunningError(f"crawl already running for company {company_id}")
    _RUNNING_CRAWLS.add(company_id)
    run_id = run_id or str(uuid.uuid4())
    lock_token = str(uuid.uuid4())
    started_at = _utcnow()
    run = {
        "id": run_id,
        "company_id": company_id,
        "status": CrawlRunStatus.DISCOVERING.value,
        "trigger": trigger,
        "started_at": started_at,
        "completed_at": None,
        "pages_discovered": 0,
        "pages_new": 0,
        "pages_changed": 0,
        "pages_unchanged": 0,
        "pages_deleted": 0,
        "pages_failed": 0,
        "chunks_embedded": 0,
        "discovery_truncated": False,
        "error_summary": None,
    }
    try:
        config = await db.knowledge_crawl_configs.find_one_and_update(
            {
                "company_id": company_id,
                "$or": [
                    {"lock_expires_at": {"$exists": False}},
                    {"lock_expires_at": None},
                    {"lock_expires_at": {"$lte": started_at}},
                ],
            },
            {
                "$set": {
                    "lock_token": lock_token,
                    "lock_expires_at": started_at + timedelta(hours=24),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if not config:
            existing_config = await db.knowledge_crawl_configs.find_one({"company_id": company_id})
            if not existing_config:
                raise ValueError("crawl configuration not found")
            raise CrawlAlreadyRunningError(f"crawl already running for company {company_id}")
        company = await db.companies.find_one({"id": company_id})
        if not company:
            raise ValueError("company not found")
        user_id = company["user_id"]
        if not precreated:
            await db.knowledge_crawl_runs.insert_one(dict(run))
        else:
            await db.knowledge_crawl_runs.update_one({"id": run_id}, {"$set": dict(run)})
        await db.knowledge_crawl_configs.update_one(
            {"company_id": company_id}, {"$set": {"last_started_at": started_at}}
        )

        urls = await discover_pages(config)
        if not urls:
            raise RuntimeError(
                "no crawlable pages were discovered; deletion reconciliation skipped"
            )
        run["pages_discovered"] = len(urls)
        # When the configured cap is reached, discovery may have omitted valid
        # pages. Treat the run as partial so omitted pages cannot be deleted.
        run["discovery_truncated"] = len(urls) >= int(config.get("max_pages", 100))
        run["status"] = CrawlRunStatus.CRAWLING.value
        seen_urls: set[str] = set()
        source_id = f"website:{company_id}"

        for url in urls:
            canonical_url = normalize_url(url)
            result = await crawl_page(url)
            if "error" in result or not normalize_content(result.get("content", "")):
                run["pages_failed"] += 1
                continue
            seen_urls.add(canonical_url)
            content = normalize_content(result["content"])
            content_hash = calculate_content_hash(content)
            existing = await db.knowledge_pages.find_one(
                {"company_id": company_id, "canonical_url": canonical_url}
            )
            change = classify_page_change(existing, content_hash)
            now = _utcnow()
            page_id = existing.get("id") if existing else _page_id(company_id, canonical_url)

            if change in {"new", "changed"}:
                try:
                    chunks = await replace_page_vectors(
                        company_id=company_id,
                        user_id=user_id,
                        page_id=page_id,
                        source_id=source_id,
                        url=canonical_url,
                        title=result.get("title") or "Untitled page",
                        content=content,
                        content_hash=content_hash,
                    )
                except Exception as exc:
                    logger.exception("Failed to embed crawled page %s", canonical_url)
                    run["pages_failed"] += 1
                    await db.knowledge_pages.update_one(
                        {"id": page_id, "company_id": company_id},
                        {
                            "$set": {
                                "id": page_id,
                                "company_id": company_id,
                                "source_id": source_id,
                                "url": canonical_url,
                                "canonical_url": canonical_url,
                                "title": result.get("title") or "Untitled page",
                                "status": "failed",
                                "last_seen_at": now,
                                "last_error": str(exc),
                                "updated_at": now,
                            },
                            "$setOnInsert": {"first_seen_at": now},
                        },
                        upsert=True,
                    )
                    continue
                run["chunks_embedded"] += chunks
                run[f"pages_{change}"] += 1
                first_seen = existing.get("first_seen_at", now) if existing else now
                await db.knowledge_pages.update_one(
                    {"id": page_id, "company_id": company_id},
                    {
                        "$set": {
                            "id": page_id,
                            "company_id": company_id,
                            "source_id": source_id,
                            "url": canonical_url,
                            "canonical_url": canonical_url,
                            "title": result.get("title") or "Untitled page",
                            "content_hash": content_hash,
                            "status": "active",
                            "language": result.get("language", "en"),
                            "chunk_count": chunks,
                            "first_seen_at": first_seen,
                            "last_seen_at": now,
                            "last_changed_at": now,
                            "last_embedded_at": now,
                            "missing_count": 0,
                            "last_error": None,
                            "updated_at": now,
                        }
                    },
                    upsert=True,
                )
            else:
                run["pages_unchanged"] += 1
                await db.knowledge_pages.update_one(
                    {"id": page_id, "company_id": company_id},
                    {
                        "$set": {
                            "status": "active",
                            "last_seen_at": now,
                            "missing_count": 0,
                            "last_error": None,
                            "updated_at": now,
                        }
                    },
                )

        if run["pages_failed"] == 0 and not run["discovery_truncated"]:
            run["pages_deleted"] = await _process_missing_pages(
                company_id, seen_urls, config, _utcnow()
            )
            run["status"] = CrawlRunStatus.COMPLETED.value
        else:
            run["status"] = CrawlRunStatus.PARTIAL.value

        completed_at = _utcnow()
        run["completed_at"] = completed_at
        await db.knowledge_crawl_runs.update_one({"id": run_id}, {"$set": dict(run)})
        config_update = {"last_completed_at": completed_at} if run["status"] == "completed" else {}
        if config_update:
            await db.knowledge_crawl_configs.update_one(
                {"company_id": company_id}, {"$set": config_update}
            )
        return run
    except Exception as exc:
        run["status"] = CrawlRunStatus.FAILED.value
        run["completed_at"] = _utcnow()
        run["error_summary"] = str(exc)
        try:
            await db.knowledge_crawl_runs.update_one({"id": run_id}, {"$set": dict(run)})
        except Exception:
            pass
        raise
    finally:
        try:
            await db.knowledge_crawl_configs.update_one(
                {"company_id": company_id, "lock_token": lock_token},
                {"$unset": {"lock_token": "", "lock_expires_at": ""}},
            )
        except Exception:
            pass
        _RUNNING_CRAWLS.discard(company_id)
