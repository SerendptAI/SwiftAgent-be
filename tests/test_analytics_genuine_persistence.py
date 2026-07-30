import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from app.services import analytics_service
from app.core.auth import get_current_user
from app.core.database import get_database
from app.core.cache import TTLCache

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_analytics_cache():
    """Reset the analytics in-memory cache before each test."""
    analytics_service.analytics_cache = TTLCache(default_ttl=300)
    yield
    analytics_service.analytics_cache = TTLCache(default_ttl=300)


@pytest.mark.asyncio
async def test_record_conversation_resolution():
    """Test recording genuine resolution metadata on a widget conversation."""
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1)
        )
        mock_db.email_tickets.update_one = AsyncMock()

        await analytics_service.record_conversation_resolution(
            session_id="session_123",
            resolved_by="ai",
            escalated_to_human=False,
            fcr=True,
        )

        mock_db.widget_conversations.update_one.assert_called_once()
        args, kwargs = mock_db.widget_conversations.update_one.call_args
        assert args[0] == {"session_id": "session_123"}
        update_set = args[1]["$set"]
        assert update_set["resolved"] is True
        assert update_set["resolved_by"] == "ai"
        assert update_set["fcr"] is True
        assert update_set["escalated_to_human"] is False
        mock_db.email_tickets.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_record_ai_turn_metrics():
    """Test recording genuine AI turn latency, confidence, and KB lookup flag."""
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.update_one = AsyncMock()

        await analytics_service.record_ai_turn_metrics(
            session_id="session_456",
            generation_time_ms=850,
            confidence_score=0.94,
            kb_sources_cited=["doc_foo"],
            is_hallucinated=False,
        )

        mock_db.widget_conversations.update_one.assert_called_once()
        args, kwargs = mock_db.widget_conversations.update_one.call_args
        assert args[0] == {"session_id": "session_456"}
        update_set = args[1]["$set"]
        assert update_set["last_generation_time_ms"] == 850
        assert update_set["last_confidence_score"] == 0.94
        assert update_set["kb_lookup_performed"] is True
        assert update_set["is_hallucinated"] is False
        assert "ai_turns" in args[1]["$push"]


@pytest.mark.asyncio
async def test_record_conversation_intent():
    """Test recording semantic intent name and department on a session."""
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1)
        )

        await analytics_service.record_conversation_intent(
            session_id="session_789",
            intent_name="Password Reset & Recovery",
            department="IT Support",
        )

        mock_db.widget_conversations.update_one.assert_called_once()
        args, kwargs = mock_db.widget_conversations.update_one.call_args
        assert args[0] == {"session_id": "session_789"}
        update_set = args[1]["$set"]
        assert update_set["intent_name"] == "Password Reset & Recovery"
        assert update_set["department"] == "IT Support"


def test_submit_analytics_feedback_endpoint():
    """Test authenticated POST /api/v1/analytics/feedback."""
    mock_db = MagicMock()
    mock_db.customer_feedback.insert_one = AsyncMock()
    mock_db.widget_conversations.find_one = AsyncMock(return_value={"company_id": "company_acme"})
    mock_db.widget_conversations.update_one = AsyncMock()
    mock_db.email_tickets.find_one = AsyncMock(return_value=None)
    mock_db.email_tickets.update_one = AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: {"user_id": "usr_1", "company_id": "company_acme"}
    app.dependency_overrides[get_database] = lambda: mock_db
    try:
        payload = {
            "session_id": "chat_sess_1",
            "csat_score": 5.0,
            "nps_score": 10,
            "ces_score": 1,
            "sentiment": "positive",
            "feedback_theme": "Instant Resolution",
            "comment": "Amazing fast AI support!",
        }
        res = client.post("/api/v1/analytics/feedback", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert "feedback_id" in data

        mock_db.customer_feedback.insert_one.assert_called_once()
        insert_doc = mock_db.customer_feedback.insert_one.call_args[0][0]
        assert insert_doc["company_id"] == "company_acme"
        assert insert_doc["csat_score"] == 5.0
        assert insert_doc["sentiment"] == "positive"
        mock_db.widget_conversations.update_one.assert_called_once()
    finally:
        app.dependency_overrides.clear()


def test_submit_public_feedback_endpoint():
    """Test unauthenticated POST /api/v1/public/feedback."""
    mock_db = MagicMock()
    mock_db.customer_feedback.insert_one = AsyncMock()
    mock_db.widget_conversations.find_one = AsyncMock(return_value={"company_id": "public_acme"})
    mock_db.widget_conversations.update_one = AsyncMock()
    mock_db.email_tickets.find_one = AsyncMock(return_value=None)
    mock_db.email_tickets.update_one = AsyncMock()

    app.dependency_overrides[get_database] = lambda: mock_db
    try:
        payload = {
            "session_id": "pub_sess_99",
            "csat_score": 4.0,
            "sentiment": "positive",
        }
        res = client.post("/api/v1/public/feedback", json=payload)
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        mock_db.customer_feedback.insert_one.assert_called_once()
    finally:
        app.dependency_overrides.clear()


def test_submit_public_feedback_missing_ids():
    """Test POST /api/v1/public/feedback fails when neither session_id nor ticket_id provided."""
    mock_db = MagicMock()
    app.dependency_overrides[get_database] = lambda: mock_db
    try:
        payload = {
            "csat_score": 5.0,
        }
        res = client.post("/api/v1/public/feedback", json=payload)
        assert res.status_code == 400
        assert "Either session_id or ticket_id must be provided" in res.json()["detail"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_customer_experience_genuine_aggregation_overrides():
    """Test that get_customer_experience uses genuine aggregated DB averages."""
    with patch("app.services.analytics_service.db") as mock_db:
        # 10 chats in DB
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=10)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        # Mock aggregate to return genuine CSAT=4.9, NPS=85.0, CES=1.5
        def mock_aggregate(pipeline):
            field = str(pipeline)
            mock_cursor = MagicMock()
            if "csat_score" in field:
                mock_cursor.to_list = AsyncMock(return_value=[{"_id": None, "avg_csat": 4.9, "count": 10}])
            elif "nps_score" in field:
                mock_cursor.to_list = AsyncMock(return_value=[{"_id": None, "avg_nps": 85.0, "count": 10}])
            elif "ces_score" in field:
                mock_cursor.to_list = AsyncMock(return_value=[{"_id": None, "avg_ces": 1.5, "count": 10}])
            elif "feedback_theme" in field:
                mock_cursor.to_list = AsyncMock(return_value=[
                    {"_id": {"theme": "Real DB Theme", "sentiment": "POSITIVE"}, "count": 7}
                ])
            elif "sentiment" in field:
                mock_cursor.to_list = AsyncMock(return_value=[
                    {"_id": "positive", "count": 8},
                    {"_id": "neutral", "count": 2},
                ])
            else:
                mock_cursor.to_list = AsyncMock(return_value=[])
            return mock_cursor

        mock_db.widget_conversations.aggregate = MagicMock(side_effect=mock_aggregate)
        mock_db.customer_feedback.aggregate = MagicMock(side_effect=mock_aggregate)

        result = await analytics_service.get_customer_experience(days=30, company_id="acme")
        assert result.kpis.csat_score.value == 4.9
        assert result.kpis.nps_score.value == 85.0
        assert result.kpis.ces_score.value == 1.5
        assert result.top_feedback_themes[0].theme == "Real DB Theme"
