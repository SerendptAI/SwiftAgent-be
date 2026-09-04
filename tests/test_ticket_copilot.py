"""
Tests for Agent Copilot Service and Action Execution
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services import ticket_service, ticket_copilot_service
from app.models.email_models import (
    CopilotSuggestRequest,
    CopilotExecuteActionRequest,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestTicketInternalNote:
    @pytest.mark.asyncio
    async def test_add_internal_note_success(self):
        ticket = {
            "id": "TCK_NOTE_01",
            "company_id": "comp_1",
            "subject": "Payment issue",
            "activity_log": [],
        }

        mock_db = MagicMock()
        mock_db.email_tickets = MagicMock()
        mock_db.email_tickets.find_one = AsyncMock(return_value=ticket)

        updated_ticket = dict(ticket)
        updated_ticket["activity_log"] = [
            {"action": "internal_note", "actor": "agent_bob", "note": "Customer verified via phone"}
        ]
        mock_db.email_tickets.find_one_and_update = AsyncMock(return_value=updated_ticket)

        with patch.object(ticket_service, "db", mock_db):
            res = await ticket_service.add_internal_note(
                company_id="comp_1",
                ticket_id="TCK_NOTE_01",
                note="Customer verified via phone",
                actor="agent_bob",
            )

        assert res is not None
        assert len(res["activity_log"]) == 1
        assert res["activity_log"][0]["action"] == "internal_note"
        assert res["activity_log"][0]["note"] == "Customer verified via phone"

    @pytest.mark.asyncio
    async def test_add_internal_note_empty_raises(self):
        with pytest.raises(ValueError, match="Internal note cannot be empty"):
            await ticket_service.add_internal_note("comp_1", "TCK_1", "   ", "agent")


class TestTicketCopilotReply:
    @pytest.mark.asyncio
    async def test_copilot_reply_llm_success(self):
        ticket_data = {
            "ticket": {
                "id": "TCK_COPILOT_1",
                "company_id": "comp_1",
                "customer_email": "alice@example.com",
                "customer_name": "Alice Smith",
                "subject": "Unable to export report",
                "status": "pending",
                "priority": "medium",
                "messages": [
                    {
                        "direction": "inbound",
                        "body_text": "I tried clicking Export to CSV but nothing happens.",
                    }
                ],
                "handoff_context": {
                    "customer_sentiment": "frustrated",
                    "conversation_summary": "Customer unable to export report.",
                    "ai_failure_points": ["Playwright timeout on export button"],
                    "suggested_actions": ["Verify browser permissions and provide direct download link"],
                },
            },
            "attributed_chat": None,
        }

        mock_company = {"id": "comp_1", "name": "Acme Corp", "ai_provider": "anthropic"}

        llm_json_response = {
            "suggested_reply": "Dear Alice, I am so sorry for the trouble exporting your report. Let me assist you directly.",
            "suggested_subject": "Re: Unable to export report - Solution",
            "customer_sentiment": "frustrated",
            "reasoning": "Acknowledge frustration and offer resolution.",
            "suggested_actions": [
                {
                    "action_type": "status_change",
                    "label": "Mark as Awaiting Customer",
                    "confidence": 0.95,
                    "parameters": {"status": "awaiting_customer"},
                    "reason": "Draft queued for customer review.",
                },
                {
                    "action_type": "priority_change",
                    "label": "Escalate Priority to Urgent",
                    "confidence": 0.90,
                    "parameters": {"priority": "urgent"},
                    "reason": "Customer is frustrated by repeated export timeouts.",
                },
            ],
        }

        mock_llm = AsyncMock()
        ai_message_mock = MagicMock()
        import json
        ai_message_mock.content = json.dumps(llm_json_response)
        mock_llm.ainvoke = AsyncMock(return_value=ai_message_mock)

        with patch("app.services.company_email_service.get_ticket_with_chat", AsyncMock(return_value=ticket_data)):
            with patch("app.services.company_service.get_company", AsyncMock(return_value=mock_company)):
                with patch("app.services.knowledge_service.search_knowledge", AsyncMock(return_value={"results": []})):
                    with patch("app.services.ticket_copilot_service.get_llm", return_value=mock_llm):
                        res = await ticket_copilot_service.generate_copilot_reply(
                            company_id="comp_1",
                            ticket_id="TCK_COPILOT_1",
                            tone="empathic_professional",
                        )

        assert res["suggested_reply"].startswith("Dear Alice")
        assert res["customer_sentiment"] == "frustrated"
        assert len(res["suggested_actions"]) == 2
        assert res["suggested_actions"][0]["action_type"] == "status_change"
        assert res["suggested_actions"][1]["action_type"] == "priority_change"

    @pytest.mark.asyncio
    async def test_copilot_reply_rule_based_fallback(self):
        ticket_data = {
            "ticket": {
                "id": "TCK_FALLBACK_1",
                "company_id": "comp_1",
                "customer_email": "bob@example.com",
                "customer_name": "Bob",
                "subject": "Billing dispute",
                "status": "pending",
                "priority": "medium",
                "messages": [
                    {"direction": "inbound", "body_text": "I was double-charged on my credit card!"}
                ],
                "handoff_context": {
                    "customer_sentiment": "frustrated",
                    "conversation_summary": "Customer reports double-charge on billing.",
                    "suggested_actions": ["Review Stripe invoices and issue refund"],
                },
            },
            "attributed_chat": None,
        }

        # Mock LLM to throw an exception to exercise the resilient fallback
        with patch("app.services.company_email_service.get_ticket_with_chat", AsyncMock(return_value=ticket_data)):
            with patch("app.services.company_service.get_company", AsyncMock(return_value={"id": "comp_1"})):
                with patch("app.services.knowledge_service.search_knowledge", AsyncMock(return_value={"results": []})):
                    with patch("app.services.ticket_copilot_service.get_llm", side_effect=RuntimeError("Provider offline")):
                        res = await ticket_copilot_service.generate_copilot_reply(
                            company_id="comp_1",
                            ticket_id="TCK_FALLBACK_1",
                            tone="empathic_professional",
                            instruction="Offer a 20% future discount coupon.",
                        )

        assert "Hi Bob" in res["suggested_reply"]
        assert "apologize for the frustration" in res["suggested_reply"]
        assert "Offer a 20% future discount coupon." in res["suggested_reply"]
        assert res["customer_sentiment"] == "frustrated"

        # Check suggested actions in fallback
        action_types = [a["action_type"] for a in res["suggested_actions"]]
        assert "status_change" in action_types
        assert "priority_change" in action_types
        assert "internal_note" in action_types


class TestTicketCopilotActionExecution:
    @pytest.mark.asyncio
    async def test_execute_status_change(self):
        with patch.object(ticket_service, "transition_ticket", AsyncMock(return_value={"id": "TCK_1", "status": "awaiting_customer"})) as mock_trans:
            res = await ticket_copilot_service.execute_copilot_action(
                company_id="comp_1",
                ticket_id="TCK_1",
                action_type="status_change",
                parameters={"status": "awaiting_customer"},
                actor="agent_1",
                reason="Replied to customer",
            )

        assert res["success"] is True
        assert res["action_type"] == "status_change"
        mock_trans.assert_called_once_with(
            company_id="comp_1",
            ticket_id="TCK_1",
            to_status="awaiting_customer",
            actor="agent_1",
            note="Replied to customer",
        )

    @pytest.mark.asyncio
    async def test_execute_priority_change(self):
        with patch.object(ticket_service, "set_priority", AsyncMock(return_value={"id": "TCK_1", "priority": "urgent"})) as mock_prio:
            res = await ticket_copilot_service.execute_copilot_action(
                company_id="comp_1",
                ticket_id="TCK_1",
                action_type="priority_change",
                parameters={"priority": "urgent"},
                actor="agent_1",
            )

        assert res["success"] is True
        assert res["action_type"] == "priority_change"
        mock_prio.assert_called_once_with(
            company_id="comp_1",
            ticket_id="TCK_1",
            priority="urgent",
            actor="agent_1",
        )

    @pytest.mark.asyncio
    async def test_execute_internal_note(self):
        with patch.object(ticket_service, "add_internal_note", AsyncMock(return_value={"id": "TCK_1"})) as mock_note:
            res = await ticket_copilot_service.execute_copilot_action(
                company_id="comp_1",
                ticket_id="TCK_1",
                action_type="internal_note",
                parameters={"note": "Investigating billing log in Stripe"},
                actor="agent_1",
            )

        assert res["success"] is True
        assert res["action_type"] == "internal_note"
        mock_note.assert_called_once_with(
            company_id="comp_1",
            ticket_id="TCK_1",
            note="Investigating billing log in Stripe",
            actor="agent_1",
        )

    @pytest.mark.asyncio
    async def test_execute_unsupported_action(self):
        with pytest.raises(ValueError, match="Unsupported action type"):
            await ticket_copilot_service.execute_copilot_action(
                company_id="comp_1",
                ticket_id="TCK_1",
                action_type="random_invalid_action",
                parameters={},
                actor="agent_1",
            )
