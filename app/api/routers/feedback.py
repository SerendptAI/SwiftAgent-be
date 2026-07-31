"""
Customer feedback router — endpoints for submitting genuine CSAT, NPS, CES ratings,
sentiment, and feedback themes on widget conversations and email tickets.
"""

import logging
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Depends, HTTPException, status
from app.core.auth import verify_analytics_secret_key
from app.core.database import get_database
from app.models.analytics_models import FeedbackSubmitRequest, FeedbackSubmitResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Feedback"])


async def _process_feedback_submission(
    request: FeedbackSubmitRequest,
    db,
    company_id: str | None = None,
) -> FeedbackSubmitResponse:
    if not request.session_id and not request.ticket_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either session_id or ticket_id must be provided to submit feedback.",
        )

    now = datetime.now(tz=timezone.utc)
    feedback_id = f"fb_{uuid4().hex[:12]}"

    # Store raw customer feedback document in customer_feedback collection
    doc = {
        "_id": feedback_id,
        "session_id": request.session_id,
        "ticket_id": request.ticket_id,
        "company_id": company_id or request.company_id,
        "csat_score": request.csat_score,
        "nps_score": request.nps_score,
        "ces_score": request.ces_score,
        "sentiment": request.sentiment,
        "comment": request.comment,
        "theme": request.feedback_theme,
        "created_at": now,
    }
    await db.customer_feedback.insert_one(doc)

    # In-place update: if session_id is provided, attach feedback to widget_conversations
    if request.session_id:
        update_fields = {"feedback_submitted_at": now}
        if request.csat_score is not None:
            update_fields["csat_score"] = request.csat_score
        if request.nps_score is not None:
            update_fields["nps_score"] = request.nps_score
        if request.ces_score is not None:
            update_fields["ces_score"] = request.ces_score
        if request.sentiment is not None:
            update_fields["customer_sentiment"] = request.sentiment
        if request.feedback_theme:
            update_fields["feedback_theme"] = request.feedback_theme

        await db.widget_conversations.update_one(
            {"session_id": request.session_id},
            {"$set": update_fields},
        )

    # In-place update: if ticket_id is provided, attach feedback to email_tickets
    if request.ticket_id:
        update_fields = {"feedback_submitted_at": now}
        if request.csat_score is not None:
            update_fields["csat_score"] = request.csat_score
        if request.nps_score is not None:
            update_fields["nps_score"] = request.nps_score
        if request.ces_score is not None:
            update_fields["ces_score"] = request.ces_score
        if request.sentiment is not None:
            update_fields["customer_sentiment"] = request.sentiment
        if request.feedback_theme:
            update_fields["feedback_theme"] = request.feedback_theme

        await db.email_tickets.update_one(
            {"ticket_id": request.ticket_id},
            {"$set": update_fields},
        )

    return FeedbackSubmitResponse(
        success=True,
        message="Customer feedback submitted successfully.",
        feedback_id=feedback_id,
        updated_at=now,
    )


@router.post("/api/v1/analytics/feedback", response_model=FeedbackSubmitResponse)
async def submit_analytics_feedback(
    request: FeedbackSubmitRequest,
    auth: dict = Depends(verify_analytics_secret_key),
    db=Depends(get_database),
):
    """
    Authenticated endpoint to submit customer experience ratings (CSAT, NPS, CES, sentiment)
    for a specific conversation session or ticket.
    """
    user_company_id = auth.get("company_id") or request.company_id
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
