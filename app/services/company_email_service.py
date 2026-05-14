import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pymongo import ReturnDocument
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import (
    Mail,
    From,
    To,
    Subject,
    Content,
    Header,
    MimeType,
    Attachment,
    FileContent,
    FileName,
    FileType,
    Disposition,
    ContentId,
)
import base64

from app.core.config import settings
from app.core.database import db
from app.services import company_service
from app.services.email_utils import process_html_for_inline_images, get_image_data

logger = logging.getLogger(__name__)

_sg_client = None

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "email_templates"
TICKET_REPLY_TEMPLATE = TEMPLATES_DIR / "ticket_reply.html"

_TICKET_ID_RE = re.compile(r"\[Ticket\s*#([A-Z0-9]{8})\]", re.IGNORECASE)


def _get_sendgrid_client() -> SendGridAPIClient:
    global _sg_client
    if _sg_client is None:
        _sg_client = SendGridAPIClient(api_key=settings.SENDGRID_API_KEY)
    return _sg_client


def _load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def create_ticket(
    company_id: str,
    customer_email: str,
    subject: str,
    chat_summary: str,
    chat_session_id: str | None = None,
    customer_name: str | None = None,
) -> dict:
    """Create a new support ticket (called by the AI agent)."""
    # ensure ticket id is unique (avoid rare collisions)
    ticket_id = str(uuid4())[:8].upper()
    while await db.email_tickets.find_one({"id": ticket_id}):
        ticket_id = str(uuid4())[:8].upper()
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

    # Mark the originating chat as escalated so it's excluded from the resolved list
    if chat_session_id:
        await db.widget_conversations.update_one(
            {"company_id": company_id, "session_id": chat_session_id},
            {"$set": {"escalated": True, "ticket_id": ticket_id}},
        )

    # Return a clean projection (exclude MongoDB internal _id)
    created = await db.email_tickets.find_one({"id": ticket_id}, {"_id": 0})
    # Defensive: ensure status remains pending
    if created and created.get("status") != "pending":
        logger.warning("Ticket %s created with non-pending status: %s", ticket_id, created.get("status"))
        await db.email_tickets.update_one({"id": ticket_id}, {"$set": {"status": "pending", "updated_at": now}})
        created["status"] = "pending"

    return created or doc


async def get_ticket(company_id: str, ticket_id: str) -> dict | None:
    return await db.email_tickets.find_one(
        {"company_id": company_id, "id": ticket_id}
    )


