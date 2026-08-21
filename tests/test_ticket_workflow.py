"""Tests for the full ticket workflow: state machine, assignment, priority/SLA,
auto-escalation heuristics, activity log, and the first-response hook."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.email_models import (
    TicketAssignRequest,
    TicketPriorityUpdate,
    TicketStatusUpdate,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ticket(**overrides) -> dict:
    ticket = {
        "id": "ABC12345",
        "company_id": "comp_1",
        "customer_email": "customer@example.com",
        "subject": "Test subject",
        "status": "pending",
        "priority": "medium",
        "created_at": NOW,
        "updated_at": NOW,
        "activity_log": [],
    }
    ticket.update(overrides)
    return ticket


@pytest.fixture
def mock_ticket_db():
    """A mock db focused on the collections ticket_service touches."""
    db = MagicMock()
    db.email_tickets = MagicMock()
    db.users = MagicMock()
    db.widget_conversations = MagicMock()
    db.notifications = MagicMock()
    db.companies = MagicMock()
    return db


@pytest.fixture(autouse=True)
def silence_notification_tasks():
    """Neutralize fire-and-forget notification tasks so tests stay clean."""
    from app.services import ticket_service

    def close_coroutine(coro):
        try:
            coro.close()
        except Exception:
            pass

    with patch.object(ticket_service, "_fire_company_notification", MagicMock()):
        with patch.object(ticket_service, "asyncio") as aio:
            aio.create_task = close_coroutine
            yield


# ---------------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------------


class TestModelValidation:
    def test_status_update_validates_status(self):
        with pytest.raises(ValueError):
            TicketStatusUpdate(status="bogus")
        assert TicketStatusUpdate(status="in_progress").status == "in_progress"

    def test_status_update_validates_priority(self):
        with pytest.raises(ValueError):
            TicketStatusUpdate(status="pending", priority="extreme")
        assert TicketStatusUpdate(status="pending", priority="urgent").priority == "urgent"

    def test_priority_update_defaults_to_medium(self):
        assert TicketPriorityUpdate().priority == "medium"

    def test_priority_update_validates(self):
        with pytest.raises(ValueError):
            TicketPriorityUpdate(priority="top")
        assert TicketPriorityUpdate(priority="high").priority == "high"

    def test_assign_request_defaults_to_unassign(self):
        assert TicketAssignRequest().assignee_user_id is None


# ---------------------------------------------------------------------------
# Transition state machine
# ---------------------------------------------------------------------------


class TestTransitionTicket:
    @pytest.mark.asyncio
    async def test_valid_transition_matrix(self, mock_ticket_db):
        from app.services import ticket_service

        valid_pairs = [
            ("pending", "in_progress"),
            ("in_progress", "pending"),
            ("pending", "resolved"),
            ("in_progress", "resolved"),
            ("awaiting_customer", "resolved"),
            ("follow_up", "resolved"),
            ("open", "resolved"),
            ("resolved", "open"),
        ]
        for from_status, to_status in valid_pairs:
            mock_ticket_db.email_tickets.find_one = AsyncMock(
                return_value=_ticket(status=from_status, updated_at=NOW)
            )
            mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(
                return_value=_ticket(status=to_status)
            )
            with patch.object(ticket_service, "db", mock_ticket_db):
                with patch.object(ticket_service, "notification_service") as notif:
                    notif.notify_company = AsyncMock()
                    with patch.object(ticket_service, "_utcnow", return_value=NOW):
                        result = await ticket_service.transition_ticket(
                            "comp_1", "ABC12345", to_status, actor="user_1"
                        )
            assert result is not None, f"{from_status} -> {to_status} should be valid"

    @pytest.mark.asyncio
    async def test_invalid_transition_rejected(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(
            return_value=_ticket(status="awaiting_customer")
        )
        with patch.object(ticket_service, "db", mock_ticket_db):
            with pytest.raises(ValueError):
                await ticket_service.transition_ticket(
                    "comp_1", "ABC12345", "pending", actor="user_1"
                )

    @pytest.mark.asyncio
    async def test_resolved_to_open_rejected_after_48h(self, mock_ticket_db):
        from app.services import ticket_service

        stale = NOW - timedelta(hours=49)
        mock_ticket_db.email_tickets.find_one = AsyncMock(
            return_value=_ticket(status="resolved", updated_at=stale)
        )
        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "_utcnow", return_value=NOW):
                with pytest.raises(ValueError):
                    await ticket_service.transition_ticket(
                        "comp_1", "ABC12345", "open", actor="user_1"
                    )

    @pytest.mark.asyncio
    async def test_missing_ticket_raises(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(return_value=None)
        with patch.object(ticket_service, "db", mock_ticket_db):
            with pytest.raises(ValueError):
                await ticket_service.transition_ticket(
                    "comp_1", "NOPE", "resolved", actor="user_1"
                )

    @pytest.mark.asyncio
    async def test_resolve_sets_resolved_at_and_activity(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(
            return_value=_ticket(status="follow_up", activity_log=[])
        )
        updated = _ticket(status="resolved", resolved_at=NOW)
        mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(return_value=updated)

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "notification_service") as notif:
                notif.notify_company = AsyncMock()
                with patch.object(ticket_service, "_utcnow", return_value=NOW):
                    await ticket_service.transition_ticket(
                        "comp_1", "ABC12345", "resolved", actor="user_1", note="done"
                    )

        args, kwargs = mock_ticket_db.email_tickets.find_one_and_update.call_args
        assert args[0] == {"company_id": "comp_1", "id": "ABC12345"}
        update = args[1]
        assert update["$set"]["resolved_at"] == NOW
        assert update["$set"]["status"] == "resolved"
        pushed = update["$push"]["activity_log"]
        assert pushed["action"] == "status_change"
        assert pushed["from_status"] == "follow_up"
        assert pushed["to_status"] == "resolved"
        assert pushed["actor"] == "user_1"
        assert pushed["note"] == "done"
        assert pushed["timestamp"] == NOW

    @pytest.mark.asyncio
    async def test_reopen_unsets_resolved_at(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(
            return_value=_ticket(status="resolved", updated_at=NOW)
        )
        mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(
            return_value=_ticket(status="open")
        )
        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "notification_service") as notif:
                notif.notify_company = AsyncMock()
                with patch.object(ticket_service, "_utcnow", return_value=NOW):
                    await ticket_service.transition_ticket(
                        "comp_1", "ABC12345", "open", actor="user_1"
                    )

        args, _ = mock_ticket_db.email_tickets.find_one_and_update.call_args
        assert "$unset" in args[1]
        assert args[1]["$unset"] == {"resolved_at": ""}


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------


class TestAssignTicket:
    @pytest.mark.asyncio
    async def test_assign_to_member_succeeds(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(return_value=_ticket())
        mock_ticket_db.users.find_one = AsyncMock(
            return_value={"user_id": "member_1", "email": "member@example.com"}
        )
        mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(
            return_value=_ticket(assigned_to="member_1", assigned_by="user_1", assigned_at=NOW)
        )

        company = {"id": "comp_1", "user_id": "owner_1", "members": [{"user_id": "member_1"}]}

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_service") as comp_svc:
                comp_svc.get_company = AsyncMock(return_value=company)
                with patch.object(ticket_service, "notification_service") as notif:
                    notif.create_notification = AsyncMock()
                    with patch.object(ticket_service, "_utcnow", return_value=NOW):
                        await ticket_service.assign_ticket(
                            "comp_1", "ABC12345", "member_1", actor="user_1"
                        )

        args, _ = mock_ticket_db.email_tickets.find_one_and_update.call_args
        update = args[1]
        assert update["$set"]["assigned_to"] == "member_1"
        assert update["$set"]["assigned_by"] == "user_1"
        assert update["$set"]["assigned_at"] == NOW
        assert update["$push"]["activity_log"]["action"] == "assigned"

    @pytest.mark.asyncio
    async def test_assign_to_owner_succeeds(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(return_value=_ticket())
        mock_ticket_db.users.find_one = AsyncMock(
            return_value={"user_id": "owner_1", "email": "owner@example.com"}
        )
        mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(return_value=_ticket())

        company = {"id": "comp_1", "user_id": "owner_1", "members": []}
        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_service") as comp_svc:
                comp_svc.get_company = AsyncMock(return_value=company)
                with patch.object(ticket_service, "notification_service") as notif:
                    notif.create_notification = AsyncMock()
                    await ticket_service.assign_ticket(
                        "comp_1", "ABC12345", "owner_1", actor="owner_1"
                    )

        mock_ticket_db.email_tickets.find_one_and_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_assign_to_non_member_rejected(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(return_value=_ticket())
        mock_ticket_db.users.find_one = AsyncMock(
            return_value={"user_id": "stranger", "email": "stranger@example.com"}
        )
        company = {"id": "comp_1", "user_id": "owner_1", "members": [{"user_id": "member_1"}]}

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_service") as comp_svc:
                comp_svc.get_company = AsyncMock(return_value=company)
                with pytest.raises(ValueError):
                    await ticket_service.assign_ticket(
                        "comp_1", "ABC12345", "stranger", actor="user_1"
                    )

        mock_ticket_db.email_tickets.find_one_and_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_to_unknown_user_rejected(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(return_value=_ticket())
        mock_ticket_db.users.find_one = AsyncMock(return_value=None)

        with patch.object(ticket_service, "db", mock_ticket_db):
            with pytest.raises(ValueError):
                await ticket_service.assign_ticket(
                    "comp_1", "ABC12345", "ghost", actor="user_1"
                )

    @pytest.mark.asyncio
    async def test_unassign_clears_assignment(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(
            return_value=_ticket(assigned_to="member_1", assigned_by="user_1", assigned_at=NOW)
        )
        mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(return_value=_ticket())

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "_utcnow", return_value=NOW):
                await ticket_service.assign_ticket("comp_1", "ABC12345", None, actor="user_1")

        args, _ = mock_ticket_db.email_tickets.find_one_and_update.call_args
        update = args[1]
        assert update["$unset"] == {"assigned_to": "", "assigned_by": "", "assigned_at": ""}
        assert update["$push"]["activity_log"]["action"] == "unassigned"


# ---------------------------------------------------------------------------
# Priority + SLA
# ---------------------------------------------------------------------------


class TestSetPriority:
    @pytest.mark.asyncio
    async def test_invalid_priority_rejected(self, mock_ticket_db):
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(return_value=_ticket())
        with patch.object(ticket_service, "db", mock_ticket_db):
            with pytest.raises(ValueError):
                await ticket_service.set_priority(
                    "comp_1", "ABC12345", "extreme", actor="user_1"
                )

    @pytest.mark.asyncio
    async def test_recomputes_deadlines_from_default_policy(self, mock_ticket_db):
        from app.services import company_service as real_company_service
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(return_value=_ticket())
        mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(return_value=_ticket())

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_service") as comp_svc:
                comp_svc.get_company = AsyncMock(return_value=None)  # default policy
                comp_svc.resolve_sla_policy = real_company_service.resolve_sla_policy
                with patch.object(ticket_service, "notification_service") as notif:
                    notif.notify_company = AsyncMock()
                    with patch.object(ticket_service, "_utcnow", return_value=NOW):
                        await ticket_service.set_priority(
                            "comp_1", "ABC12345", "high", actor="user_1"
                        )

        args, _ = mock_ticket_db.email_tickets.find_one_and_update.call_args
        update = args[1]
        assert update["$set"]["priority"] == "high"
        assert update["$set"]["sla_first_response_deadline"] == NOW + timedelta(hours=2)
        assert update["$set"]["sla_resolution_deadline"] == NOW + timedelta(hours=8)
        assert update["$set"]["sla_breached"] is False

    @pytest.mark.asyncio
    async def test_priority_change_resets_breach_when_deadlines_in_future(self, mock_ticket_db):
        from app.services import company_service as real_company_service
        from app.services import ticket_service

        mock_ticket_db.email_tickets.find_one = AsyncMock(
            return_value=_ticket(sla_breached=True, sla_breach_reason="resolution")
        )
        mock_ticket_db.email_tickets.find_one_and_update = AsyncMock(return_value=_ticket())

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_service") as comp_svc:
                comp_svc.get_company = AsyncMock(return_value=None)
                comp_svc.resolve_sla_policy = real_company_service.resolve_sla_policy
                with patch.object(ticket_service, "notification_service") as notif:
                    notif.notify_company = AsyncMock()
                    with patch.object(ticket_service, "_utcnow", return_value=NOW):
                        await ticket_service.set_priority(
                            "comp_1", "ABC12345", "urgent", actor="user_1"
                        )

        args, _ = mock_ticket_db.email_tickets.find_one_and_update.call_args
        assert args[1]["$set"]["sla_breached"] is False
        assert args[1]["$set"]["sla_breach_reason"] is None


class TestSlaPolicyResolution:
    def test_default_policy_when_company_none(self):
        from app.services import company_service

        policy = company_service.resolve_sla_policy(None)
        assert policy["low"]["first_response_h"] == 8
        assert policy["medium"]["first_response_h"] == 4
        assert policy["high"]["first_response_h"] == 2
        assert policy["urgent"]["first_response_h"] == 1

    def test_company_policy_merges_over_defaults(self):
        from app.services import company_service

        company = {
            "sla_policy": {
                "high": {"first_response_h": 1, "resolution_h": 4},
                "urgent": {"first_response_h": 0.5},
            }
        }
        policy = company_service.resolve_sla_policy(company)
        assert policy["high"]["first_response_h"] == 1
        assert policy["high"]["resolution_h"] == 4
        # urgent only overrides first_response, resolution stays at default
        assert policy["urgent"]["first_response_h"] == 0.5
        assert policy["urgent"]["resolution_h"] == 4
        # untouched priorities keep defaults
        assert policy["low"]["first_response_h"] == 8


class TestSlaWatchLoop:
    @pytest.mark.asyncio
    async def test_flags_first_response_breach(self, mock_ticket_db):
        from app.services import ticket_service

        deadline = NOW - timedelta(minutes=1)
        ticket = _ticket(
            sla_first_response_deadline=deadline,
            sla_resolution_deadline=NOW + timedelta(days=1),
            sla_breached=False,
        )
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[ticket])
        mock_ticket_db.email_tickets.find = MagicMock(return_value=cursor)
        mock_ticket_db.email_tickets.update_one = AsyncMock()

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "notification_service") as notif:
                notif.notify_company = AsyncMock()
                with patch.object(ticket_service, "_utcnow", return_value=NOW):
                    flagged = await ticket_service.sla_watch_loop()

        assert flagged == 1
        args, _ = mock_ticket_db.email_tickets.update_one.call_args
        assert args[1]["$set"]["sla_breached"] is True
        assert args[1]["$set"]["sla_breach_reason"] == "first_response"

    @pytest.mark.asyncio
    async def test_flags_resolution_breach(self, mock_ticket_db):
        from app.services import ticket_service

        ticket = _ticket(
            first_response_at=NOW - timedelta(hours=5),
            sla_first_response_deadline=NOW - timedelta(hours=4),
            sla_resolution_deadline=NOW - timedelta(minutes=1),
        )
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[ticket])
        mock_ticket_db.email_tickets.find = MagicMock(return_value=cursor)
        mock_ticket_db.email_tickets.update_one = AsyncMock()

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "notification_service") as notif:
                notif.notify_company = AsyncMock()
                with patch.object(ticket_service, "_utcnow", return_value=NOW):
                    flagged = await ticket_service.sla_watch_loop()

        assert flagged == 1
        args, _ = mock_ticket_db.email_tickets.update_one.call_args
        assert args[1]["$set"]["sla_breach_reason"] == "resolution"

    @pytest.mark.asyncio
    async def test_skips_ticket_within_deadlines(self, mock_ticket_db):
        from app.services import ticket_service

        ticket = _ticket(
            sla_first_response_deadline=NOW + timedelta(hours=2),
            sla_resolution_deadline=NOW + timedelta(days=1),
        )
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[ticket])
        mock_ticket_db.email_tickets.find = MagicMock(return_value=cursor)
        mock_ticket_db.email_tickets.update_one = AsyncMock()

        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "notification_service") as notif:
                notif.notify_company = AsyncMock()
                with patch.object(ticket_service, "_utcnow", return_value=NOW):
                    flagged = await ticket_service.sla_watch_loop()

        assert flagged == 0
        mock_ticket_db.email_tickets.update_one.assert_not_called()


# ---------------------------------------------------------------------------
# Auto-escalation heuristics
# ---------------------------------------------------------------------------


FALLBACK = "I'm sorry, I wasn't able to find that information right now. Could you try rephrasing your question?"


def _chat(*messages) -> dict:
    return {
        "company_id": "comp_1",
        "session_id": "sess_1",
        "subject": "Need help",
        "sdk_user_email": "customer@example.com",
        "messages": list(messages),
    }


def _msg(role: str, content: str, timestamp: datetime | None = None) -> dict:
    return {
        "role": role,
        "content": content,
        "timestamp": (timestamp or NOW).isoformat(),
    }


class TestShouldEscalate:
    def test_healthy_chat_not_escalated(self):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "How do I reset my password?"),
            _msg("assistant", "Go to Settings > Security > Reset password."),
        )
        should, reason = ticket_service.should_escalate(chat, NOW)
        assert should is False
        assert reason is None

    def test_fallback_text_triggers(self):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "Tell me about quantum widgets"),
            _msg("assistant", FALLBACK),
        )
        should, reason = ticket_service.should_escalate(chat, NOW)
        assert should is True
        assert reason == "ai_fallback_response"

    def test_two_fallbacks_trigger_repeated_failures(self):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "Question one"),
            _msg("assistant", FALLBACK),
            _msg("user", "Question two"),
            _msg("assistant", "Here is a real answer this time."),
            _msg("user", "Question three"),
            _msg("assistant", FALLBACK),
        )
        should, reason = ticket_service.should_escalate(chat, NOW)
        assert should is True
        assert reason == "ai_fallback_response"

    def test_idle_unanswered_user_message_triggers(self):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "First question"),
            _msg("assistant", "Answer one"),
            _msg("user", "Second question, no answer", timestamp=NOW - timedelta(minutes=15)),
        )
        should, reason = ticket_service.should_escalate(chat, NOW)
        assert should is True
        assert reason == "idle_unanswered"

    def test_idle_rule_requires_two_user_messages(self):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "Only question, unanswered", timestamp=NOW - timedelta(minutes=30)),
        )
        should, reason = ticket_service.should_escalate(chat, NOW)
        assert should is False

    def test_recent_unanswered_message_not_escalated(self):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "First question"),
            _msg("assistant", "Answer"),
            _msg("user", "Follow-up, unanswered", timestamp=NOW - timedelta(minutes=5)),
        )
        should, reason = ticket_service.should_escalate(chat, NOW)
        assert should is False


class TestAutoEscalateChats:
    @pytest.mark.asyncio
    async def test_creates_ticket_for_stuck_chat(self, mock_ticket_db):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "Help me please", timestamp=NOW - timedelta(minutes=15)),
            _msg("assistant", FALLBACK, timestamp=NOW - timedelta(minutes=14)),
        )
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[chat])
        mock_ticket_db.widget_conversations.find = MagicMock(return_value=cursor)

        created_ticket = {"id": "TICKET01"}
        create_ticket_mock = AsyncMock(return_value=created_ticket)
        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_email_service") as email_svc:
                email_svc.create_ticket = create_ticket_mock
                with patch.object(ticket_service, "_utcnow", return_value=NOW):
                    result = await ticket_service.auto_escalate_chats("comp_1")

        assert result == [
            {
                "session_id": "sess_1",
                "ticket_id": "TICKET01",
                "reason": "ai_fallback_response",
            }
        ]
        call_kwargs = create_ticket_mock.call_args.kwargs
        assert call_kwargs["company_id"] == "comp_1"
        assert call_kwargs["customer_email"] == "customer@example.com"
        assert call_kwargs["escalation_reason"] == "ai_fallback_response"
        assert call_kwargs["chat_session_id"] == "sess_1"

    @pytest.mark.asyncio
    async def test_skips_healthy_chats(self, mock_ticket_db):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "Question"),
            _msg("assistant", "Solid answer"),
        )
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[chat])
        mock_ticket_db.widget_conversations.find = MagicMock(return_value=cursor)

        create_ticket_mock = AsyncMock()
        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_email_service") as email_svc:
                email_svc.create_ticket = create_ticket_mock
                result = await ticket_service.auto_escalate_chats("comp_1")

        assert result == []
        create_ticket_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_chat_without_customer_email(self, mock_ticket_db):
        from app.services import ticket_service

        chat = _chat(
            _msg("user", "Help", timestamp=NOW - timedelta(minutes=20)),
            _msg("assistant", FALLBACK),
        )
        chat.pop("sdk_user_email")
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[chat])
        mock_ticket_db.widget_conversations.find = MagicMock(return_value=cursor)

        create_ticket_mock = AsyncMock()
        with patch.object(ticket_service, "db", mock_ticket_db):
            with patch.object(ticket_service, "company_email_service") as email_svc:
                email_svc.create_ticket = create_ticket_mock
                result = await ticket_service.auto_escalate_chats("comp_1")

        assert result == []
        create_ticket_mock.assert_not_called()


# ---------------------------------------------------------------------------
# create_ticket SLA stamping
# ---------------------------------------------------------------------------


class TestCreateTicketSla:
    @pytest.mark.asyncio
    async def test_create_ticket_stamps_sla_fields(self):
        from app.services import company_email_service
        from app.services import company_service as real_company_service

        db = MagicMock()
        db.email_tickets = MagicMock()
        db.email_tickets.find_one = AsyncMock(return_value=None)
        db.email_tickets.insert_one = AsyncMock()
        db.widget_conversations = MagicMock()
        db.widget_conversations.find_one = AsyncMock(return_value=None)

        company = {"id": "comp_1", "sla_policy": {"high": {"first_response_h": 1, "resolution_h": 3}}}
        created = {
            "id": "TICKET01",
            "company_id": "comp_1",
            "status": "pending",
        }

        with patch.object(company_email_service, "db", db):
            with patch.object(company_email_service, "company_service") as comp_svc:
                comp_svc.get_company = AsyncMock(return_value=company)
                comp_svc.resolve_sla_policy = real_company_service.resolve_sla_policy
                with patch.object(company_email_service, "notification_service") as notif:
                    notif.notify_company = AsyncMock()
                    with patch.object(company_email_service, "_send_new_ticket_email", AsyncMock()):
                        with patch.object(
                            company_email_service, "_send_ticket_confirmation_email", AsyncMock()
                        ):
                            with patch("app.services.company_email_service.datetime") as dt:
                                dt.now.return_value = NOW
                                # first call: id-uniqueness loop -> None
                                # second call: fetch of the created doc -> created
                                db.email_tickets.find_one = AsyncMock(
                                    side_effect=[None, created]
                                )
                                result = await company_email_service.create_ticket(
                                    "comp_1",
                                    "customer@example.com",
                                    "Subject",
                                    "Summary",
                                    priority="high",
                                    escalation_reason="ai_fallback_response",
                                )

        assert result is not None
        doc = db.email_tickets.insert_one.call_args.args[0]
        assert doc["priority"] == "high"
        assert doc["sla_first_response_deadline"] == NOW + timedelta(hours=1)
        assert doc["sla_resolution_deadline"] == NOW + timedelta(hours=3)
        assert doc["sla_breached"] is False
        assert doc["escalation_reason"] == "ai_fallback_response"
        assert doc["escalated_at"] == NOW
        assert doc["activity_log"][0]["action"] == "created"
        assert doc["sla_policy"]["high"]["first_response_h"] == 1

    @pytest.mark.asyncio
    async def test_create_ticket_defaults_priority(self):
        from app.services import company_email_service
        from app.services import company_service as real_company_service

        db = MagicMock()
        db.email_tickets = MagicMock()
        db.email_tickets.find_one = AsyncMock(return_value=None)
        db.email_tickets.insert_one = AsyncMock()
        db.widget_conversations = MagicMock()
        db.widget_conversations.find_one = AsyncMock(return_value=None)
        created = {"id": "TICKET02", "company_id": "comp_1", "status": "pending"}

        with patch.object(company_email_service, "db", db):
            with patch.object(company_email_service, "company_service") as comp_svc:
                comp_svc.get_company = AsyncMock(return_value=None)
                comp_svc.resolve_sla_policy = real_company_service.resolve_sla_policy
                with patch.object(company_email_service, "notification_service") as notif:
                    notif.notify_company = AsyncMock()
                    with patch.object(company_email_service, "_send_new_ticket_email", AsyncMock()):
                        with patch.object(
                            company_email_service, "_send_ticket_confirmation_email", AsyncMock()
                        ):
                            with patch("app.services.company_email_service.datetime") as dt:
                                dt.now.return_value = NOW
                                db.email_tickets.find_one = AsyncMock(
                                    side_effect=[None, created]
                                )
                                await company_email_service.create_ticket(
                                    "comp_1",
                                    "customer@example.com",
                                    "Subject",
                                    "Summary",
                                    priority="bogus",
                                )

        doc = db.email_tickets.insert_one.call_args.args[0]
        assert doc["priority"] == "medium"
        # default policy: medium -> 4h first response, 24h resolution
        assert doc["sla_first_response_deadline"] == NOW + timedelta(hours=4)
        assert doc["sla_resolution_deadline"] == NOW + timedelta(hours=24)


# ---------------------------------------------------------------------------
# send_ticket_reply first-response hook
# ---------------------------------------------------------------------------


class TestFirstResponseHook:
    @pytest.mark.asyncio
    async def test_reply_stamps_first_response_and_clears_breach(self):
        from app.services import company_email_service

        ticket = {
            "id": "ABC12345",
            "company_id": "comp_1",
            "customer_email": "customer@example.com",
            "subject": "Subject",
            "resolve_token": "tok",
            "status": "follow_up",
            "messages": [],
            "sla_breached": True,
            "sla_breach_reason": "first_response",
        }
        company = {"id": "comp_1", "name": "Test Co", "email_slug": "testco"}

        db = MagicMock()
        db.email_tickets = MagicMock()
        db.email_tickets.update_one = AsyncMock()

        with patch.object(company_email_service, "db", db):
            with patch.object(company_email_service, "get_ticket", AsyncMock(return_value=ticket)):
                with patch.object(company_email_service, "company_service") as comp_svc:
                    comp_svc.get_company = AsyncMock(return_value=company)
                    with patch.object(company_email_service, "asyncio") as aio:
                        aio.to_thread = AsyncMock()
                        with patch.object(
                            company_email_service,
                            "add_html_with_inline_images",
                            MagicMock(),
                        ):
                            with patch.object(
                                company_email_service, "get_random_avatar", return_value="/a.png"
                            ):
                                with patch(
                                    "app.services.company_email_service.datetime"
                                ) as dt:
                                    dt.now.return_value = NOW
                                    await company_email_service.send_ticket_reply(
                                        "comp_1",
                                        "ABC12345",
                                        "Here is your answer",
                                        agent_name="Agent",
                                    )

        args, _ = db.email_tickets.update_one.call_args
        update = args[1]
        assert update["$set"]["first_response_at"] == NOW
        assert update["$set"]["sla_breached"] is False
        assert update["$set"]["sla_breach_reason"] is None
        assert update["$set"]["status"] == "awaiting_customer"
        assert update["$push"]["activity_log"]["action"] == "agent_reply"

    @pytest.mark.asyncio
    async def test_reply_preserves_existing_first_response(self):
        from app.services import company_email_service

        first_response = NOW - timedelta(hours=1)
        ticket = {
            "id": "ABC12345",
            "company_id": "comp_1",
            "customer_email": "customer@example.com",
            "subject": "Subject",
            "resolve_token": "tok",
            "status": "follow_up",
            "messages": [],
            "first_response_at": first_response,
        }
        company = {"id": "comp_1", "name": "Test Co", "email_slug": "testco"}

        db = MagicMock()
        db.email_tickets = MagicMock()
        db.email_tickets.update_one = AsyncMock()

        with patch.object(company_email_service, "db", db):
            with patch.object(company_email_service, "get_ticket", AsyncMock(return_value=ticket)):
                with patch.object(company_email_service, "company_service") as comp_svc:
                    comp_svc.get_company = AsyncMock(return_value=company)
                    with patch.object(company_email_service, "asyncio") as aio:
                        aio.to_thread = AsyncMock()
                        with patch.object(
                            company_email_service,
                            "add_html_with_inline_images",
                            MagicMock(),
                        ):
                            with patch.object(
                                company_email_service, "get_random_avatar", return_value="/a.png"
                            ):
                                with patch("app.services.company_email_service.datetime") as dt:
                                    dt.now.return_value = NOW
                                    await company_email_service.send_ticket_reply(
                                        "comp_1",
                                        "ABC12345",
                                        "Another reply",
                                        agent_name="Agent",
                                    )

        args, _ = db.email_tickets.update_one.call_args
        assert args[1]["$set"]["first_response_at"] == first_response


# ---------------------------------------------------------------------------
# list_tickets filters
# ---------------------------------------------------------------------------


class TestListTicketsFilters:
    @pytest.mark.asyncio
    async def test_filters_build_query(self):
        from app.services import company_email_service

        db = MagicMock()
        db.email_tickets = MagicMock()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        db.email_tickets.aggregate = MagicMock(return_value=cursor)

        with patch.object(company_email_service, "db", db):
            await company_email_service.list_tickets(
                "comp_1",
                status="follow_up",
                priority="urgent",
                assigned_to="member_1",
                sla_breached=True,
            )

        pipeline = db.email_tickets.aggregate.call_args.args[0]
        assert pipeline[0]["$match"]["status"] == "follow_up"
        assert pipeline[0]["$match"]["priority"] == "urgent"
        assert pipeline[0]["$match"]["assigned_to"] == "member_1"
        assert pipeline[0]["$match"]["sla_breached"] is True
        project = [s for s in pipeline if "$project" in s][0]["$project"]
        assert project["priority"] == 1
        assert project["assigned_to"] == 1
        assert project["sla_breached"] == 1

    @pytest.mark.asyncio
    async def test_count_tickets_applies_filters(self):
        from app.services import company_email_service

        db = MagicMock()
        db.email_tickets = MagicMock()
        db.email_tickets.count_documents = AsyncMock(return_value=3)

        with patch.object(company_email_service, "db", db):
            total = await company_email_service.count_tickets(
                "comp_1", priority="high", sla_breached=True
            )

        assert total == 3
        query = db.email_tickets.count_documents.call_args.args[0]
        assert query["priority"] == "high"
        assert query["sla_breached"] is True
        assert query["status"] == {"$ne": "resolved"}
