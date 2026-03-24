"""
Stroll Scheduler — manages background cron jobs for automated dashboard strolls.

Uses APScheduler (AsyncIOScheduler) to register and run strolls based on
each company's configured cron schedule.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.database import db
from app.services import stroll_service, stroll_index_service

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler = AsyncIOScheduler()


async def _scheduled_stroll_task(company_id: str):
    """The actual job function executed by the scheduler."""
    logger.info(f"Starting scheduled stroll for company {company_id}")
    try:
        config = await stroll_service.get_stroll_config(company_id)
        if not config:
            logger.warning(f"Config not found for scheduled stroll (company: {company_id})")
            # auto-remove job if config is gone
            remove_stroll_job(company_id)
            return

        version = await stroll_service.run_stroll(company_id, config)

        if version.status != "success":
            logger.error(f"Scheduled stroll failed for {company_id}: {version.status}")
            version.diff = None
            await stroll_service.db.stroll_versions.insert_one(version.model_dump())
            return

        prev = await stroll_service.get_latest_version(company_id)
        diff = stroll_service.diff_stroll(version.graph, prev)

        committed = await stroll_service.commit_stroll(company_id, version, diff)
        if committed:
            await stroll_index_service.build_index(company_id, committed)
            logger.info(f"Scheduled stroll completed/indexed for {company_id}")
        else:
            logger.info(f"Scheduled stroll found no changes for {company_id}")

    except Exception as e:
        logger.exception(f"Scheduled stroll encountered an error for {company_id}: {e}")


def _cron_to_trigger(cron_expr: str) -> CronTrigger:
    """Convert a standard cron string (e.g. '0 2 * * *') to an APScheduler CronTrigger."""
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        # fallback to daily at 2am
        return CronTrigger(minute="0", hour="2", day="*", month="*", day_of_week="*")
    
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
    )


def schedule_stroll_job(company_id: str, cron_expr: str):
    """Add or update the scheduled stroll job for a company."""
    if not _scheduler.running:
        logger.warning(f"Scheduler is not running, skipping job schedule for {company_id}")
        return

    job_id = f"stroll_{company_id}"
    
    # remove existing job if any
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)

    trigger = _cron_to_trigger(cron_expr)
    
    _scheduler.add_job(
        _scheduled_stroll_task,
        trigger=trigger,
        id=job_id,
        args=[company_id],
        replace_existing=True,
    )
    logger.info(f"Scheduled stroll for {company_id} with cron '{cron_expr}'")


def remove_stroll_job(company_id: str):
    """Remove the scheduled stroll job for a company."""
    job_id = f"stroll_{company_id}"
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info(f"Removed scheduled stroll for {company_id}")


async def init_scheduler():
    """Start the scheduler and load all active configs from the DB."""
    if _scheduler.running:
        return

    _scheduler.start()
    logger.info("Stroll APScheduler started")

    # load all configs and schedule them
    try:
        cursor = db.stroll_configs.find({})
        count = 0
        async for doc in cursor:
            company_id = doc.get("company_id")
            schedule = doc.get("schedule", "0 2 * * *")
            if company_id:
                schedule_stroll_job(company_id, schedule)
                count += 1
        logger.info(f"Loaded {count} stroll schedules from DB")
    except Exception as e:
        logger.error(f"Failed to load stroll schedules during startup: {e}")


async def close_scheduler():
    """Stop the scheduler."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Stroll APScheduler stopped")