async def list_tickets(
    company_id: str,
    limit: int = 50,
    skip: int = 0,
) -> list:
    """List only unresolved tickets (pending section)."""
    query: dict = {"company_id": company_id, "status": {"$ne": "resolved"}}

    pipeline = [
        {"$match": query},
        {"$sort": {"updated_at": -1}},
        {"$skip": skip},
        {"$limit": limit},
        {
            "$project": {
                "_id": 0,
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


async def count_tickets(company_id: str) -> int:
    """Count only unresolved tickets."""
    query: dict = {"company_id": company_id, "status": {"$ne": "resolved"}}
    return await db.email_tickets.count_documents(query)


async def mark_ticket_seen(company_id: str, ticket_id: str) -> bool:
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


def _build_reply_html(
    body_html: str,
    company_name: str,
    resolve_url: str,
    logo_url: str | None = None,
) -> str:
    logo_block = ""
    if logo_url:
        logo_block = (
            f'<img src="{logo_url}" alt="{company_name}" '
            f'style="max-height:40px;margin-bottom:16px;" />'
        )

    html = _load_template(TICKET_REPLY_TEMPLATE)
    html = html.replace("{{logo_block}}", logo_block)
    html = html.replace("{{body_html}}", body_html)
    html = html.replace("{{resolve_url}}", resolve_url)
    html = html.replace("{{company_name}}", company_name)
    return html


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

    html_body = body_html or f"<p>{body_text}</p>"
    full_html = _build_reply_html(html_body, company_name, resolve_url, logo_url)
    
    full_html, attachments_map = process_html_for_inline_images(full_html)

    subject = f"Re: [Ticket #{ticket_id}] {ticket['subject']}"

    last_message_id = None
    for msg in reversed(ticket.get("messages", [])):
        if msg.get("message_id"):
            last_message_id = msg["message_id"]
            break

    outbound_message_id = f"<{uuid4()}@{settings.EMAIL_DOMAIN}>"

    message = Mail(
        from_email=From(from_email, company_name),
        to_emails=To(ticket["customer_email"]),
        subject=Subject(subject),
    )
    message.add_content(Content(MimeType.text, body_text))
    message.add_content(Content(MimeType.html, full_html))

    for filename, cid in attachments_map.items():
        try:
            data, maintype, subtype = get_image_data(filename)
            encoded = base64.b64encode(data).decode('utf-8')
            sg_attachment = Attachment(
                FileContent(encoded),
                FileName(filename),
                FileType(f"{maintype}/{subtype}"),
                Disposition("inline"),
                ContentId(cid)
            )
            message.add_attachment(sg_attachment)
        except Exception as e:
            logger.warning(f"Could not attach image {filename} to ticket reply: {e}")

    message.add_header(Header("Message-ID", outbound_message_id))
    if last_message_id:
        message.add_header(Header("In-Reply-To", last_message_id))
        message.add_header(Header("References", last_message_id))
    message.add_header(Header("X-Swift-Ticket-ID", ticket_id))

    try:
        sg = _get_sendgrid_client()
        response = sg.send(message)
        logger.info(
            "Sent ticket reply for %s, status=%s", ticket_id, response.status_code
        )
    except Exception as e:
        logger.exception("Failed to send email for ticket %s: %s", ticket_id, e)
        raise

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


def _extract_ticket_id_from_subject(subject: str) -> str | None:
    match = _TICKET_ID_RE.search(subject)
    return match.group(1).upper() if match else None


def _extract_slug_from_recipient(to_email: str) -> str | None:
    if "@" not in to_email:
        return None
    local_part = to_email.split("@")[0].strip().lower()
    return local_part if local_part else None


# ── Quoted-reply stripping ────────────────────────────────────────────

# Patterns that mark the beginning of quoted / forwarded content
_QUOTE_SEPARATORS = [
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^On\s+.+wrote:\s*$", re.MULTILINE),
    re.compile(r"^From:\s+.+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^>{1,}\s*", re.MULTILINE),  # standard "> " quote prefix lines
]


def _strip_quoted_reply(text: str) -> str:
    """Extract only the new reply content, stripping quoted thread text.

    Handles Gmail ("On … wrote:"), Outlook ("-----Original Message-----"),
    and standard "> " quote-prefix lines.  Returns the stripped text, or the
    original if nothing could be detected.
    """
    if not text:
        return text

    # 1. Try splitting on well-known separator lines first (most reliable)
    for pattern in _QUOTE_SEPARATORS[:3]:  # skip the ">" pattern for now
        match = pattern.search(text)
        if match:
            new_reply = text[: match.start()].rstrip()
            if new_reply:
                return new_reply

    # 2. Fall back to stripping lines that start with ">"
    lines = text.splitlines()
    new_lines: list[str] = []
    hit_quote_block = False
    for line in lines:
        if line.lstrip().startswith(">"):
            hit_quote_block = True
            continue
        if hit_quote_block:
            # Once we enter a quote block, skip everything after it
            continue
        new_lines.append(line)

    stripped = "\n".join(new_lines).rstrip()
    return stripped if stripped else text


def _strip_quoted_html(html: str | None) -> str | None:
    """Remove <blockquote> elements and Gmail/Outlook quote wrappers from HTML."""
    if not html:
        return html

    # Remove <blockquote ...>...</blockquote> (greedy across newlines)
    cleaned = re.sub(
        r"<blockquote[^>]*>.*?</blockquote>",
        "",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Remove Gmail quote wrapper: <div class="gmail_quote">...</div>
    cleaned = re.sub(
        r'<div\s+class="gmail_quote"[^>]*>.*?</div>',
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Remove Outlook-style <div id="appendonsend">...</div>
    cleaned = re.sub(
        r'<div\s+id="appendonsend"[^>]*>.*?</div>',
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Remove Mozilla cite prefix
    cleaned = re.sub(
        r'<div\s+class="moz-cite-prefix"[^>]*>.*?</div>',
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    return cleaned.strip() or html


async def process_inbound_email(payload: dict) -> dict:
    """Process an inbound email from SendGrid Inbound Parse webhook."""
    sender_raw = payload.get("from", "")
    to_raw = payload.get("to", "")
    subject = payload.get("subject", "")
    body_text_raw = payload.get("text", "")
    body_html_raw = payload.get("html")

    email_match = re.search(r"<([^>]+)>", sender_raw)
    sender_email = email_match.group(1) if email_match else sender_raw.strip()

    slug = None
    for addr in re.findall(r"[\w.\-+]+@[\w.\-]+", to_raw):
        if addr.endswith(f"@{settings.EMAIL_DOMAIN}"):
            slug = _extract_slug_from_recipient(addr)
            break

    if not slug:
        logger.warning("Inbound email with no matching slug. to=%s", to_raw)
        return {"status": "ignored", "reason": "no matching recipient"}

    company = await company_service.get_company_by_slug(slug)
    if not company:
        logger.warning("Inbound email for unknown slug: %s", slug)
        return {"status": "ignored", "reason": "unknown company slug"}

    company_id = company["id"]

    ticket_id = _extract_ticket_id_from_subject(subject)

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

    ticket = await get_ticket(company_id, ticket_id)
    if not ticket:
        logger.warning("Ticket %s not found for company %s", ticket_id, company_id)
        return {"status": "ignored", "reason": "ticket not found"}

    message_id = None
    headers_raw = payload.get("headers", "")
    msg_id_match = re.search(r"Message-ID:\s*(<[^>]+>)", headers_raw, re.IGNORECASE)
    if msg_id_match:
        message_id = msg_id_match.group(1)

    # Strip quoted thread text — keep only the customer's new reply
    body_text = _strip_quoted_reply(body_text_raw)
    body_html = _strip_quoted_html(body_html_raw)

    now = datetime.now(tz=timezone.utc)
    inbound_msg = {
        "direction": "inbound",
        "body_text": body_text,
        "body_html": body_html,
        "body_text_full": body_text_raw,  # preserve original for debugging
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


async def get_ticket_by_resolve_token(token: str) -> dict | None:
    return await db.email_tickets.find_one({"resolve_token": token})


async def resolve_ticket(token: str) -> dict | None:
    now = datetime.now(tz=timezone.utc)
    result = await db.email_tickets.find_one_and_update(
        {"resolve_token": token, "status": {"$ne": "resolved"}},
        {
            "$set": {
                "status": "resolved",
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if result:
        logger.info("Ticket %s resolved via token", result["id"])
    return result


async def get_ticket_with_chat(company_id: str, ticket_id: str) -> dict | None:
    """Fetch a ticket and its attributed chat session (if escalated from a chat)."""
    ticket = await db.email_tickets.find_one(
        {"company_id": company_id, "id": ticket_id},
        {"_id": 0}
    )
    if not ticket:
        return None
    
    # If ticket came from a chat session, fetch and attach it
    chat_session = None
    if ticket.get("chat_session_id"):
        chat_session = await db.widget_conversations.find_one(
            {"company_id": company_id, "session_id": ticket["chat_session_id"]},
            {"_id": 0}
        )
    
    return {
        "ticket": ticket,
        "attributed_chat": chat_session or None,
    }
