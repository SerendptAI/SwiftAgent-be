"""
Ticket Workflow Scheduler — background jobs for ticket operations.

Jobs:
  - Auto-escalation: hourly scan of widget conversations stuck on AI failures.
  - SLA watch: every 5 minutes, flag tickets past their SLA deadlines.

Follows the same AsyncIOScheduler pattern as wrap_scheduler/stroll_scheduler
and is wired into the FastAPI lifespan in main.py.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.database import db
from app.services import ticket_service

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler = AsyncIOScheduler()


async def _scheduled_auto_escalate_task():
    """Hourly: scan every company's widget chats for conversations that need a human."""
    logger.info("Starting auto-escalation scan.")
    try:
        company_ids = await db.widget_conversations.distinct(
            "company_id", {"escalated": {"$ne": True}}
        )
        for company_id in company_ids:
            if not company_id:
                continue
            try:
                created = await ticket_service.auto_escalate_chats(company_id)
                if created:
                    logger.info(
                        "Auto-escalated %d chat(s) for company %s", len(created), company_id
                    )
            except Exception as e:
                logger.exception(
                    "Auto-escalation scan failed for company %s: %s", company_id, e
                )
        logger.info("Auto-escalation scan complete.")
    except Exception as e:
        logger.exception("Auto-escalation scan failed: %s", e)


async def _scheduled_sla_watch_task():
    """Every 5 minutes: flag tickets past their first-response / resolution deadlines."""
    try:
        flagged = await ticket_service.sla_watch_loop()
        if flagged:
            logger.info("SLA watch flagged %d ticket(s).", flagged)
    except Exception as e:
        logger.exception("SLA watch pass failed: %s", e)


def init_scheduler():
    """Initialize and start the ticket workflow scheduler."""
    if not getattr(settings, "ENABLE_TICKET_SCHEDULER", True):
        logger.info("Ticket workflow scheduler is disabled in settings.")
        return

    if _scheduler.running:
        return

    logger.info("Initializing ticket workflow scheduler...")

    # Auto-escalation: every hour on the hour
    _scheduler.add_job(
        _scheduled_auto_escalate_task,
        trigger=CronTrigger(minute=0),
        id="ticket_auto_escalate_job",
        replace_existing=True,
    )

    # SLA watch: every 5 minutes
    _scheduler.add_job(
        _scheduled_sla_watch_task,
        trigger=CronTrigger(minute="*/5"),
        id="ticket_sla_watch_job",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("Ticket workflow scheduler started.")


def close_scheduler():
    """Gracefully shutdown the scheduler."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Ticket workflow scheduler shutdown complete.")
