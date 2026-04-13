"""
Company Email Service — handles sending ticket replies via SendGrid
and processing inbound email webhooks.

Outbound: company replies from dashboard → sent from slug@swfty.email
Inbound: customer replies to email thread → matched to ticket → stored as follow-up
"""

import logging
import re
from datetime import datetime, timezone
from uuid import uuid4

from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail,
    From,
    To,
    Subject,
    Content,
    Header,
    MimeType,
)

from app.core.config import settings
from app.core.database import db
from app.services import company_service

logger = logging.getLogger(__name__)

_sg_client = None


def _get_sendgrid_client() -> SendGridAPIClient:
    global _sg_client
    if _sg_client is None:
        _sg_client = SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)
    return _sg_client


# ---------------------------------------------------------------------------
# Ticket CRUD
# ---------------------------------------------------------------------------


async def create_ticket(
    company_id: str,
    customer_email: str,
    subject: str,
    chat_summary: str,
    chat_session_id: str | None = None,
    customer_name: str | None = None,
) -> dict:
    """Create a new support ticket (called by the AI agent)."""
    ticket_id = str(uuid4())[:8].upper()  # short readable ID like "A1B2C3D4"
    resolve_token = str(uuid4())
    now = datetime.now(tz=timezone.utc)

    doc = {
        "id": ticket_id,
        "company_id": company_id,
        "customer_email": customer_email,
        "customer_name": customer_name,
        "subject": subject,
        "status": "pending",
        "resolve_token": resolve_token,
        "messages": [
            {
                "direction": "system",
                "body_text": chat_summary,
                "body_html": None,
                "sender_email": "system",
                "message_id": None,
                "timestamp": now,
                "seen": False,
            }
        ],
        "unseen_count": 1,
        "chat_session_id": chat_session_id,
        "chat_summary": chat_summary,
        "created_at": now,
        "updated_at": now,
    }

    await db.email_tickets.insert_one(doc)
    logger.info("Created ticket %s for company %s", ticket_id, company_id)
    return doc


async def get_ticket(company_id: str, ticket_id: str) -> dict | None:
    """Get a single ticket by ID."""
    return await db.email_tickets.find_one(
        {"company_id": company_id, "id": ticket_id}
    )


