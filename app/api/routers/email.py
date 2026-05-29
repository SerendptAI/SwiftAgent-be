import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.core.auth import get_current_user
from app.core.config import settings
from app.models.email_models import (
    EmailReplyRequest,
    EmailTicketResponse,
    EmailTicketSummary,
)
from app.services import company_service, company_email_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Email"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "email_templates"
RESOLVE_CONFIRM_TEMPLATE = TEMPLATES_DIR / "resolve_confirm.html"
RESOLVED_TEMPLATE = TEMPLATES_DIR / "resolved.html"


def _load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@router.post("/inbound")
async def inbound_email_webhook(request: Request):
    """Receive inbound emails from SendGrid Inbound Parse."""
    try:
        form = await request.form()
        payload = {key: form[key] for key in form}

        logger.info(
            "Inbound email received: from=%s to=%s subject=%s",
            payload.get("from", ""),
            payload.get("to", ""),
            payload.get("subject", ""),
        )

        result = await company_email_service.process_inbound_email(payload)
        return result
    except Exception as e:
        logger.exception("Error processing inbound email: %s", e)
        return {"status": "error", "message": "Failed to process inbound email. The message could not be routed to a ticket."}


@router.get("/resolve/{token}", response_class=HTMLResponse)
async def resolve_ticket_page(token: str):
    """Show a confirmation page when customer clicks 'Mark as Resolved'."""
    ticket = await company_email_service.get_ticket_by_resolve_token(token)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    if ticket["status"] == "resolved":
        html = _load_template(RESOLVED_TEMPLATE)
        html = html.replace("{{confirmation_message}}", "This ticket was already resolved.")
        return HTMLResponse(content=html, status_code=200)

    html = _load_template(RESOLVE_CONFIRM_TEMPLATE)
    html = html.replace("{{ticket_id}}", ticket["id"])
    html = html.replace("{{ticket_subject}}", ticket["subject"])
    html = html.replace("{{resolve_token}}", token)
    return HTMLResponse(content=html, status_code=200)


@router.post("/resolve/{token}/confirm", response_class=HTMLResponse)
async def confirm_resolve_ticket(token: str):
    """Confirm ticket resolution and show a styled confirmation page."""
    result = await company_email_service.resolve_ticket(token)
    html = _load_template(RESOLVED_TEMPLATE)

    if result:
        msg = "Your ticket has been confirmed as resolved. Thank you!"
    else:
        ticket = await company_email_service.get_ticket_by_resolve_token(token)
        if ticket and ticket["status"] == "resolved":
            msg = "This ticket was already resolved."
        else:
            raise HTTPException(status_code=404, detail="Ticket not found")

    html = html.replace("{{confirmation_message}}", msg)
    return HTMLResponse(content=html, status_code=200)


@router.get("/{company_id}/tickets")
async def list_tickets(
    company_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """List unresolved email tickets for a company (Pending section)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    tickets = await company_email_service.list_tickets(company_id, limit, skip)
    total = await company_email_service.count_tickets(company_id)

    return {
        "items": tickets,
        "total": total,
        "limit": limit,
        "skip": skip,
        "has_next": (skip + len(tickets)) < total,
    }


async def _get_ticket_with_context(company_id: str, ticket_id: str) -> dict:
    """Fetch ticket with attributed chat context."""
    return await company_email_service.get_ticket_with_chat(company_id, ticket_id)


@router.get("/{company_id}/tickets/{ticket_id}", response_model=EmailTicketResponse)
async def get_ticket(
    company_id: str,
    ticket_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get full ticket thread with attributed chat (if escalated from chat)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    result = await _get_ticket_with_context(company_id, ticket_id)
    if not result:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    # Return ticket with attributed chat reference if available
    ticket = result.get("ticket", {})
    attributed_chat = result.get("attributed_chat")
    
    if attributed_chat:
        ticket["attributed_chat"] = attributed_chat
    
    return ticket


@router.post("/{company_id}/tickets/{ticket_id}/reply")
async def reply_to_ticket(
    company_id: str,
    ticket_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Send a reply to a ticket, emailed from company@swfty.email.

    Accepts either:
    - application/json: { "body_text": "...", "body_html": "..." }  (legacy, no attachments)
    - multipart/form-data: body_text + body_html + attachments[]     (new, with file attachments)
    """
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if not company.get("email_slug"):
        raise HTTPException(
            status_code=400,
            detail="Company email not configured. Set up your email slug first.",
        )

    content_type = request.headers.get("content-type", "")
    attachment_data = []

    if "multipart/form-data" in content_type:
        # New path: form data with optional file attachments
        form = await request.form()
        body_text = form.get("body_text", "")
        body_html = form.get("body_html")

        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB per file
        MAX_ATTACHMENTS = 5

        files = form.getlist("attachments")
        if len(files) > MAX_ATTACHMENTS:
            raise HTTPException(
                status_code=422,
                detail=f"Maximum {MAX_ATTACHMENTS} attachments allowed"
            )

        for file in files:
            if not hasattr(file, "read"):
                continue  # skip non-file fields
            content = await file.read()
            if len(content) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=422,
                    detail=f"File '{file.filename}' exceeds the 10MB size limit"
                )
            attachment_data.append({
                "filename": file.filename or "attachment",
                "content_type": file.content_type or "application/octet-stream",
                "content": content,
            })
    else:
        # Legacy path: JSON body (no attachments)
        body = await request.json()
        body_text = body.get("body_text", "")
        body_html = body.get("body_html")

    if not body_text or not body_text.strip():
        raise HTTPException(status_code=422, detail="Reply body cannot be empty")

    try:
        result = await company_email_service.send_ticket_reply(
            company_id, ticket_id, body_text.strip(), body_html,
            attachments=attachment_data if attachment_data else None,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to send ticket reply: %s", e)
        raise HTTPException(status_code=502, detail="Failed to send the reply email. The mail server may be temporarily unavailable. Please try again.")


@router.patch("/{company_id}/tickets/{ticket_id}/seen")
async def mark_ticket_seen(
    company_id: str,
    ticket_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Mark all messages in a ticket as seen."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    success = await company_email_service.mark_ticket_seen(company_id, ticket_id)
    if not success:
        ticket = await company_email_service.get_ticket(company_id, ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
    return {"status": "success"}
