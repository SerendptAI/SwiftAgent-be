"""Tests for Structured Handoff Briefing formatting, priority calculation,
and enhanced ticket escalation payloads.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from app.services.handoff_service import (
    AIAttemptedSolution,
    HandoffContext,
    format_structured_briefing,
    calculate_escalation_priority,
    create_enhanced_handoff,
)
from app.services import ticket_service


NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestStructuredHandoffBriefing:
    def test_format_structured_briefing_with_pydantic_model(self):
        ctx = HandoffContext(
            session_id="sess_123",
            company_id="comp_1",
            customer_email="jane@example.com",
            escalation_reason="human_request",
            escalation_reason_text="Customer requested human agent",
            conversation_summary="Customer inquired about resetting account password.",
            message_count=4,
            user_message_count=2,
            conversation_duration_minutes=3.5,
            ai_attempted_solutions=[
                AIAttemptedSolution(
                    tool_used="search_knowledge_base",
                    tool_input="password reset",
                    result_summary="No matching articles found",
                    success=False,
                    error="ArticleNotFoundException",
                )
            ],
            ai_failure_points=["AI gave fallback response 1 time(s)"],
            customer_sentiment="frustrated",
            customer_language="en",
            page_url="https://app.example.com/settings/security",
            intent="knowledge",
            suggested_actions=["Verify customer email and guide through reset portal."],
            escalated_at=NOW,
        )

        briefing = format_structured_briefing(ctx)

        # Verify [Customer Intent] section
        assert "### 📋 AI Handoff Briefing" in briefing
        assert "🎯 **Customer Intent**" in briefing
        assert "Customer inquired about resetting account password." in briefing
        assert "`knowledge`" in briefing
        assert "FRUSTRATED" in briefing
        assert "Customer requested human agent" in briefing
        assert "https://app.example.com/settings/security" in briefing

        # Verify [Failed Steps/Friction] section
        assert "⚠️ **Failed Steps & Friction**" in briefing
        assert "AI gave fallback response 1 time(s)" in briefing
        assert "❌ Tool `search_knowledge_base` failed (Error: ArticleNotFoundException)" in briefing

        # Verify [Suggested Action] section
        assert "👉 **Suggested Next Actions**" in briefing
        assert "1. Verify customer email and guide through reset portal." in briefing

    def test_format_structured_briefing_with_dict(self):
        ctx_dict = {
            "session_id": "sess_456",
            "company_id": "comp_1",
            "intent": "navigation",
            "customer_sentiment": "negative",
            "conversation_summary": "Customer unable to locate billing invoice.",
            "escalation_reason": "repeated_ai_failures",
            "escalation_reason_text": "AI failed multiple times",
            "ai_failure_points": ["Customer sent multiple messages without resolution"],
            "ai_attempted_solutions": [
                {
                    "tool_used": "get_dashboard_navigation",
                    "success": True,
                    "result_summary": "Returned route to billing tab",
                }
            ],
            "suggested_actions": ["Provide direct link to /billing/invoices."],
        }

        briefing = format_structured_briefing(ctx_dict)

        assert "🎯 **Customer Intent**" in briefing
        assert "Customer unable to locate billing invoice." in briefing
        assert "`navigation`" in briefing
        assert "NEGATIVE" in briefing
        assert "AI failed multiple times" in briefing
        assert "⚠️ Customer sent multiple messages without resolution" in briefing
        assert "ℹ️ Tool `get_dashboard_navigation`: Returned route to billing tab" in briefing
        assert "1. Provide direct link to /billing/invoices." in briefing

    def test_calculate_escalation_priority(self):
        # Frustrated -> urgent
        p_frustrated = calculate_escalation_priority({"customer_sentiment": "frustrated"})
        assert p_frustrated == "urgent"

        # 2+ failed attempts -> urgent
        p_multi_fail = calculate_escalation_priority({
            "customer_sentiment": "neutral",
            "ai_attempted_solutions": [
                {"tool_used": "t1", "success": False},
                {"tool_used": "t2", "success": False},
            ]
        })
        assert p_multi_fail == "urgent"

        # Negative -> high
        p_negative = calculate_escalation_priority({"customer_sentiment": "negative"})
        assert p_negative == "high"

        # idle_unanswered -> high
        p_idle = calculate_escalation_priority({"escalation_reason": "idle_unanswered"})
        assert p_idle == "high"

        # Positive + human_request -> low
        p_low = calculate_escalation_priority({
            "customer_sentiment": "positive",
            "escalation_reason": "human_request"
        })
        assert p_low == "low"

        # Default fallback
        p_default = calculate_escalation_priority({"customer_sentiment": "neutral"}, default_priority="medium")
        assert p_default == "medium"

    @pytest.mark.asyncio
    async def test_auto_escalate_creates_ticket_with_structured_briefing(self):
        chat = {
            "session_id": "sess_escalate_briefing",
            "company_id": "comp_1",
            "sdk_user_email": "user@example.com",
            "subject": "Help with checkout",
            "page_url": "https://store.example.com/cart",
            "intent": "navigation",
            "messages": [
                {"role": "user", "content": "The checkout page is broken and I hate this."},
                {"role": "assistant", "content": "I'm sorry, I wasn't able to find that information right now."},
            ],
            "updated_at": NOW,
            "escalated": False,
        }

        async def mock_cursor_iter(items):
            for item in items:
                yield item

        mock_db = MagicMock()
        mock_db.email_tickets = MagicMock()
        mock_db.users = MagicMock()
        mock_db.companies = MagicMock()
        cursor = MagicMock()
        cursor.__aiter__.side_effect = lambda *args: mock_cursor_iter([chat])
        mock_db.widget_conversations.find = MagicMock(return_value=cursor)

        create_ticket_mock = AsyncMock(return_value={"id": "TICKET99"})
        with patch.object(ticket_service, "db", mock_db):
            with patch.object(ticket_service, "company_email_service") as email_svc:
                email_svc.create_ticket = create_ticket_mock
                with patch.object(ticket_service, "_utcnow", return_value=NOW):
                    result = await ticket_service.auto_escalate_chats("comp_1")

        assert len(result) == 1
        assert result[0]["ticket_id"] == "TICKET99"
        assert result[0]["session_id"] == "sess_escalate_briefing"

        call_kwargs = create_ticket_mock.call_args.kwargs
        chat_summary = call_kwargs["chat_summary"]
        handoff_ctx = call_kwargs["handoff_context"]

        # Assert briefing structure
        assert "### 📋 AI Handoff Briefing" in chat_summary
        assert "🎯 **Customer Intent**" in chat_summary
        assert "⚠️ **Failed Steps & Friction**" in chat_summary
        assert "👉 **Suggested Next Actions**" in chat_summary
        assert "---" in chat_summary
        assert "### 💬 Full Transcript" in chat_summary

        # Assert handoff_context passed to ticket creation
        assert handoff_ctx is not None
        assert handoff_ctx["session_id"] == "sess_escalate_briefing"
        assert handoff_ctx["customer_sentiment"] in ("negative", "frustrated")
        # Priority should be calibrated
        assert call_kwargs["priority"] in ("high", "urgent")
