from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, Field, HttpUrl, field_validator


class CrawlRunStatus(StrEnum):
    QUEUED = "queued"
    DISCOVERING = "discovering"
    CRAWLING = "crawling"
    EMBEDDING = "embedding"
    COMPLETED = "completed"
    PARTIAL = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PageStatus(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    DELETED = "deleted"
    ERROR = "error"


class CrawlConfigCreate(BaseModel):
    company_id: str = Field(min_length=1)
    root_url: HttpUrl
    enabled: bool = True
    schedule: str = "0 2 * * *"
    timezone: str = "UTC"
    max_pages: int = Field(default=100, ge=1, le=2000)
    max_depth: int = Field(default=3, ge=0, le=10)
    include_patterns: list[str] = Field(default_factory=list)
    exclude_patterns: list[str] = Field(
        default_factory=lambda: ["/login*", "/admin*", "/checkout*"]
    )
    respect_robots_txt: bool = True
    follow_sitemap: bool = True
    remove_deleted_pages: bool = True
    missing_threshold: int = Field(default=2, ge=1, le=10)
    request_delay_ms: int = Field(default=250, ge=0, le=10_000)

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        try:
            CronTrigger.from_crontab(value)
        except ValueError as exc:
            raise ValueError("schedule must be a valid five-field cron expression") from exc
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class CrawlConfigUpdate(BaseModel):
    root_url: HttpUrl | None = None
    enabled: bool | None = None
    schedule: str | None = None
    timezone: str | None = None
    max_pages: int | None = Field(default=None, ge=1, le=2000)
    max_depth: int | None = Field(default=None, ge=0, le=10)
    include_patterns: list[str] | None = None
    exclude_patterns: list[str] | None = None
    respect_robots_txt: bool | None = None
    follow_sitemap: bool | None = None
    remove_deleted_pages: bool | None = None
    missing_threshold: int | None = Field(default=None, ge=1, le=10)
    request_delay_ms: int | None = Field(default=None, ge=0, le=10_000)

    @field_validator("schedule")
    @classmethod
    def validate_cron(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                CronTrigger.from_crontab(value)
            except ValueError as exc:
                raise ValueError("schedule must be a valid five-field cron expression") from exc
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ValueError("timezone must be a valid IANA timezone") from exc
        return value


class CrawlConfig(CrawlConfigCreate):
    id: str
    created_at: datetime
    updated_at: datetime
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    next_run_at: datetime | None = None


class CrawlRun(BaseModel):
    id: str
    company_id: str
    status: CrawlRunStatus = CrawlRunStatus.QUEUED
    trigger: Literal["manual", "scheduled"] = "manual"
    started_at: datetime
    completed_at: datetime | None = None
    pages_discovered: int = 0
    pages_new: int = 0
    pages_changed: int = 0
    pages_unchanged: int = 0
    pages_deleted: int = 0
    pages_failed: int = 0
    chunks_embedded: int = 0
    discovery_truncated: bool = False
    error_summary: str | None = None


class CrawledPage(BaseModel):
    id: str
    company_id: str
    source_id: str
    url: str
    canonical_url: str
    title: str = "Untitled page"
    content_hash: str
    status: PageStatus = PageStatus.ACTIVE
    language: str = "en"
    chunk_count: int = 0
    first_seen_at: datetime
    last_seen_at: datetime
    last_changed_at: datetime
    last_embedded_at: datetime | None = None
    missing_count: int = 0
    last_error: str | None = None


class FreshnessSummary(BaseModel):
    company_id: str
    status: Literal["fresh", "due", "stale", "degraded", "failed", "never_crawled"]
    last_successful_crawl: datetime | None = None
    next_run_at: datetime | None = None
    indexed_pages: int = 0
    stale_pages: int = 0
    failed_pages: int = 0
    new_pages_last_run: int = 0
    changed_pages_last_run: int = 0
    knowledge_gap_count: int = 0


class KnowledgeGapStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class KnowledgeGap(BaseModel):
    id: str
    company_id: str
    topic_key: str
    topic: str
    representative_queries: list[str] = Field(default_factory=list)
    question_count: int = 0
    unique_sessions: int = 0
    escalation_count: int = 0
    average_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    priority_score: float = Field(default=0.0, ge=0.0)
    priority: Literal["low", "medium", "high"] = "low"
    status: KnowledgeGapStatus = KnowledgeGapStatus.OPEN
    first_seen_at: datetime
    last_seen_at: datetime
    resolved_at: datetime | None = None
    verification: dict[str, Any] | None = None


class KnowledgeGapUpdate(BaseModel):
    status: KnowledgeGapStatus | None = None
    topic: str | None = None


class CrawlRunRequest(BaseModel):
    trigger: Literal["manual", "scheduled"] = "manual"