async def list_tickets(
    company_id: str,
    status: str | None = None,
    limit: int = 50,
    skip: int = 0,
) -> list:
    """List tickets for a company, optionally filtered by status."""
    query: dict = {"company_id": company_id}
    if status:
        query["status"] = status

    pipeline = [
        {"$match": query},
        {"$sort": {"updated_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {
            "$project": {
                "id": 1,
                "company_id": 1,
                "customer_email": 1,
                "customer_name": 1,
                "subject": 1,
                "status": 1,
                "unseen_count": 1,
                "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                "created_at": 1,
                "updated_at": 1,
            }
        },
    ]
    cursor = db.email_tickets.aggregate(pipeline)
    return await cursor.to_list(length=limit)


async def count_tickets(company_id: str, status: str | None = None) -> int:
    """Count tickets, optionally by status."""
    query: dict = {"company_id": company_id}
    if status:
        query["status"] = status
    return await db.email_tickets.count_documents(query)


async def mark_ticket_seen(company_id: str, ticket_id: str) -> bool:
    """Mark all messages in a ticket as seen."""
    result = await db.email_tickets.update_one(
        {"company_id": company_id, "id": ticket_id},
        {
            "$set": {
                "messages.$[].seen": True,
                "unseen_count": 0,
            }
        },
    )
    return result.modified_count > 0


# ---------------------------------------------------------------------------
# Outbound — company replies to a ticket
# ---------------------------------------------------------------------------


def _build_reply_html(
    body_html: str,
    company_name: str,
    resolve_url: str,
    logo_url: str | None = None,
) -> str:
    """Build the outbound email HTML with 'Mark as Resolved' button."""
    logo_block = ""
    if logo_url:
        logo_block = f'<img src="{logo_url}" alt="{company_name}" style="max-height:40px;margin-bottom:16px;" />'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8" /></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
             color: #1a1a1a; max-width: 600px; margin: 0 auto; padding: 24px;">
  {logo_block}
  <div style="line-height: 1.6;">
    {body_html}
  </div>
  <hr style="border: none; border-top: 1px solid #e5e5e5; margin: 32px 0;" />
  <div style="text-align: center; margin: 24px 0;">
    <p style="color: #666; font-size: 14px; margin-bottom: 12px;">
      Is your issue resolved?
    </p>
    <a href="{resolve_url}" 
       style="display: inline-block; background-color: #10b981; color: #ffffff; 
              text-decoration: none; padding: 12px 32px; border-radius: 8px; 
              font-weight: 600; font-size: 14px;">
      ✓ Mark as Resolved
    </a>
  </div>
  <p style="color: #999; font-size: 12px; text-align: center;">
    Sent by {company_name} via Swift Agent
  </p>
</body>
</html>"""


async def send_ticket_reply(
    company_id: str,
    ticket_id: str,
    body_text: str,
    body_html: str | None = None,
) -> dict:
    """Send a reply from the company to the customer via SendGrid."""
    ticket = await get_ticket(company_id, ticket_id)
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found")

    company = await company_service.get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")

    email_slug = company.get("email_slug")
    if not email_slug:
        raise ValueError("Company has no email slug configured")

    company_name = company.get("name", "Support")
    from_email = f"{email_slug}@{settings.EMAIL_DOMAIN}"
    resolve_url = f"{settings.API_BASE_URL}/api/v1/email/resolve/{ticket['resolve_token']}"
    logo_url = company.get("logo_url")

    # build HTML body with resolve button
    html_body = body_html or f"<p>{body_text}</p>"
    full_html = _build_reply_html(html_body, company_name, resolve_url, logo_url)

    # email subject with ticket ID for threading
    subject = f"Re: [Ticket #{ticket_id}] {ticket['subject']}"

    # get last message_id for threading headers
    last_message_id = None
    for msg in reversed(ticket.get("messages", [])):
        if msg.get("message_id"):
            last_message_id = msg["message_id"]
            break

    # generate message ID for this outbound
    outbound_message_id = f"<{uuid4()}@{settings.EMAIL_DOMAIN}>"

    # build SendGrid message
    message = Mail(
        from_email=From(from_email, company_name),
        to_emails=To(ticket["customer_email"]),
        subject=Subject(subject),
    )
    message.add_content(Content(MimeType.text, body_text))
    message.add_content(Content(MimeType.html, full_html))

    # add threading headers
    message.add_header(Header("Message-ID", outbound_message_id))
    if last_message_id:
        message.add_header(Header("In-Reply-To", last_message_id))
        message.add_header(Header("References", last_message_id))

    # add ticket ID as custom header for inbound matching
    message.add_header(Header("X-Swift-Ticket-ID", ticket_id))

    # send via SendGrid
    try:
        sg = _get_sendgrid_client()
        response = sg.send(message)
        logger.info(
            "Sent ticket reply for %s, status=%s", ticket_id, response.status_code
        )
    except Exception as e:
        logger.exception("Failed to send email for ticket %s: %s", ticket_id, e)
        raise

    # store outbound message in ticket
    now = datetime.now(tz=timezone.utc)
    outbound_msg = {
        "direction": "outbound",
        "body_text": body_text,
        "body_html": body_html,
        "sender_email": from_email,
        "message_id": outbound_message_id,
        "timestamp": now,
        "seen": True,
    }

    await db.email_tickets.update_one(
        {"id": ticket_id, "company_id": company_id},
        {
            "$push": {"messages": outbound_msg},
            "$set": {
                "status": "awaiting_customer",
                "updated_at": now,
            },
        },
    )

    return {"status": "sent", "message_id": outbound_message_id}


# ---------------------------------------------------------------------------
# Inbound — customer replies to an email (SendGrid webhook)
# ---------------------------------------------------------------------------

# Regex to extract ticket ID from subject like "Re: [Ticket #A1B2C3D4] ..."
_TICKET_ID_RE = re.compile(r"\[Ticket\s*#([A-Z0-9]{8})\]", re.IGNORECASE)


def _extract_ticket_id_from_subject(subject: str) -> str | None:
    """Extract ticket ID from email subject line."""
    match = _TICKET_ID_RE.search(subject)
    return match.group(1).upper() if match else None


def _extract_slug_from_recipient(to_email: str) -> str | None:
    """Extract slug from recipient like 'lambda@swfty.email'."""
    if "@" not in to_email:
        return None
    local_part = to_email.split("@")[0].strip().lower()
    return local_part if local_part else None


async def process_inbound_email(payload: dict) -> dict:
    """
    Process an inbound email from SendGrid Inbound Parse webhook.

    SendGrid sends multipart/form-data with fields:
    - from: sender email (customer)
    - to: recipient email (company@swfty.email)
    - subject: email subject
    - text: plain text body
    - html: HTML body
    - headers: raw email headers as text
    """
    sender_raw = payload.get("from", "")
    to_raw = payload.get("to", "")
    subject = payload.get("subject", "")
    body_text = payload.get("text", "")
    body_html = payload.get("html")

    # extract sender email from "Name <email@example.com>" format
    email_match = re.search(r"<([^>]+)>", sender_raw)
    sender_email = email_match.group(1) if email_match else sender_raw.strip()

    # extract slug from recipient
    # handle multiple recipients — find the one with our domain
    slug = None
    for addr in re.findall(r"[\w.\-+]+@[\w.\-]+", to_raw):
        if addr.endswith(f"@{settings.EMAIL_DOMAIN}"):
            slug = _extract_slug_from_recipient(addr)
            break

    if not slug:
        logger.warning("Inbound email with no matching slug. to=%s", to_raw)
        return {"status": "ignored", "reason": "no matching recipient"}

    # look up company by slug
    company = await company_service.get_company_by_slug(slug)
    if not company:
        logger.warning("Inbound email for unknown slug: %s", slug)
        return {"status": "ignored", "reason": "unknown company slug"}

    company_id = company["id"]

    # try to match to existing ticket
    ticket_id = _extract_ticket_id_from_subject(subject)

    # also try In-Reply-To header matching
    if not ticket_id:
        headers_raw = payload.get("headers", "")
        in_reply_to_match = re.search(r"In-Reply-To:\s*<([^>]+)>", headers_raw)
        if in_reply_to_match:
            ref_message_id = f"<{in_reply_to_match.group(1)}>"
            ticket = await db.email_tickets.find_one(
                {
                    "company_id": company_id,
                    "messages.message_id": ref_message_id,
                }
            )
            if ticket:
                ticket_id = ticket["id"]

    if not ticket_id:
        logger.warning(
            "Inbound email could not be matched to a ticket. subject=%s, from=%s",
            subject,
            sender_email,
        )
        return {"status": "ignored", "reason": "no matching ticket found"}

    # verify ticket exists
    ticket = await get_ticket(company_id, ticket_id)
    if not ticket:
        logger.warning("Ticket %s not found for company %s", ticket_id, company_id)
        return {"status": "ignored", "reason": "ticket not found"}

    # extract Message-ID from headers
    message_id = None
    headers_raw = payload.get("headers", "")
    msg_id_match = re.search(r"Message-ID:\s*(<[^>]+>)", headers_raw, re.IGNORECASE)
    if msg_id_match:
        message_id = msg_id_match.group(1)

    # store inbound message
    now = datetime.now(tz=timezone.utc)
    inbound_msg = {
        "direction": "inbound",
        "body_text": body_text,
        "body_html": body_html,
        "sender_email": sender_email,
        "message_id": message_id,
        "timestamp": now,
        "seen": False,
    }

    await db.email_tickets.update_one(
        {"id": ticket_id, "company_id": company_id},
        {
            "$push": {"messages": inbound_msg},
            "$set": {
                "status": "follow_up",
                "updated_at": now,
            },
            "$inc": {"unseen_count": 1},
        },
    )

    logger.info(
        "Stored inbound email on ticket %s from %s", ticket_id, sender_email
    )
    return {"status": "stored", "ticket_id": ticket_id}


# ---------------------------------------------------------------------------
# Ticket resolution
# ---------------------------------------------------------------------------


async def get_ticket_by_resolve_token(token: str) -> dict | None:
    """Look up a ticket by its resolve token."""
    return await db.email_tickets.find_one({"resolve_token": token})


async def resolve_ticket(token: str) -> dict | None:
    """Mark a ticket as resolved using the resolve token."""
    now = datetime.now(tz=timezone.utc)
    result = await db.email_tickets.find_one_and_update(
        {"resolve_token": token, "status": {"$ne": "resolved"}},
        {
            "$set": {
                "status": "resolved",
                "updated_at": now,
            }
        },
        return_document=1,
    )

    if result:
        logger.info("Ticket %s resolved via token", result["id"])
    return result
