"""ARQ durable background-job queue.

Heavy, long-running work (crawls, GDPR exports/deletions, embeddings) moves
off the web request path into ARQ workers backed by Redis. Jobs survive
worker restarts and are retried with backoff on failure. When
``REDIS_URL`` is unset or the pool is unavailable, ``enqueue_job``
degrades gracefully: callers can fall back to in-process execution so no
feature breaks in deployments without Redis.
"""

import asyncio
import logging
from dataclasses import dataclass

from app.core.config import settings

logger = logging.getLogger(__name__)

_arq_pool = None
_pool_lock = asyncio.Lock()


@dataclass
class JobNames:
    """Central registry of queueable jobs — keeps producer/worker in sync."""

    KNOWLEDGE_CRAWL = "knowledge_crawl"
    GDPR_EXPORT = "gdpr_export"
    GDPR_DELETE = "gdpr_delete"
    RETENTION_SWEEP = "retention_sweep"


JOB_NAMES = JobNames()


@dataclass
class EnqueueResult:
    queued: bool
    job_id: str | None = None
    reason: str | None = None


async def get_pool():
    """Return the shared ARQ pool, creating it on first use.

    Returns None when Redis is not configured — callers decide whether to run
    inline or skip.
    """
    global _arq_pool
    if _arq_pool is not None:
        return _arq_pool
    if not settings.REDIS_URL:
        return None
    async with _pool_lock:
        if _arq_pool is None:
            try:
                from arq import create_pool
                from arq.connections import RedisSettings

                redis_settings = RedisSettings.from_dsn(settings.REDIS_URL)
                _arq_pool = await create_pool(redis_settings)
            except Exception:
                logger.exception("Failed to connect ARQ pool")
                return None
    return _arq_pool


async def close_pool() -> None:
    global _arq_pool
    if _arq_pool is not None:
        try:
            await _arq_pool.aclose()
        except Exception:
            logger.exception("Failed to close ARQ pool")
        _arq_pool = None


async def enqueue_job(
    job_name: str,
    *args,
    job_id: str | None = None,
    max_tries: int = 3,
    **kwargs,
) -> EnqueueResult:
    """Queue a job. Returns an EnqueueResult describing what happened."""
    pool = await get_pool()
    if pool is None:
        return EnqueueResult(queued=False, reason="queue_unavailable")
    try:
        job = await pool.enqueue_job(job_name, *args, _job_id=job_id, **kwargs)
        if job is None:
            # ARQ returns None when a job with the same id is already queued.
            return EnqueueResult(
                queued=False,
                job_id=job_id,
                reason="duplicate_job_id",
            )
        return EnqueueResult(queued=True, job_id=job.job_id)
    except Exception as exc:
        logger.exception("Failed to enqueue %s", job_name)
        return EnqueueResult(queued=False, reason=str(exc))


async def enqueue_or_run(
    job_name: str,
    func,
    *args,
    job_id: str | None = None,
    max_tries: int = 3,
    **kwargs,
):
    """Try ARQ first; if the queue is unavailable, run inline.

    This is the bridge for gradual migration: features call this instead of
    spawning their own tasks, and behavior degrades to today's in-process
    model when Redis is absent.
    """
    result = await enqueue_job(job_name, *args, job_id=job_id, **kwargs)
    if result.queued:
        return result

    if result.reason == "duplicate_job_id":
        logger.info("Job %s already queued; skipping inline run", job_id)
        return result

    logger.warning("Queue unavailable (%s); running %s inline", result.reason, job_name)
    asyncio.create_task(func(*args, **kwargs))
    return EnqueueResult(queued=False, job_id=None, reason="ran_inline")


def worker_functions() -> list:
    """Return all worker task functions for the ARQ worker process."""
    from app.workers import tasks as worker_tasks

    return worker_tasks.all_worker_tasks


class WorkerSettings:
    """ARQ WorkerSettings for `arq app.core.queue.WorkerSettings`.

    Functions are resolved at worker start so the web process never imports
    them. Long crawls get generous timeouts; retries use ARQ's per-job
    ``max_tries``.
    """

    functions: list = []

    @classmethod
    def build(cls):
        cls.functions = worker_functions()
        return cls

    max_jobs = 4
    job_timeout = 3600
    max_tries = 3
    health_check_interval = 30

    @classmethod
    async def startup(cls, ctx):
        from app.core.database import create_indexes

        try:
            await create_indexes()
        except Exception:
            logger.exception("Worker startup index creation failed")

    @classmethod
    async def shutdown(cls, ctx):
        pass
