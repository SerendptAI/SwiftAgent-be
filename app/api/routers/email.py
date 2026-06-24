import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import asyncio

from app.core.auth import get_current_user
from app.core.security import decode_access_token
from app.core.config import settings
from app.models.email_models import (
    EmailReplyRequest,
    EmailTicketResponse,
    EmailTicketSummary,
    TestDispatchRequest,
)
from app.services import company_service, company_email_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Email"])

TEMPLATES_DIR = Path(__file__).resolve().parent.parent.parent / "email_templates"
RESOLVE_CONFIRM_TEMPLATE = TEMPLATES_DIR / "resolve_confirm.html"
RESOLVED_TEMPLATE = TEMPLATES_DIR / "resolved.html"


def _load_template(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    return html.replace("{{base_url}}", settings.API_BASE_URL)


@router.post("/inbound")
async def inbound_email_webhook(request: Request):
    """Receive inbound emails from SendGrid Inbound Parse."""
    # Verify webhook secret
    secret = request.query_params.get("token") or request.headers.get("x-webhook-secret")
    if settings.SENDGRID_WEBHOOK_SECRET and secret != settings.SENDGRID_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

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
        # Return generic error to prevent leaking internal logic
        return {"status": "processed"}


@router.get("/resolve/{token}", response_class=HTMLResponse)
async def resolve_ticket_page(token: str):
    """Show a confirmation page when customer clicks 'Mark as Resolved'."""
    ticket = await company_email_service.get_ticket_by_resolve_token(token)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    company = await company_service.get_company(ticket["company_id"])
    company_name = company.get("name", "Support") if company else "Support"
    logo_url = company.get("logo_url") if company else None
    
    logo_url_fallback = logo_url or f"{settings.API_BASE_URL}/images/logo 2.png"

    if ticket["status"] == "resolved":
        html = _load_template(RESOLVED_TEMPLATE)
        html = html.replace("{{company_name}}", company_name)
        html = html.replace("{{company_logo_url}}", logo_url_fallback)
        return HTMLResponse(content=html, status_code=200)

    html = _load_template(RESOLVE_CONFIRM_TEMPLATE)
    html = html.replace("{{ticket_id}}", ticket["id"])
    html = html.replace("{{ticket_subject}}", ticket["subject"])
    html = html.replace("{{resolve_token}}", token)
    html = html.replace("{{company_name}}", company_name)
    html = html.replace("{{company_logo_url}}", logo_url_fallback)
    return HTMLResponse(content=html, status_code=200)


@router.post("/resolve/{token}/confirm", response_class=HTMLResponse)
async def confirm_resolve_ticket(token: str):
    """Confirm ticket resolution and show a styled confirmation page."""
    result = await company_email_service.resolve_ticket(token)
    html = _load_template(RESOLVED_TEMPLATE)

    if result:
        msg = "Your ticket has been confirmed as resolved. Thank you!"
        company_id = result["company_id"]
    else:
        ticket = await company_email_service.get_ticket_by_resolve_token(token)
        if ticket and ticket["status"] == "resolved":
            msg = "This ticket was already resolved."
            company_id = ticket["company_id"]
        else:
            raise HTTPException(status_code=404, detail="Ticket not found")

    company = await company_service.get_company(company_id)
    company_name = company.get("name", "Support") if company else "Support"
    logo_url = company.get("logo_url") if company else None
    logo_url_fallback = logo_url or f"{settings.API_BASE_URL}/images/logo 2.png"

    html = html.replace("{{company_name}}", company_name)
    html = html.replace("{{company_logo_url}}", logo_url_fallback)
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


@router.websocket("/{company_id}/tickets/ws")
async def tickets_websocket(
    websocket: WebSocket,
    company_id: str,
    token: str = Query(...)
):
    """Real-time WebSocket for pending tickets.
    Pushes the full ticket list upon connection, and whenever a ticket changes.
    Requires a valid JWT token passed as a query parameter (?token=...).
    """
    await websocket.accept()

    # Authenticate via query param token
    try:
        payload = decode_access_token(token)
        if not payload or not payload.get("sub"):
            await websocket.send_json({"type": "error", "message": "Invalid or missing token"})
            await websocket.close(code=1008)
            return
        user_id = payload.get("sub")
        
        # Verify the user has access to this company
        company = await company_service.get_company(company_id, user_id)
        if not company:
            await websocket.send_json({"type": "error", "message": "Company not found or unauthorized"})
            await websocket.close(code=1008)
            return
            
    except Exception as e:
        logger.error(f"WebSocket auth failed: {e}")
        await websocket.close(code=1008)
        return

    from fastapi.encoders import jsonable_encoder

    # Initial push of tickets
    try:
        tickets = await company_email_service.list_tickets(company_id, 50, 0)
        total = await company_email_service.count_tickets(company_id)
        await websocket.send_json({
            "items": jsonable_encoder(tickets),
            "total": total,
            "limit": 50,
            "skip": 0,
            "has_next": len(tickets) < total
        })
    except Exception as e:
        logger.error(f"Error fetching initial tickets for WS: {e}")
        await websocket.close()
        return

    from app.core.database import db

    try:
        # Watch for changes in email_tickets collection
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace", "delete"]}}}]
        async with db.email_tickets.watch(pipeline) as stream:
            async for change in stream:
                # Optional: Filter by company_id if it's available in the change document
                full_doc = change.get("fullDocument")
                if full_doc and full_doc.get("company_id") != company_id:
                    continue

                # Refetch and push the updated list
                tickets = await company_email_service.list_tickets(company_id, 50, 0)
                total = await company_email_service.count_tickets(company_id)
                await websocket.send_json({
                    "items": jsonable_encoder(tickets),
                    "total": total,
                    "limit": 50,
                    "skip": 0,
                    "has_next": len(tickets) < total
                })
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for company {company_id} tickets")
    except Exception as e:
        logger.error(f"WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass


@router.websocket("/{company_id}/tickets/{ticket_id}/ws")
async def ticket_detail_websocket(
    websocket: WebSocket,
    company_id: str,
    ticket_id: str,
    token: str = Query(...)
):
    """Real-time WebSocket for a specific ticket's details.
    Pushes the full ticket details upon connection, and whenever the ticket changes.
    Requires a valid JWT token passed as a query parameter (?token=...).
    """
    await websocket.accept()

    # Authenticate via query param token
    try:
        payload = decode_access_token(token)
        if not payload or not payload.get("sub"):
            await websocket.send_json({"type": "error", "message": "Invalid or missing token"})
            await websocket.close(code=1008)
            return
        user_id = payload.get("sub")
        
        # Verify the user has access to this company
        company = await company_service.get_company(company_id, user_id)
        if not company:
            await websocket.send_json({"type": "error", "message": "Company not found or unauthorized"})
            await websocket.close(code=1008)
            return
            
    except Exception as e:
        logger.error(f"WebSocket auth failed: {e}")
        await websocket.close(code=1008)
        return

    from fastapi.encoders import jsonable_encoder

    # Initial push
    try:
        result = await _get_ticket_with_context(company_id, ticket_id)
        if result:
            ticket = result.get("ticket", {})
            attributed_chat = result.get("attributed_chat")
            if attributed_chat:
                ticket["attributed_chat"] = attributed_chat
            await websocket.send_json({"type": "init", "data": jsonable_encoder(ticket)})
    except Exception as e:
        logger.error(f"Error fetching initial ticket detail for WS: {e}")
        await websocket.close()
        return

    from app.core.database import db

    try:
        # Watch for changes to this specific ticket
        pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}, "fullDocument.id": ticket_id}}]
        async with db.email_tickets.watch(pipeline) as stream:
            async for change in stream:
                full_doc = change.get("fullDocument")
                if full_doc and full_doc.get("company_id") == company_id:
                    res = await _get_ticket_with_context(company_id, ticket_id)
                    if res:
                        ticket = res.get("ticket", {})
                        attributed_chat = res.get("attributed_chat")
                        if attributed_chat:
                            ticket["attributed_chat"] = attributed_chat
                        await websocket.send_json({"type": "update", "data": jsonable_encoder(ticket)})
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for ticket {ticket_id}")
    except Exception as e:
        logger.error(f"Ticket detail WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass


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
            agent_name=current_user.get("name"),
            agent_avatar_url=current_user.get("picture"),
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


@router.patch("/{company_id}/tickets/{ticket_id}/resolve")
async def resolve_ticket_by_agent_endpoint(
    company_id: str,
    ticket_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Resolve a ticket manually from the dashboard and trigger an email to the customer."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    result = await company_email_service.resolve_ticket_by_agent(company_id, ticket_id)
    if not result:
        raise HTTPException(status_code=404, detail="Ticket not found or already resolved")

    return {"status": "resolved", "ticket_id": ticket_id}


@router.patch("/{company_id}/tickets/{ticket_id}/reopen")
async def reopen_ticket_endpoint(
    company_id: str,
    ticket_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Reopen a resolved ticket."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    result = await company_email_service.reopen_ticket(company_id, ticket_id)
    if not result:
        raise HTTPException(status_code=404, detail="Ticket not found or not resolved")

    return {"status": "reopened", "ticket_id": ticket_id}

@router.post("/test-dispatch")
async def dispatch_test_suite(
    request: TestDispatchRequest,
    current_user: dict = Depends(get_current_user)
):
    """Dispatch all 10 templates securely from the backend to the target recipients."""
    success, msg = await company_email_service.dispatch_all_test_templates(
        company_id=request.company_id,
        recipients=request.recipients,
        auth_user=current_user
    )
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "success", "message": msg}
