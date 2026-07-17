"""
Stroll Scheduler — manages background cron jobs for automated dashboard strolls.

Uses APScheduler (AsyncIOScheduler) to register and run strolls based on
each company's configured cron schedule.
"""

import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.database import db
from app.services import stroll_service
from app.core.plan_enforcement import enforce_stroll_limit
from fastapi import HTTPException
from app.services.billing_service import billing_service

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler = AsyncIOScheduler()

# Concurrency guard — prevents overlapping strolls for the same company.
# Protects against: cron firing while a previous run is still in progress,
# or a manual trigger overlapping with a scheduled one.
_running_strolls: set[str] = set()


def is_stroll_running(company_id: str) -> bool:
    """Check if a stroll is currently running for a company."""
    return company_id in _running_strolls


def mark_stroll_running(company_id: str) -> bool:
    """Mark a stroll as running. Returns False if already running (skip)."""
    if company_id in _running_strolls:
        return False
    _running_strolls.add(company_id)
    return True


def mark_stroll_done(company_id: str):
    """Mark a stroll as done (remove from running set)."""
    _running_strolls.discard(company_id)


async def _scheduled_stroll_task(company_id: str):
    """The actual job function executed by the scheduler."""
    if not mark_stroll_running(company_id):
        logger.warning(
            f"Stroll already running for company {company_id}, skipping scheduled run"
        )
        return

    logger.info(f"Starting scheduled stroll for company {company_id}")
    try:
        company = await db.companies.find_one({"id": company_id})
        if company:
            try:
                await enforce_stroll_limit(company)
            except HTTPException as e:
                logger.warning(f"Stroll limit reached for company {company_id}, skipping scheduled run")
                return

        config = await stroll_service.get_stroll_config(company_id)
        if not config:
            logger.warning(f"Config not found for scheduled stroll (company: {company_id})")
            # auto-remove job if config is gone
            remove_stroll_job(company_id)
            return

        version = await stroll_service.run_stroll(company_id, config)

        if version.status != "success":
            logger.error(f"Scheduled stroll failed for {company_id}: {version.status}")
            return

        prev = await stroll_service.get_latest_version(company_id)
        diff = stroll_service.diff_stroll(version.graph, prev)

        committed = await stroll_service.commit_stroll(company_id, version, diff)
        if committed:
            logger.info(f"Scheduled stroll completed for {company_id}")
        else:
            logger.info(f"Scheduled stroll found no changes for {company_id}")

        # Record metered usage since the scheduled stroll succeeded
        await billing_service.ingest_meter_event(company_id, "stroll_used")

    except Exception as e:
        logger.exception(f"Scheduled stroll encountered an error for {company_id}: {e}")
    finally:
        mark_stroll_done(company_id)


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

    # Register the meter queue processor (every 5 minutes)
    try:
        from app.services.meter_queue_service import process_meter_queue
        _scheduler.add_job(
            process_meter_queue,
            trigger=CronTrigger(minute="*/5"),
            id="meter_queue_processor",
            replace_existing=True,
        )
        logger.info("Meter queue processor scheduled (every 5 minutes)")
    except Exception as e:
        logger.error(f"Failed to schedule meter queue processor: {e}")


async def close_scheduler():
    """Stop the scheduler."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Stroll APScheduler stopped")
