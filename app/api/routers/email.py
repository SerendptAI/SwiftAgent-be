"""
Email Ticketing Router — handles inbound webhooks from SendGrid,
company ticket management, replies, and ticket resolution.
"""

import logging

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


# ---------------------------------------------------------------------------
# Inbound webhook (SendGrid Inbound Parse)
# ---------------------------------------------------------------------------


@router.post("/inbound")
async def inbound_email_webhook(request: Request):
    """
    Receive inbound emails from SendGrid Inbound Parse.

    SendGrid sends multipart/form-data POST with parsed email fields.
    This endpoint is unauthenticated — relies on URL obscurity
    and optional SendGrid webhook signature verification.
    """
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
        # always return 200 to SendGrid so it doesn't retry forever
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Ticket resolution (public — token-based auth)
# ---------------------------------------------------------------------------


@router.get("/resolve/{token}", response_class=HTMLResponse)
async def resolve_ticket_page(token: str):
    """Show a confirmation page when customer clicks 'Mark as Resolved'."""
    ticket = await company_email_service.get_ticket_by_resolve_token(token)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    if ticket["status"] == "resolved":
        return HTMLResponse(
            content=_resolved_page_html(ticket, already_resolved=True),
            status_code=200,
        )

    return HTMLResponse(
        content=_resolve_confirm_page_html(ticket, token),
        status_code=200,
    )


@router.post("/resolve/{token}/confirm")
async def confirm_resolve_ticket(token: str):
    """Confirm ticket resolution."""
    result = await company_email_service.resolve_ticket(token)
    if not result:
        ticket = await company_email_service.get_ticket_by_resolve_token(token)
        if ticket and ticket["status"] == "resolved":
            return {"status": "already_resolved"}
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"status": "resolved", "ticket_id": result["id"]}


# ---------------------------------------------------------------------------
# Company ticket management (JWT-authenticated)
# ---------------------------------------------------------------------------


@router.get("/{company_id}/tickets")
async def list_tickets(
    company_id: str,
    status: str | None = Query(default=None, description="Filter by status"),
    limit: int = Query(default=50, ge=1, le=100),
    skip: int = Query(default=0, ge=0),
    current_user: dict = Depends(get_current_user),
):
    """List email tickets for a company."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    tickets = await company_email_service.list_tickets(company_id, status, limit, skip)
    total = await company_email_service.count_tickets(company_id, status)

    return {
        "items": tickets,
        "total": total,
        "limit": limit,
        "skip": skip,
        "has_next": (skip + len(tickets)) < total,
    }


@router.get("/{company_id}/tickets/{ticket_id}", response_model=EmailTicketResponse)
async def get_ticket(
    company_id: str,
    ticket_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get full ticket thread."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    ticket = await company_email_service.get_ticket(company_id, ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return ticket


@router.post("/{company_id}/tickets/{ticket_id}/reply")
async def reply_to_ticket(
    company_id: str,
    ticket_id: str,
    req: EmailReplyRequest,
    current_user: dict = Depends(get_current_user),
):
    """Send a reply to a ticket, emailed from company@swfty.email."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if not company.get("email_slug"):
        raise HTTPException(
            status_code=400,
            detail="Company email not configured. Set up your email slug first.",
        )

    try:
        result = await company_email_service.send_ticket_reply(
            company_id, ticket_id, req.body_text, req.body_html
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Failed to send ticket reply: %s", e)
        raise HTTPException(status_code=502, detail="Failed to send email")


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


# ---------------------------------------------------------------------------
# HTML pages for ticket resolution
# ---------------------------------------------------------------------------


def _resolve_confirm_page_html(ticket: dict, token: str) -> str:
    """Confirmation page HTML — asks customer to confirm resolution."""
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Resolve Ticket</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #e2e8f0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }}
        .card {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 16px;
            padding: 48px;
            max-width: 480px;
            width: 100%;
            text-align: center;
            backdrop-filter: blur(12px);
        }}
        h1 {{ font-size: 24px; margin-bottom: 16px; color: #f1f5f9; }}
        .subject {{ color: #94a3b8; font-size: 14px; margin-bottom: 32px; }}
        .btn {{
            display: inline-block;
            background: #10b981;
            color: #fff;
            padding: 14px 40px;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            border: none;
            cursor: pointer;
            text-decoration: none;
            transition: background 0.2s;
        }}
        .btn:hover {{ background: #059669; }}
        .note {{ color: #64748b; font-size: 13px; margin-top: 24px; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>Resolve your ticket?</h1>
        <p class="subject">Ticket #{ticket['id']} — {ticket['subject']}</p>
        <form action="/api/v1/email/resolve/{token}/confirm" method="POST">
            <button type="submit" class="btn">✓ Confirm Resolution</button>
        </form>
        <p class="note">This will close your support ticket.</p>
    </div>
</body>
</html>"""


def _resolved_page_html(ticket: dict, already_resolved: bool = False) -> str:
    """Success page after ticket is resolved."""
    message = "This ticket was already resolved." if already_resolved else "Your ticket has been resolved!"
    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Ticket Resolved</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            color: #e2e8f0;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 24px;
        }}
        .card {{
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid rgba(148, 163, 184, 0.1);
            border-radius: 16px;
            padding: 48px;
            max-width: 480px;
            width: 100%;
            text-align: center;
            backdrop-filter: blur(12px);
        }}
        .check {{
            font-size: 48px;
            margin-bottom: 16px;
        }}
        h1 {{ font-size: 24px; margin-bottom: 12px; color: #10b981; }}
        .subject {{ color: #94a3b8; font-size: 14px; margin-bottom: 8px; }}
        .note {{ color: #64748b; font-size: 13px; margin-top: 24px; }}
    </style>
</head>
<body>
    <div class="card">
        <div class="check">✓</div>
        <h1>{message}</h1>
        <p class="subject">Ticket #{ticket['id']} — {ticket['subject']}</p>
        <p class="note">Thank you for your feedback.</p>
    </div>
</body>
</html>"""
