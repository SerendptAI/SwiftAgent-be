import base64
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pymongo import ReturnDocument
import smtplib
from email.message import EmailMessage

from app.core.config import settings
from app.core.database import db
from app.core.utils import get_random_avatar
from app.services import company_service
from app.services.email_utils import get_image_data, process_html_for_inline_images, add_html_with_inline_images
from app.services import notification_service

logger = logging.getLogger(__name__)

def _send_smtp_email(msg: EmailMessage):
    """Helper to dispatch via native Zepto/Zoho SMTP connection."""
    try:
        with smtplib.SMTP_SSL(settings.active_smtp_server, settings.active_smtp_port) as smtp:
            smtp.login(settings.active_smtp_username, settings.active_smtp_password)
            smtp.send_message(msg)
    except Exception as e:
        logger.exception("Failed to send SMTP email: %s", e)
        raise

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "email_templates"
TICKET_REPLY_TEMPLATE = TEMPLATES_DIR / "response.html"
RESOLVED_TEMPLATE = TEMPLATES_DIR / "resolved.html"
NEW_MESSAGE_TEMPLATE = TEMPLATES_DIR / "new_message.html"
NEW_TICKET_TEMPLATE = TEMPLATES_DIR / "new_ticket.html"
TICKET_CONFIRMATION_TEMPLATE = TEMPLATES_DIR / "ticket_confirmation.html"

_TICKET_ID_RE = re.compile(r"\[Ticket\s*#([A-Z0-9]{8})\]", re.IGNORECASE)


