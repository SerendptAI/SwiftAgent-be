import asyncio
import logging
import sys
from datetime import datetime, timezone, timedelta

# Setup logging to see output
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def trigger_test():
    # Import the FastAPI DB module
    from app.core.database import db
    from app.services import swift_wrap_service
    
    recipient_email = "team@swiftagents.org"
    logger.info("Looking up user %s...", recipient_email)
    
    company_id = "1e1bccc0-40a5-4700-a5e2-0dd55cddb75c"
    
    logger.info("Found company_id: %s", company_id)
    
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=7)
    
    logger.info("Generating and sending wrap...")
    success = await swift_wrap_service.generate_and_send_wrap(
        company_id=company_id,
        start_date=start_date,
        end_date=end_date,
        recipient_email=recipient_email
    )
    
    if success:
        logger.info("Successfully sent wrap to %s", recipient_email)
    else:
        logger.error("Failed to send wrap")

if __name__ == "__main__":
    asyncio.run(trigger_test())
