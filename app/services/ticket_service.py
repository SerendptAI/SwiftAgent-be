"""
Ticket Workflow Service — state machine, assignment, priority, SLA tracking,
and auto-escalation for email tickets.

Keeps all ticket mutations centralized so the dashboard endpoints, the
background scheduler, and future integrations share one audited code path.
Every mutation appends an entry to the ticket's ``activity_log`` and fires
the appropriate company notifications.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from app.core.database import db
from app.services import (
    company_email_service,
    company_service,
    handoff_service,
    notification_service,
)

logger = logging.getLogger(__name__)

# Allowed ticket priorities
PRIORITIES = ("low", "medium", "high", "urgent")

# Allowed ticket statuses (superset of the legacy statuses)
STATUSES = ("pending", "in_progress", "awaiting_customer", "follow_up", "open", "resolved")

# Reopen window (matches the legacy reopen rule in company_email_service)
REOPEN_WINDOW_HOURS = 48

# Fallback text the graph executor yields when the AI produced no answer.
# See app/services/graph/executor.py.
FALLBACK_TEXT = "I'm sorry, I wasn't able to find that information right now."

# Idle threshold for auto-escalation: last user message unanswered for >= N minutes.
IDLE_ESCALATION_MINUTES = 10

# Minimum number of user messages before the idle rule applies.
MIN_USER_MESSAGES_FOR_IDLE = 2

# A transition map from current status -> allowed target statuses.
#
# Matches the workflow spec:
#   - pending <-> in_progress
#   - pending / awaiting_customer / follow_up -> resolved
#   - resolved -> open (within 48h, mirroring the legacy reopen rule)
# Two pragmatic additions keep the machine complete: in_progress -> resolved
# (agents resolve directly from work) and open -> resolved (reopened tickets
# must be closable through the same API).
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"in_progress", "resolved"},
    "in_progress": {"pending", "resolved"},
    "awaiting_customer": {"resolved"},
    "follow_up": {"resolved"},
    "open": {"resolved"},
    "resolved": {"open"},
}

_EMAIL_RE = re.compile(r"[\w\.-]+@[\w\.-]+\.\w+")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _as_utc_datetime(value) -> datetime | None:
    """Coerce a stored timestamp (str or datetime) into a tz-aware datetime."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    return None


def _activity_entry(action: str, actor: str, **extra) -> dict:
    entry = {
        "action": action,
        "actor": actor,
        "timestamp": _utcnow(),
    }
    entry.update(extra)
    return entry


async def _fetch_ticket(company_id: str, ticket_id: str) -> dict:
    ticket = await db.email_tickets.find_one({"company_id": company_id, "id": ticket_id})
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found")
    return ticket


def _fire_company_notification(
    company_id: str,
    title: str,
    body: str,
    type: str,
    data: dict | None = None,
):
    asyncio.create_task(
        notification_service.notify_company(
            company_id=company_id,
            title=title,
            body=body,
            type=type,
            data=data,
        )
    )


async def transition_ticket(
    company_id: str,
    ticket_id: str,
    to_status: str,
    actor: str,
    note: str | None = None,
) -> dict | None:
    """Move a ticket through the allowed state machine, with audit + notifications."""
    if to_status not in STATUSES:
        raise ValueError(f"Status must be one of {STATUSES}")

    ticket = await _fetch_ticket(company_id, ticket_id)
    from_status = ticket.get("status", "pending")

    allowed = _VALID_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise ValueError(f"Cannot transition ticket from '{from_status}' to '{to_status}'")

    now = _utcnow()

    # Enforce the 48h reopen window (same rule as the legacy reopen endpoint).
    if from_status == "resolved" and to_status == "open":
        updated_at = _as_utc_datetime(ticket.get("updated_at"))
        if updated_at and updated_at < now - timedelta(hours=REOPEN_WINDOW_HOURS):
            raise ValueError(
                "Cannot reopen a ticket that has been closed for more than 48 hours."
            )

    entry = _activity_entry(
        "status_change",
        actor,
        from_status=from_status,
        to_status=to_status,
        note=note,
    )

    update: dict = {
        "$set": {
            "status": to_status,
            "updated_at": now,
        },
        "$push": {"activity_log": entry},
    }
    if to_status == "resolved":
        update["$set"]["resolved_at"] = now
    if from_status == "resolved" and to_status == "open":
        update["$unset"] = {"resolved_at": ""}

    result = await db.email_tickets.find_one_and_update(
        {"company_id": company_id, "id": ticket_id},
        update,
        return_document=ReturnDocument.AFTER,
    )

    if result:
        from app.services import webhook_service

        if to_status == "escalated":
            event_type = "ticket.escalated"
        elif from_status == "pending" and to_status == "open":
            event_type = "ticket.created"
        else:
            event_type = None
        if event_type:
            await webhook_service.emit_event(
                company_id,
                event_type,
                {
                    "ticket_id": ticket_id,
                    "from_status": from_status,
                    "to_status": to_status,
                    "actor": actor,
                },
            )
        if to_status == "resolved":
            _fire_company_notification(
                company_id,
                title="✅ Ticket Closed",
                body=f"Ticket #{ticket_id} was marked as resolved by {actor}.",
                type="ticket_close",
                data={"ticket_id": ticket_id},
            )
        elif to_status == "open":
            _fire_company_notification(
                company_id,
                title="🔄 Ticket Reopened",
                body=f"Ticket #{ticket_id} was reopened by {actor}.",
                type="ticket_reopen",
                data={"ticket_id": ticket_id},
            )

    return result


