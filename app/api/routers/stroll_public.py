"""
Stroll Public Router — unauthenticated endpoints for the frontend widget.

Endpoints:
- POST   /stroll/{company_id}/report   — receive DOM-intercept stroll reports from the live widget
"""

import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.core.database import db
from app.services import stroll_service
from app.models.stroll_models import WidgetStrollReport

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Stroll Public"])


@router.post("/{company_id}/report", status_code=202)
async def submit_widget_report(
    company_id: str,
    report: WidgetStrollReport,
    background_tasks: BackgroundTasks,
):
    """
    Accept a live DOM-intercept stroll report from the frontend widget.
    Processes the screenshots via vision AI and builds the nav graph in the background.
    """
    # Verify the company exists
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # The widget sends widget_page_nodes, we pass them to the service in the background
    background_tasks.add_task(stroll_service.process_widget_stroll, company_id, report)

    return {"status": "accepted", "message": "Widget stroll report received and processing started"}
