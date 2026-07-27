"""
Forms Public Router — unauthenticated endpoints used by embedded widgets
and external websites to fetch form definitions and submit responses.
"""

import logging
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import Response

from app.core.database import db
from app.models.form_models import (
    FormResponse, 
    FormSubmissionCreate, 
    FormSubmissionResponse,
    WidgetSubmissionCreate,
)
from app.services.form_service import form_service
from app.services import form_key_service
import json

router = APIRouter()
logger = logging.getLogger(__name__)


async def _get_published_form(form_id: str) -> dict:
    """Shared helper: fetch a form doc by ID or raise 404."""
    form = await form_service.get_form_by_id(form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found.")
    return form


async def _send_submission_alert(
    alert_email: str,
    form_title: str,
    data: dict,
    form_id: str | None = None,
    submitted_at=None,
    page_url: str | None = None,
):
    """Background task: send a branded notification email when a form is submitted."""
    try:
        from app.services.company_email_service import send_form_submission_alert
        await send_form_submission_alert(
            alert_email=alert_email,
            form_title=form_title,
            data=data,
            form_id=form_id,
            submitted_at=submitted_at,
            page_url=page_url,
        )
    except Exception as e:
        logger.warning("Form submission alert email failed: %s", e)


@router.get("/widget.js", summary="Get Widget JavaScript")
async def get_widget_js():
    """Serve the embeddable JavaScript widget file."""
    widget_path = Path("app/static/widget.js")
    if not widget_path.exists():
        raise HTTPException(status_code=404, detail="Widget script not found.")
    
    content = widget_path.read_text()
    return Response(
        content=content, 
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=3600"}
    )


@router.post("/widget/submit", summary="Submit Form from Widget")
async def submit_widget_form(
    request: Request,
    background_tasks: BackgroundTasks,
    x_public_key: str | None = Header(None, alias="X-Public-Key", description="Public key from the widget snippet"),
    public_key: str | None = Query(None, description="Public key from query param (avoids CORS preflight)"),
):
    """
    Accept a form submission from the embedded JS widget.
    Authenticated via the X-Public-Key header or public_key query param.
    """
    key_to_use = x_public_key or public_key
    if not key_to_use:
        raise HTTPException(status_code=401, detail="Missing public key.")

    # Verify public key
    key_info = await form_key_service.verify_public_key(key_to_use)
    if not key_info:
        raise HTTPException(status_code=401, detail="Invalid or revoked public key.")
        
    form_id = key_info["form_id"]
    company_id = key_info["company_id"]

    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes)
        submission = WidgetSubmissionCreate(**data)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON payload.")

    # Get form to check for alert email
    form = await _get_published_form(form_id)

    # Process submission
    result = await form_service.submit_from_widget(form_id, company_id, submission)

    # Fire-and-forget alert email
    if getattr(form, "alert_email", None):
        background_tasks.add_task(
            _send_submission_alert,
            alert_email=form.alert_email,
            form_title=form.form_title or form.website_link or "Website Form",
            data=submission.data,
            form_id=form_id,
            submitted_at=result.submitted_at,
            page_url=submission.page_url,
        )

    return {"submission_id": result.id, "status": "ok"}


@router.get("/online/{form_id}", response_model=FormResponse, summary="Get Online Form")
async def get_online_form(form_id: str):
    """Get published online form definition."""
    return await _get_published_form(form_id)


@router.post("/online/{form_id}/submit", response_model=FormSubmissionResponse, summary="Submit Online Form")
async def submit_online_form(
    form_id: str,
    submission: FormSubmissionCreate,
    background_tasks: BackgroundTasks,
):
    """Accept an online form submission."""
    form = await _get_published_form(form_id)
    result = await form_service.submit_form(form_id, form.company_id, submission)

    if getattr(form, "alert_email", None):
        background_tasks.add_task(
            _send_submission_alert,
            alert_email=form.alert_email,
            form_title=form.form_title or "Online Form",
            data=submission.data,
            form_id=form_id,
            submitted_at=result.submitted_at,
        )

    return result


@router.get("/{form_id}", response_model=FormResponse, summary="Get Published Form")
async def get_public_form(form_id: str):
    """Legacy: Retrieve a form definition by its ID."""
    return await _get_published_form(form_id)


@router.post("/{form_id}/submit", response_model=FormSubmissionResponse, summary="Submit Form Response")
async def submit_public_form(
    form_id: str,
    submission: FormSubmissionCreate,
    background_tasks: BackgroundTasks,
):
    """Legacy: Accept a form submission from an end-user."""
    form = await _get_published_form(form_id)
    result = await form_service.submit_form(form_id, form.company_id, submission)

    if getattr(form, "alert_email", None):
        background_tasks.add_task(
            _send_submission_alert,
            alert_email=form.alert_email,
            form_title=form.form_title or getattr(form, "website_link", "Form"),
            data=submission.data,
            form_id=form_id,
            submitted_at=result.submitted_at,
        )

    return result