async def assign_ticket(
    company_id: str,
    ticket_id: str,
    assignee_user_id: str | None,
    actor: str,
) -> dict | None:
    """Assign (or, with ``assignee_user_id=None``, unassign) a ticket.

    The assignee must be the company owner or a member. The assignee is
    notified directly via ``create_notification`` (which also fires a push
    through ``send_generic_push``).
    """
    ticket = await _fetch_ticket(company_id, ticket_id)
    now = _utcnow()

    if not assignee_user_id:
        entry = _activity_entry("unassigned", actor)
        return await db.email_tickets.find_one_and_update(
            {"company_id": company_id, "id": ticket_id},
            {
                "$unset": {"assigned_to": "", "assigned_by": "", "assigned_at": ""},
                "$set": {"updated_at": now},
                "$push": {"activity_log": entry},
            },
            return_document=ReturnDocument.AFTER,
        )

    # Validate the assignee exists and belongs to this company.
    user = await db.users.find_one({"user_id": assignee_user_id})
    if not user:
        raise ValueError("Assignee user not found")

    company = await company_service.get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")

    owner_id = company.get("user_id")
    member_ids = [m.get("user_id") for m in company.get("members", []) if m.get("user_id")]
    if assignee_user_id != owner_id and assignee_user_id not in member_ids:
        raise ValueError("Assignee is not a member of this company")

    entry = _activity_entry("assigned", actor, assignee_user_id=assignee_user_id)

    result = await db.email_tickets.find_one_and_update(
        {"company_id": company_id, "id": ticket_id},
        {
            "$set": {
                "assigned_to": assignee_user_id,
                "assigned_by": actor,
                "assigned_at": now,
                "updated_at": now,
            },
            "$push": {"activity_log": entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if result:
        asyncio.create_task(
            notification_service.create_notification(
                user_id=assignee_user_id,
                title="🎟 Ticket Assigned",
                body=(
                    f"You have been assigned Ticket #{ticket_id}: "
                    f"{ticket.get('subject', 'No subject')}"
                ),
                type="ticket_assigned",
                data={"ticket_id": ticket_id, "company_id": company_id},
            )
        )

    return result


async def set_priority(
    company_id: str,
    ticket_id: str,
    priority: str,
    actor: str,
) -> dict | None:
    """Change a ticket's priority and recompute its SLA deadlines from policy.

    If the ticket had a breach flag and the new deadlines are still in the
    future, the breach state is cleared.
    """
    if priority not in PRIORITIES:
        raise ValueError(f"Priority must be one of {PRIORITIES}")

    ticket = await _fetch_ticket(company_id, ticket_id)
    now = _utcnow()

    company = await company_service.get_company(company_id)
    policy = company_service.resolve_sla_policy(company)
    cfg = policy.get(priority) or policy["medium"]

    created_at = _as_utc_datetime(ticket.get("created_at")) or now
    first_response_deadline = created_at + timedelta(hours=cfg["first_response_h"])
    resolution_deadline = created_at + timedelta(hours=cfg["resolution_h"])

    # Reset the breach flag if the new deadlines are still in the future.
    sla_breached = bool(ticket.get("sla_breached", False))
    sla_breach_reason = ticket.get("sla_breach_reason")
    if sla_breached and first_response_deadline > now and resolution_deadline > now:
        sla_breached = False
        sla_breach_reason = None

    entry = _activity_entry("priority_change", actor, priority=priority)

    result = await db.email_tickets.find_one_and_update(
        {"company_id": company_id, "id": ticket_id},
        {
            "$set": {
                "priority": priority,
                "sla_policy": policy,
                "sla_first_response_deadline": first_response_deadline,
                "sla_resolution_deadline": resolution_deadline,
                "sla_breached": sla_breached,
                "sla_breach_reason": sla_breach_reason,
                "updated_at": now,
            },
            "$push": {"activity_log": entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if result:
        _fire_company_notification(
            company_id,
            title="🚨 Ticket Priority Updated",
            body=f"Ticket #{ticket_id} priority set to {priority}.",
            type="ticket_priority",
            data={"ticket_id": ticket_id, "priority": priority},
        )

    return result


async def add_internal_note(
    company_id: str,
    ticket_id: str,
    note: str,
    actor: str,
) -> dict | None:
    """Add an internal note to a ticket's audit activity log without customer visibility."""
    if not note or not note.strip():
        raise ValueError("Internal note cannot be empty")

    await _fetch_ticket(company_id, ticket_id)
    now = _utcnow()
    entry = _activity_entry("internal_note", actor, note=note.strip())

    return await db.email_tickets.find_one_and_update(
        {"company_id": company_id, "id": ticket_id},
        {
            "$set": {"updated_at": now},
            "$push": {"activity_log": entry},
        },
        return_document=ReturnDocument.AFTER,
    )


# ---------------------------------------------------------------------------
# Auto-escalation
# ---------------------------------------------------------------------------


def _is_fallback_message(message: dict) -> bool:
    content = (message.get("content") or "").strip()
    return content.lower().startswith(FALLBACK_TEXT.lower())


def _extract_customer_email(conversation: dict) -> str | None:
    """Resolve a customer email from the conversation, or None if unavailable."""
    email = conversation.get("sdk_user_email")
    if email:
        return email
    for message in reversed(conversation.get("messages") or []):
        if message.get("role") != "user":
            continue
        match = _EMAIL_RE.search(message.get("content") or "")
        if match:
            return match.group(0)
    return None


def should_escalate(conversation: dict, now: datetime | None = None) -> tuple[bool, str | None]:
    """Heuristic pass over a widget conversation.

    Returns ``(should_escalate, reason)`` where reason is one of:
      - ``ai_fallback_response`` — the last assistant reply was the fallback text
      - ``repeated_ai_failures`` — 2+ fallback replies (2+ consecutive unsuccessful AI answers)
      - ``idle_unanswered`` — user's last message unanswered for >= 10 min and
        the chat contains >= 2 user messages
    """
    now = now or _utcnow()
    messages = conversation.get("messages") or []
    if not messages:
        return False, None

    assistant_messages = [m for m in messages if m.get("role") == "assistant"]
    user_messages = [m for m in messages if m.get("role") == "user"]

    # Rule 1: last assistant message is the fallback text.
    if assistant_messages and _is_fallback_message(assistant_messages[-1]):
        return True, "ai_fallback_response"

    # Rule 3: 2+ fallback replies => repeated AI failures.
    if sum(1 for m in assistant_messages if _is_fallback_message(m)) >= 2:
        return True, "repeated_ai_failures"

    # Rule 2: user's last message unanswered for >= 10 min with >= 2 user messages.
    if len(user_messages) >= MIN_USER_MESSAGES_FOR_IDLE and messages[-1].get("role") == "user":
        last_user_ts = _as_utc_datetime(messages[-1].get("timestamp"))
        if last_user_ts and (now - last_user_ts) >= timedelta(minutes=IDLE_ESCALATION_MINUTES):
            return True, "idle_unanswered"

    return False, None


async def auto_escalate_chats(company_id: str) -> list[dict]:
    """Scan un-escalated widget conversations and create tickets for stuck chats.

    Tickets are created via ``company_email_service.create_ticket`` (which marks
    the originating chat as escalated), with structured handoff briefing containing
    [Customer Intent] + [Failed Steps/Friction] + [Suggested Action].
    """
    now = _utcnow()
    cutoff = now - timedelta(hours=24)
    cursor = db.widget_conversations.find({
        "company_id": company_id, 
        "escalated": {"$ne": True},
        "updated_at": {"$gte": cutoff}
    })

    created: list[dict] = []
    async for conversation in cursor:
        should, reason = should_escalate(conversation, now)
        if not should or not reason:
            continue

        session_id = conversation.get("session_id")
        if not session_id:
            continue

        customer_email = _extract_customer_email(conversation)
        if not customer_email:
            logger.info(
                "Skipping auto-escalation for session %s: no customer email available",
                session_id,
            )
            continue

        messages = conversation.get("messages", [])

        # Generate structured handoff briefing
        handoff_ctx: dict = {}
        try:
            handoff_data = await handoff_service.create_enhanced_handoff(
                session_id=session_id,
                company_id=company_id,
                messages=messages,
                escalation_reason=reason,
                customer_email=customer_email,
                intent=conversation.get("intent") or "support_request",
                page_url=conversation.get("page_url"),
            )
            handoff_ctx = handoff_data.get("handoff_context") or {}
            briefing = handoff_service.format_structured_briefing(handoff_ctx)
            priority = handoff_service.calculate_escalation_priority(handoff_ctx, default_priority="high")
        except Exception as e:
            logger.warning("Failed to generate rich handoff briefing for %s: %s", session_id, e)
            briefing = (
                f"### 📋 AI Handoff Briefing\n\n"
                f"🎯 **Customer Intent**\nAuto-escalated support request.\n"
                f"- **Escalation Reason:** {reason}"
            )
            priority = "high"

        history_texts = [
            f"{m.get('role', 'user')}: {m.get('content', '')}"
            for m in messages
        ]
        full_transcript = "\n\n".join(history_texts) if history_texts else "No transcript available."
        chat_summary = f"{briefing}\n\n---\n### 💬 Full Transcript\n\n{full_transcript}"

        default_intent_title = str(handoff_ctx.get("intent", "Auto-Escalated")).replace("_", " ").title()
        subject = conversation.get("subject") or f"Support Request: {default_intent_title}"

        try:
            ticket = await company_email_service.create_ticket(
                company_id=company_id,
                customer_email=customer_email,
                subject=subject,
                chat_summary=chat_summary,
                chat_session_id=session_id,
                escalation_reason=reason,
                priority=priority,
                handoff_context=handoff_ctx,
            )
            created.append(
                {"session_id": session_id, "ticket_id": ticket["id"], "reason": reason}
            )
            logger.info(
                "Auto-escalated session %s -> ticket %s (%s)", session_id, ticket["id"], reason
            )
        except Exception as e:
            logger.exception("Auto-escalation failed for session %s: %s", session_id, e)

    return created


# ---------------------------------------------------------------------------
# SLA watch
# ---------------------------------------------------------------------------


async def sla_watch_loop() -> int:
    """Flag tickets past their SLA deadlines and notify admins + assignee.

    A ticket breaches when:
      - it has no ``first_response_at`` and is past ``sla_first_response_deadline``, or
      - it is unresolved and past ``sla_resolution_deadline``.

    Returns the number of newly-flagged tickets.
    """
    now = _utcnow()
    query = {
        "status": {"$ne": "resolved"},
        "sla_breached": {"$ne": True},
        "$or": [
            {
                "first_response_at": {"$exists": False},
                "sla_first_response_deadline": {"$lte": now},
            },
            {"sla_resolution_deadline": {"$lte": now}},
        ],
    }
    cursor = db.email_tickets.find(query)

    flagged = 0
    async for ticket in cursor:
        ticket_id = ticket.get("id")
        company_id = ticket.get("company_id")
        if not ticket_id or not company_id:
            continue

        first_response_deadline = _as_utc_datetime(ticket.get("sla_first_response_deadline"))
        resolution_deadline = _as_utc_datetime(ticket.get("sla_resolution_deadline"))

        reasons = []
        if not ticket.get("first_response_at") and first_response_deadline and first_response_deadline <= now:
            reasons.append("first_response")
        if resolution_deadline and resolution_deadline <= now:
            reasons.append("resolution")
        if not reasons:
            continue

        reason = ",".join(reasons)
        entry = _activity_entry("sla_breach", "system", reason=reason)

        await db.email_tickets.update_one(
            {"company_id": company_id, "id": ticket_id},
            {
                "$set": {
                    "sla_breached": True,
                    "sla_breach_reason": reason,
                    "updated_at": now,
                },
                "$push": {"activity_log": entry},
            },
        )
        flagged += 1

        _fire_company_notification(
            company_id,
            title="⏰ SLA Breach",
            body=(
                f"Ticket #{ticket_id} breached its SLA ({reason.replace('_', ' ')}). "
                f"Subject: {ticket.get('subject', 'No subject')}"
            ),
            type="ticket_sla_breach",
            data={"ticket_id": ticket_id, "reason": reason},
        )

        assignee = ticket.get("assigned_to")
        if assignee:
            asyncio.create_task(
                notification_service.create_notification(
                    user_id=assignee,
                    title="⏰ SLA Breach",
                    body=(
                        f"Ticket #{ticket_id} assigned to you breached its SLA "
                        f"({reason.replace('_', ' ')})."
                    ),
                    type="ticket_sla_breach",
                    data={"ticket_id": ticket_id, "company_id": company_id, "reason": reason},
                )
            )

    if flagged:
        logger.info("SLA watch flagged %d tickets", flagged)
    return flagged