def _load_template(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    return html.replace("{{base_url}}", settings.API_BASE_URL)


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

    avatar = get_random_avatar()
    if chat_session_id:
        chat_session = await db.widget_conversations.find_one(
            {"company_id": company_id, "session_id": chat_session_id}
        )
        if chat_session and "avatar" in chat_session:
            avatar = chat_session["avatar"]

    doc = {
        "id": ticket_id,
        "avatar": avatar,
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
    
    # Notify dashboard users about the new ticket
    import asyncio
    asyncio.create_task(
        notification_service.notify_company(
            company_id=company_id,
            title="🎫 New Ticket Opened",
            body=f"Ticket #{ticket_id} opened by {customer_email}: {subject}",
            type="ticket_open",
            data={"ticket_id": ticket_id}
        )
    )
    company = await company_service.get_company(company_id)
    if company:
        asyncio.create_task(_send_new_ticket_email(company, doc))
        asyncio.create_task(_send_ticket_confirmation_email(company, doc))

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
        logger.warning(
            "Ticket %s created with non-pending status: %s", ticket_id, created.get("status")
        )
        await db.email_tickets.update_one(
            {"id": ticket_id}, {"$set": {"status": "pending", "updated_at": now}}
        )
        created["status"] = "pending"

    return created or doc


async def get_ticket(company_id: str, ticket_id: str) -> dict | None:
    return await db.email_tickets.find_one({"company_id": company_id, "id": ticket_id})


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
            "$lookup": {
                "from": "widget_conversations",
                "localField": "chat_session_id",
                "foreignField": "session_id",
                "as": "attributed_chat_docs"
            }
        },
        {
            "$addFields": {
                "last_ticket_msg": {"$arrayElemAt": ["$messages", -1]},
                "attr_chat_doc": {"$arrayElemAt": ["$attributed_chat_docs", 0]}
            }
        },
        {
            "$project": {
                "_id": 0,
                "id": 1,
                "company_id": 1,
                "session_id": {"$ifNull": ["$customer_name", {"$ifNull": ["$customer_email", "$id"]}]},
                "customer_email": 1,
                "customer_name": 1,
                "subject": 1,
                "status": 1,
                "unseen_count": 1,
                "avatar": {"$ifNull": ["$avatar", "/chat-avatars/newimg.svg"]},
                "message_count": {"$size": {"$ifNull": ["$messages", []]}},
                "created_at": 1,
                "updated_at": 1,
                "type": {"$literal": "ticket"},
                "seen": {"$literal": False},
                "preview_message": {
                    "$cond": {
                        "if": {"$eq": ["$last_ticket_msg.direction", "system"]},
                        "then": {
                            "$let": {
                                "vars": {
                                    "user_msgs": {
                                        "$filter": {
                                            "input": {"$ifNull": ["$attr_chat_doc.messages", []]},
                                            "as": "msg",
                                            "cond": {"$eq": ["$$msg.role", "user"]}
                                        }
                                    }
                                },
                                "in": {
                                    "$ifNull": [
                                        {"$arrayElemAt": ["$$user_msgs.content", -1]},
                                        "$last_ticket_msg.body_text"
                                    ]
                                }
                            }
                        },
                        "else": "$last_ticket_msg.body_text"
                    }
                }
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
    return result.matched_count > 0


def _build_reply_html(
    response_message: str,
    agent_name: str,
    agent_avatar: str,
    resolve_url: str,
    company_logo_url: str,
    company_name: str,
) -> str:
    html = _load_template(TICKET_REPLY_TEMPLATE)
    html = html.replace("{{agent_name}}", agent_name)
    html = html.replace("{{company_name}}", company_name)
    html = html.replace("{{agent_image_url}}", agent_avatar)
    html = html.replace("{{resolve_url}}", resolve_url)
    html = html.replace("{{company_logo_url}}", company_logo_url)
    html = html.replace("{{message}}", response_message)
    return html


async def send_ticket_reply(
    company_id: str,
    ticket_id: str,
    body_text: str,
    body_html: str | None = None,
    attachments: list[dict] | None = None,
    agent_name: str | None = None,
    agent_avatar_url: str | None = None,
) -> dict:
    """Send a reply from the company to the customer via SendGrid.

    attachments: list of dicts with keys: filename, content_type, content (bytes)
    """
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

    # Agent identity for the email header. Fall back to the company name so we
    # never ship an empty name, and the team line reads as a support role.
    agent_display = (agent_name or "").strip() or company_name
    support_team = "Support Team"

    # Resolve the agent's avatar to an absolute URL the email client can load.
    avatar_path = agent_avatar_url or get_random_avatar()
    agent_avatar = (
        f"{settings.API_BASE_URL}{avatar_path}"
        if avatar_path.startswith("/")
        else avatar_path
    )

    # Company logo (next to the agent name).
    logo_url = company.get("logo_url") or f"{settings.API_BASE_URL}/images/logo 2.png"

    html_body = body_html or f"<p>{body_text}</p>"
    full_html = _build_reply_html(
        html_body, agent_display, agent_avatar, resolve_url, logo_url, company_name
    )

    # Process base64 inline images from rich text editors
    base64_attachments = []

    def _replace_base64(match):
        prefix = match.group(1)
        quote = match.group(2)
        mime_type = match.group(3)
        b64_data = match.group(4)

        cid = str(uuid4())
        ext = mime_type.split("/")[-1] if "/" in mime_type else "png"
        filename = f"image_{cid[:8]}.{ext}"

        base64_attachments.append(
            {"filename": filename, "cid": cid, "mime_type": mime_type, "data": b64_data}
        )
        return f"{prefix}{quote}cid:{cid}{quote}"

    base64_img_re = re.compile(
        r'(<img\b[^>]*\bsrc=)(["\'])data:(image/[a-zA-Z0-9+-]+);base64,([^"\']+)\2', re.IGNORECASE
    )
    full_html = base64_img_re.sub(_replace_base64, full_html)

    subject = f"Re: [Ticket #{ticket_id}] {ticket['subject']}"

    last_message_id = None
    for m in reversed(ticket.get("messages", [])):
        if m.get("message_id"):
            last_message_id = m["message_id"]
            break

    outbound_message_id = f"<{uuid4()}@{settings.EMAIL_DOMAIN}>"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{company_name} <{settings.active_sender_email}>"
    msg["To"] = ticket["customer_email"]
    msg["Reply-To"] = from_email
    msg["Message-ID"] = outbound_message_id
    if last_message_id:
        msg["In-Reply-To"] = last_message_id
        msg["References"] = last_message_id
    msg["X-Swift-Ticket-ID"] = ticket_id

    msg.set_content(body_text)
    add_html_with_inline_images(msg, full_html)

    # Now add the base64 inline images to the html_part
    html_part = msg.get_payload()[1]
    for att in base64_attachments:
        try:
            data = base64.b64decode(att["data"])
            maintype, subtype = att["mime_type"].split("/", 1)
            html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{att['cid']}>")
            image_part = html_part.get_payload()[-1]
            # Replace Content-Disposition to be strictly inline without a filename
            # to prevent email clients from rendering attachment pills at the bottom
            del image_part["Content-Disposition"]
            image_part["Content-Disposition"] = "inline"
            image_part["X-Attachment-Id"] = att["cid"]
        except Exception as e:
            logger.warning(f"Could not attach base64 image {att['filename']} to ticket reply: {e}")

    # Attach user-uploaded files as regular (non-inline) attachments
    attachment_meta = []
    if attachments:
        for att in attachments:
            try:
                maintype, subtype = att["content_type"].split("/", 1) if "/" in att["content_type"] else ("application", "octet-stream")
                msg.add_attachment(
                    att["content"],
                    maintype=maintype,
                    subtype=subtype,
                    filename=att["filename"]
                )
                attachment_meta.append({
                    "filename": att["filename"],
                    "content_type": att["content_type"],
                    "size": len(att["content"]),
                })
            except Exception as e:
                logger.warning("Could not attach file %s: %s", att["filename"], e)

    try:
        import asyncio
        await asyncio.to_thread(_send_smtp_email, msg)
        logger.info("Sent ticket reply for %s", ticket_id)
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
        "attachments": attachment_meta,
        "agent_name": agent_display,
        "agent_avatar_url": agent_avatar,
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

# Patterns that mark the beginning of quoted / forwarded content or signatures
_QUOTE_SEPARATORS = [
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^On\s+.+wrote:\s*$", re.MULTILINE),
    re.compile(r"^From:\s+.+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^--\s*$", re.MULTILINE),  # standard signature separator
    re.compile(r"^_{2,}\s*$", re.MULTILINE),  # alternative separator
    re.compile(
        r"^Sent from (my|Apple|Yahoo|Mail).*", re.IGNORECASE | re.MULTILINE
    ),  # mobile signature
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
    for pattern in _QUOTE_SEPARATORS[:-1]:  # skip the ">" pattern for now
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

    logger.info("Stored inbound email on ticket %s from %s", ticket_id, sender_email)
    
    # Notify dashboard users about the reply
    import asyncio
    asyncio.create_task(
        notification_service.notify_company(
            company_id=company_id,
            title="💬 New Ticket Reply",
            body=f"Customer {sender_email} replied to Ticket #{ticket_id}",
            type="ticket_reply",
            data={"ticket_id": ticket_id}
        )
    )
    asyncio.create_task(_send_new_message_email(company, ticket, inbound_msg))
    
    return {"status": "stored", "ticket_id": ticket_id}


async def _send_new_message_email(company: dict, ticket: dict, inbound_msg: dict):
    try:
        html = _load_template(NEW_MESSAGE_TEMPLATE)
        preview_message = inbound_msg.get("body_text", "")
        if len(preview_message) > 40:
            preview_message = preview_message[:40] + "..."
        
        customer_email = ticket.get("customer_email", inbound_msg.get("sender_email", ""))
        dashboard_url = f"{settings.FRONTEND_URL}/dashboard/tickets/{ticket['id']}"

        html = html.replace("{{preview_message}}", preview_message)
        html = html.replace("{{customer_email}}", customer_email)
        html = html.replace("{{dashboard_url}}", dashboard_url)
        
        subject = f"New message from {customer_email} - Ticket #{ticket['id']}"
        company_name = "SwiftAgent"

        # Send to all company members
        user_ids = [company.get("user_id")]
        for member in company.get("members", []):
            if member.get("user_id"):
                user_ids.append(member["user_id"])
        
        user_ids = list(set([uid for uid in user_ids if uid]))
        if not user_ids:
            return

        cursor = db.users.find({"user_id": {"$in": user_ids}})
        to_emails = []
        async for u in cursor:
            if u.get("email"):
                to_emails.append(u["email"])

        if not to_emails:
            return

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{company_name} <{settings.active_sender_email}>"
        msg["To"] = ", ".join(to_emails)

        msg.set_content(f"New message from {customer_email}: {preview_message}")
        add_html_with_inline_images(msg, html)

        import asyncio
        await asyncio.to_thread(_send_smtp_email, msg)
        logger.info("Sent new message email for ticket %s to %s agents", ticket['id'], len(to_emails))
    except Exception as e:
        logger.exception("Failed to send new message email for ticket %s: %s", ticket['id'], e)


async def _send_new_ticket_email(company: dict, ticket: dict):
    try:
        html = _load_template(NEW_TICKET_TEMPLATE)
        customer_email = ticket.get("customer_email", "")
        dashboard_url = f"{settings.FRONTEND_URL}/dashboard/tickets/{ticket['id']}"

        html = html.replace("{{customer_email}}", customer_email)
        html = html.replace("{{dashboard_url}}", dashboard_url)
        
        subject = f"New Ticket #{ticket['id']} from {customer_email}"
        company_name = "SwiftAgent"

        # Send to all company members
        user_ids = [company.get("user_id")]
        for member in company.get("members", []):
            if member.get("user_id"):
                user_ids.append(member["user_id"])
        
        user_ids = list(set([uid for uid in user_ids if uid]))
        if not user_ids:
            return

        cursor = db.users.find({"user_id": {"$in": user_ids}})
        to_emails = []
        async for u in cursor:
            if u.get("email"):
                to_emails.append(u["email"])

        if not to_emails:
            return

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{company_name} <{settings.active_sender_email}>"
        msg["To"] = ", ".join(to_emails)

        msg.set_content(f"New ticket #{ticket['id']} opened by {customer_email}. View it on your dashboard.")
        add_html_with_inline_images(msg, html)

        import asyncio
        await asyncio.to_thread(_send_smtp_email, msg)
        logger.info("Sent new ticket email for ticket %s to %s agents", ticket['id'], len(to_emails))
    except Exception as e:
        logger.exception("Failed to send new ticket email for ticket %s: %s", ticket['id'], e)


async def _send_ticket_confirmation_email(company: dict, ticket: dict):
    try:
        html = _load_template(TICKET_CONFIRMATION_TEMPLATE)
        company_name = company.get("name", "Support")
        email_slug = company.get("email_slug")
        if not email_slug:
            logger.warning("Company %s has no email slug, skipping ticket confirmation email", company["id"])
            return

        from_email = f"{email_slug}@{settings.EMAIL_DOMAIN}"
        to_email = ticket["customer_email"]
        
        logo_url = company.get("logo_url") or f"{settings.API_BASE_URL}/images/logo 2.png"
        
        html = html.replace("{{company_logo_url}}", logo_url)
        html = html.replace("{{company_name}}", company_name)
        html = html.replace("{{base_url}}", settings.API_BASE_URL)
        html = html.replace("{{ticket_number}}", ticket["id"])

        subject = f"Your ticket has been received - #{ticket['id']}"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{company_name} <{settings.active_sender_email}>"
        msg["To"] = to_email
        msg["Reply-To"] = from_email

        msg.set_content(f"Your ticket #{ticket['id']} has been received. We will get back to you shortly.")
        add_html_with_inline_images(msg, html)

        import asyncio
        await asyncio.to_thread(_send_smtp_email, msg)
        logger.info("Sent ticket confirmation email for ticket %s to %s", ticket['id'], to_email)
    except Exception as e:
        logger.exception("Failed to send ticket confirmation email for ticket %s: %s", ticket['id'], e)


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
        # Notify dashboard users about the ticket being closed
        import asyncio
        asyncio.create_task(
            notification_service.notify_company(
                company_id=result["company_id"],
                title="✅ Ticket Closed",
                body=f"Ticket #{result['id']} was marked as resolved.",
                type="ticket_close",
                data={"ticket_id": result["id"]}
            )
        )
    return result


async def get_ticket_with_chat(company_id: str, ticket_id: str) -> dict | None:
    """Fetch a ticket and its attributed chat session (if escalated from a chat)."""
    ticket = await db.email_tickets.find_one(
        {"company_id": company_id, "id": ticket_id}, {"_id": 0}
    )
    if not ticket:
        return None

    # If ticket came from a chat session, fetch and attach it
    chat_session = None
    if ticket.get("chat_session_id"):
        chat_session = await db.widget_conversations.find_one(
            {"company_id": company_id, "session_id": ticket["chat_session_id"]}, {"_id": 0}
        )

    return {
        "ticket": ticket,
        "attributed_chat": chat_session or None,
    }


async def _send_resolved_email(ticket: dict):
    try:
        company = await company_service.get_company(ticket["company_id"])
        if not company:
            logger.warning("Company not found for ticket %s, skipping resolved email", ticket["id"])
            return

        company_name = company.get("name", "Support")
        email_slug = company.get("email_slug")
        if not email_slug:
            logger.warning("Company %s has no email slug, skipping resolved email", company["id"])
            return

        from_email = f"{email_slug}@{settings.EMAIL_DOMAIN}"
        to_email = ticket["customer_email"]
        
        html = _load_template(RESOLVED_TEMPLATE)
        logo_url = company.get("logo_url") or f"{settings.API_BASE_URL}/images/logo 2.png"
        
        html = html.replace("{{company_logo_url}}", logo_url)
        html = html.replace("{{company_name}}", company_name)
        
        subject = f"Re: [Ticket #{ticket['id']}] {ticket['subject']}"
        outbound_message_id = f"<{uuid4()}@{settings.EMAIL_DOMAIN}>"

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{company_name} <{settings.active_sender_email}>"
        msg["To"] = to_email
        msg["Reply-To"] = from_email
        msg["Message-ID"] = outbound_message_id
        msg["X-Swift-Ticket-ID"] = ticket["id"]

        msg.set_content("Your ticket has been closed. Thank you!")
        add_html_with_inline_images(msg, html)

        import asyncio
        await asyncio.to_thread(_send_smtp_email, msg)
        logger.info("Sent resolved email for ticket %s", ticket["id"])

        # Record message in ticket
        now = datetime.now(tz=timezone.utc)
        outbound_msg = {
            "direction": "outbound",
            "body_text": "Your ticket has been closed. Thank you!",
            "body_html": html,
            "sender_email": from_email,
            "message_id": outbound_message_id,
            "timestamp": now,
            "seen": True,
        }
        await db.email_tickets.update_one(
            {"id": ticket["id"], "company_id": ticket["company_id"]},
            {"$push": {"messages": outbound_msg}},
        )

    except Exception as e:
        logger.exception("Failed to send resolved email for ticket %s: %s", ticket["id"], e)


async def resolve_ticket_by_agent(company_id: str, ticket_id: str) -> dict | None:
    now = datetime.now(tz=timezone.utc)
    ticket = await db.email_tickets.find_one_and_update(
        {"company_id": company_id, "id": ticket_id, "status": {"$ne": "resolved"}},
        {
            "$set": {
                "status": "resolved",
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if ticket:
        logger.info("Ticket %s resolved by agent", ticket["id"])
        
        # Notify dashboard users about the ticket being closed
        import asyncio
        asyncio.create_task(
            notification_service.notify_company(
                company_id=company_id,
                title="✅ Ticket Closed",
                body=f"Ticket #{ticket['id']} was marked as resolved by an agent.",
                type="ticket_close",
                data={"ticket_id": ticket["id"]}
            )
        )

        # Trigger customer email asynchronously
        asyncio.create_task(_send_resolved_email(ticket))

    return ticket

async def reopen_ticket(company_id: str, ticket_id: str) -> dict | None:
    now = datetime.now(tz=timezone.utc)
    ticket = await db.email_tickets.find_one_and_update(
        {"company_id": company_id, "id": ticket_id, "status": "resolved"},
        {
            "$set": {
                "status": "open",
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if ticket:
        logger.info("Ticket %s reopened", ticket["id"])
        import asyncio
        asyncio.create_task(
            notification_service.notify_company(
                company_id=company_id,
                title="🔄 Ticket Reopened",
                body=f"Ticket #{ticket['id']} was reopened.",
                type="ticket_reopen",
                data={"ticket_id": ticket["id"]}
            )
        )
    return ticket

async def dispatch_all_test_templates(company_id: str, recipients: list[str], auth_user: dict) -> tuple[bool, str]:
    company = await db.companies.find_one({"id": company_id})
    if not company:
        return False, "Company not found"
        
    company_name = company.get("name", "SwiftAgent")
    company_email = company.get("contact_email") or auth_user.get("email")
    email_slug = company.get("email_slug", "support")
    company_logo_url = company.get("logo_url") or f"{settings.API_BASE_URL}/images/logo 2.png"
    
    agent_name = auth_user.get("name", "Support Agent")
    agent_avatar = auth_user.get("picture") or f"{settings.API_BASE_URL}/images/default_avatar.png"
    
    from_email = f"{email_slug}@{settings.EMAIL_DOMAIN}"
    
    template_tasks = [
        {
            "file": "welcome.html",
            "subject": "Welcome to SwiftAgent",
            "replacements": {
                "{{company_name}}": company_name,
                "{{login_url}}": f"{settings.FRONTEND_URL}/login"
            }
        },
        {
            "file": "team_member_invite.html",
            "subject": "You have been invited to join the team",
            "replacements": {
                "{{company_name}}": company_name,
                "{{invite_url}}": f"{settings.FRONTEND_URL}/invite/test",
                "{{company_logo_url}}": company_logo_url
            }
        },
        {
            "file": "otp_email.html",
            "subject": "Your verification code",
            "replacements": {
                "{{ttl_minutes}}": "10",
                "{{purpose_label}}": "Login",
                "{{app_name}}": "Swift Agent",
                "{{otp_code}}": "123456",
                "{{d0}}": "1", "{{d1}}": "2", "{{d2}}": "3", "{{d3}}": "4", "{{d4}}": "5", "{{d5}}": "6"
            },
            "from_email": "noreply@swiftagents.org",
            "from_name": "SwiftAgent"
        },
        {
            "file": "response.html",
            "subject": f"New message from {agent_name} - Ticket #TEST1234",
            "replacements": {
                "{{agent_name}}": agent_name,
                "{{company_name}}": company_name,
                "{{agent_image_url}}": agent_avatar,
                "{{resolve_url}}": f"{settings.API_BASE_URL}/api/v1/email/resolve/test-token",
                "{{company_logo_url}}": company_logo_url,
                "{{message}}": "This is a simulated test response from your support agent. We hope this solves your issue!"
            }
        },
        {
            "file": "resolved.html",
            "subject": f"Ticket #TEST1234 Resolved",
            "replacements": {
                "{{company_name}}": company_name,
                "{{company_logo_url}}": company_logo_url
            }
        },
        {
            "file": "new_ticket.html",
            "subject": f"New Ticket #TEST1234 from {recipients[0]}",
            "replacements": {
                "{{customer_email}}": recipients[0],
                "{{dashboard_url}}": f"{settings.FRONTEND_URL}/dashboard/tickets/TEST1234"
            },
            "from_email": "noreply@swiftagents.org",
            "from_name": "SwiftAgent"
        },
        {
            "file": "new_message.html",
            "subject": f"New message from {recipients[0]} - Ticket #TEST1234",
            "replacements": {
                "{{customer_email}}": recipients[0],
                "{{dashboard_url}}": f"{settings.FRONTEND_URL}/dashboard/tickets/TEST1234",
                "{{preview_message}}": "Please help, I have an issue with..."
            },
            "from_email": "noreply@swiftagents.org",
            "from_name": "SwiftAgent"
        },
        {
            "file": "approve-company.html",
            "subject": "New Company Registration",
            "replacements": {
                "{{company_name}}": company_name,
                "{{company_email}}": company_email,
                "{{customer_size}}": "1-10",
                "{{company_description}}": "A simulated test company for QA purposes.",
                "{{approval_url}}": f"{settings.API_BASE_URL}/api/v1/auth/registrations/approve/test-token"
            },
            "from_email": "noreply@swiftagents.org",
            "from_name": "SwiftAgent"
        },
        {
            "file": "discount.html",
            "subject": "You have a discount!",
            "replacements": {
                "{{discount_code}}": "LAUNCHDISC"
            }
        },
        {
            "file": "ticket_confirmation.html",
            "subject": "Your ticket has been received - #TEST1234",
            "replacements": {
                "{{company_logo_url}}": company_logo_url,
                "{{company_name}}": company_name,
                "{{ticket_number}}": "TEST1234"
            }
        }
    ]
    
    success_count = 0
    errors = []

    def _dispatch_sync():
        nonlocal success_count
        import smtplib
        import time
        try:
            with smtplib.SMTP_SSL(settings.active_smtp_server, settings.active_smtp_port) as smtp:
                smtp.login(settings.active_smtp_username, settings.active_smtp_password)

                for task in template_tasks:
                    try:
                        template_path = TEMPLATES_DIR / task["file"]
                        with open(template_path, "r", encoding="utf-8") as f:
                            html = f.read()

                        html = html.replace("{{base_url}}", settings.API_BASE_URL)
                        for k, v in task["replacements"].items():
                            html = html.replace(k, str(v))

                        sender_email = task.get("from_email", from_email)
                        sender_name = task.get("from_name", company_name)

                        msg = EmailMessage()
                        msg["Subject"] = task["subject"]
                        msg["From"] = f"{sender_name} <{settings.active_sender_email}>"
                        msg["To"] = ", ".join(recipients)
                        msg["Reply-To"] = sender_email

                        msg.set_content("Please view this email in an HTML-compatible client.")
                        add_html_with_inline_images(msg, html)

                        smtp.send_message(msg)
                        success_count += 1
                        time.sleep(1.5) # Prevent ZeptoMail rate-limit blocking for rapid bursts

                    except Exception as e:
                        errors.append(f"{task['file']}: {e}")

        except Exception as e:
            logger.exception("Failed to connect or authenticate to SMTP server during test dispatch: %s", e)
            errors.append(f"SMTP Connection Failed: {e}")

    import asyncio
    await asyncio.to_thread(_dispatch_sync)

    if errors:
        return False, f"Sent {success_count}/10. Errors: {'; '.join(errors)}"
    return True, f"Successfully dispatched all 10 templates to {len(recipients)} recipients!"
