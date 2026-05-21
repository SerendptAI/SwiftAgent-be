"""
Forms Public Router — unauthenticated endpoints used by embedded widgets
and external websites to fetch form definitions and submit responses.
"""

import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException
from app.core.database import db
from app.models.form_models import FormResponse, FormSubmissionCreate, FormSubmissionResponse
from app.services.form_service import form_service

router = APIRouter()
logger = logging.getLogger(__name__)


async def _get_published_form(form_id: str) -> dict:
    """Shared helper: fetch a form doc by ID or raise 404."""
    form = await form_service.get_form_by_id(form_id)
    if not form:
        raise HTTPException(status_code=404, detail="Form not found.")
    return form


@router.get("/{form_id}", response_model=FormResponse, summary="Get Published Form")
async def get_public_form(form_id: str):
    """
    Retrieve a form definition by its ID. Used by the embedded widget or
    external website to render the form fields to the end-user.
    No authentication required.
    """
    return await _get_published_form(form_id)


@router.post("/{form_id}/submit", response_model=FormSubmissionResponse, summary="Submit Form Response")
async def submit_public_form(
    form_id: str,
    submission: FormSubmissionCreate,
    background_tasks: BackgroundTasks,
):
    """
    Accept a form submission from an end-user.
    No authentication required — the form_id acts as the public identifier.
    Triggers an alert email to the company in the background if the form has an alert_email set.
    """
    # Validate form exists and get its company context
    form = await _get_published_form(form_id)

    result = await form_service.submit_form(form_id, form.company_id, submission)

    # Fire-and-forget: send alert email if configured on a Website Form
    if form.alert_email:
        background_tasks.add_task(
            _send_submission_alert,
            alert_email=form.alert_email,
            form_title=form.form_title or form.website_link or "Form",
            data=submission.data,
        )

    return result


async def _send_submission_alert(alert_email: str, form_title: str, data: dict):
    """Background task: send a simple notification email when a form is submitted."""
    try:
        from app.services.email_utils import send_simple_email
        body_lines = [f"<b>New submission for: {form_title}</b><br><br>"]
        for key, value in data.items():
            body_lines.append(f"<b>{key.replace('_', ' ').title()}:</b> {value}<br>")
        await send_simple_email(
            to=alert_email,
            subject=f"New Form Submission — {form_title}",
            html_body="".join(body_lines),
        )
    except Exception as e:
        logger.warning("Form submission alert email failed: %s", e)
