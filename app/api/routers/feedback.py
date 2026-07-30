"""
Customer feedback router — endpoints for submitting genuine CSAT, NPS, CES ratings,
sentiment, and feedback themes on widget conversations and email tickets.
"""

import logging
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, status
from app.core.auth import get_current_user
from app.core.database import get_database
from app.models.analytics_models import FeedbackSubmitRequest, FeedbackSubmitResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Analytics Feedback"])


async def _process_feedback_submission(req: FeedbackSubmitRequest, db, company_id: str | None = None) -> FeedbackSubmitResponse:
    now = datetime.now(tz=timezone.utc)
    feedback_id = str(uuid4())

    target_company_id = req.company_id or company_id
    if not target_company_id and req.session_id:
        chat = await db.widget_conversations.find_one({"session_id": req.session_id}, {"company_id": 1})
        if chat:
            target_company_id = chat.get("company_id")
    if not target_company_id and req.ticket_id:
        ticket = await db.email_tickets.find_one({"id": req.ticket_id}, {"company_id": 1})
        if ticket:
            target_company_id = ticket.get("company_id")

    feedback_doc = {
        "id": feedback_id,
        "company_id": target_company_id,
        "session_id": req.session_id,
        "ticket_id": req.ticket_id,
        "csat_score": req.csat_score,
        "nps_score": req.nps_score,
        "ces_score": req.ces_score,
        "sentiment": req.sentiment,
        "feedback_theme": req.feedback_theme,
        "comment": req.comment,
        "submitted_at": now,
        "updated_at": now,
    }

    await db.customer_feedback.insert_one(feedback_doc)

    update_data = {}
    if req.csat_score is not None:
        update_data["csat_score"] = float(req.csat_score)
    if req.nps_score is not None:
        update_data["nps_score"] = int(req.nps_score)
    if req.ces_score is not None:
        update_data["ces_score"] = int(req.ces_score)
    if req.sentiment is not None:
        update_data["sentiment"] = req.sentiment
    if req.feedback_theme is not None:
        update_data["feedback_theme"] = req.feedback_theme
    if req.comment is not None:
        update_data["feedback_comment"] = req.comment

    if update_data:
        if req.session_id:
            await db.widget_conversations.update_one(
                {"session_id": req.session_id},
                {"$set": update_data},
            )
        if req.ticket_id:
            await db.email_tickets.update_one(
                {"id": req.ticket_id},
                {"$set": update_data},
            )

    logger.info("Saved genuine customer feedback %s for company %s", feedback_id, target_company_id)

    return FeedbackSubmitResponse(
        success=True,
        message="Customer feedback submitted successfully.",
        feedback_id=feedback_id,
        updated_at=now,
    )


@router.post("/api/v1/analytics/feedback", response_model=FeedbackSubmitResponse)
async def submit_analytics_feedback(
    request: FeedbackSubmitRequest,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """
    Authenticated endpoint to submit customer experience ratings (CSAT, NPS, CES, sentiment)
    for a specific conversation session or ticket.
    """
    user_company_id = current_user.get("company_id")
    return await _process_feedback_submission(request, db, company_id=user_company_id)


@router.post("/api/v1/public/feedback", response_model=FeedbackSubmitResponse)
async def submit_public_feedback(
    request: FeedbackSubmitRequest,
    db=Depends(get_database),
):
    """
    Public widget endpoint to submit customer experience ratings (CSAT, NPS, CES, sentiment)
    from an end-user chat session without dashboard login.
    """
    if not request.session_id and not request.ticket_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either session_id or ticket_id must be provided when submitting feedback.",
        )
    return await _process_feedback_submission(request, db)
