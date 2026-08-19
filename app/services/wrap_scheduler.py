"""
Swift Wrap Scheduler — manages background cron jobs for the weekly email report.

Uses APScheduler to trigger the generation and sending of the Swift Wrap
to eligible company admins every Friday at 5:00 PM WAT (16:00 UTC).
"""

import logging
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.database import db
from app.core.config import settings
from app.services import swift_wrap_service

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler = AsyncIOScheduler()


async def _scheduled_wrap_task():
    """The actual job function executed by the scheduler for all companies."""
    logger.info("Starting weekly Swift Wrap generation across all companies.")
    
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=7)
    
    try:
        # Fetch all active companies
        companies = await db.companies.find({}).to_list(length=None)
        
        for company in companies:
            company_id = company.get("id") or str(company.get("_id"))
            
            # Find admin users for this company
            admins = await db.users.find({
                "company_id": company_id,
                "role": "admin"
            }).to_list(length=None)
            
            for admin in admins:
                # Check opt-out preference
                unsubscribed = admin.get("unsubscribed_from_wrap", False)
                if unsubscribed:
                    logger.info(f"Skipping wrap for {admin.get('email')} (opted out).")
                    continue
                
                recipient_email = admin.get("email")
                if not recipient_email:
                    continue
                    
                logger.info(f"Generating wrap for company {company_id} -> {recipient_email}")
                
                # Execute sending (this internalizes error handling per company)
                await swift_wrap_service.generate_and_send_wrap(
                    company_id=company_id,
                    start_date=start_date,
                    end_date=end_date,
                    recipient_email=recipient_email
                )
                
        logger.info("Finished weekly Swift Wrap generation run.")
    except Exception as e:
        logger.exception("Error in global scheduled wrap task: %s", e)


def init_scheduler():
    """Initialize and start the wrap scheduler."""
    if not getattr(settings, "ENABLE_WRAP_SCHEDULER", True):
        logger.info("Swift Wrap scheduler is disabled in settings.")
        return

    logger.info("Initializing Swift Wrap scheduler...")
    
    # Schedule for Friday 5:00 PM WAT (16:00 UTC)
    # Cron trigger: day_of_week='fri', hour=16, minute=0
    _scheduler.add_job(
        _scheduled_wrap_task,
        trigger=CronTrigger(day_of_week='fri', hour=16, minute=0, timezone=timezone.utc),
        id="weekly_swift_wrap_job",
        replace_existing=True
    )
    
    _scheduler.start()


def close_scheduler():
    """Gracefully shutdown the scheduler."""
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("Swift Wrap scheduler shutdown complete.")
