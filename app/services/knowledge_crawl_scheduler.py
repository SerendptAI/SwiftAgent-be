import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.database import db
from app.services.knowledge_crawl_service import (
    CrawlAlreadyRunningError,
    run_company_crawl,
)

logger = logging.getLogger(__name__)
_scheduler = AsyncIOScheduler()


async def reconcile_orphaned_runs() -> None:
    """Fail runs that could not have an active worker after a restart/crash."""
    now = datetime.now(UTC)
    await db.knowledge_crawl_runs.update_many(
        {"status": "queued", "started_at": {"$lt": now - timedelta(minutes=10)}},
        {
            "$set": {
                "status": "failed",
                "completed_at": now,
                "error_summary": "orphaned queued run recovered at startup",
            }
        },
    )
    await db.knowledge_crawl_runs.update_many(
        {
            "status": {"$in": ["discovering", "crawling", "embedding"]},
            "started_at": {"$lt": now - timedelta(hours=24)},
        },
        {
            "$set": {
                "status": "failed",
                "completed_at": now,
                "error_summary": "stale crawl recovered at startup",
            }
        },
    )


async def _scheduled_crawl_task(company_id: str) -> None:
    try:
        await run_company_crawl(company_id, trigger="scheduled")
    except CrawlAlreadyRunningError:
        logger.info("Knowledge crawl already running for %s; scheduled run skipped", company_id)
    except Exception:
        logger.exception("Scheduled knowledge crawl failed for %s", company_id)
    finally:
        job = _scheduler.get_job(f"knowledge_crawl_{company_id}")
        if job and job.next_run_time:
            await db.knowledge_crawl_configs.update_one(
                {"company_id": company_id},
                {"$set": {"next_run_at": job.next_run_time}},
            )


def schedule_crawl_job(company_id: str, cron_expr: str, timezone: str = "UTC"):
    if not _scheduler.running:
        logger.warning("Knowledge crawl scheduler is not running; cannot schedule %s", company_id)
        return None
    job_id = f"knowledge_crawl_{company_id}"
    trigger = CronTrigger.from_crontab(cron_expr, timezone=timezone)
    job = _scheduler.add_job(
        _scheduled_crawl_task,
        trigger=trigger,
        id=job_id,
        args=[company_id],
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return job.next_run_time


def remove_crawl_job(company_id: str) -> None:
    job_id = f"knowledge_crawl_{company_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)


async def init_scheduler() -> None:
    if not settings.ENABLE_KNOWLEDGE_CRAWL_SCHEDULER or _scheduler.running:
        return
    await reconcile_orphaned_runs()
    _scheduler.start()
    cursor = db.knowledge_crawl_configs.find({"enabled": True})
    async for config in cursor:
        try:
            next_run_at = schedule_crawl_job(
                config["company_id"],
                config.get("schedule", "0 2 * * *"),
                config.get("timezone", "UTC"),
            )
            if next_run_at:
                await db.knowledge_crawl_configs.update_one(
                    {"company_id": config["company_id"]},
                    {"$set": {"next_run_at": next_run_at}},
                )
        except Exception:
            logger.exception("Invalid crawl schedule for %s", config.get("company_id"))


async def close_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
