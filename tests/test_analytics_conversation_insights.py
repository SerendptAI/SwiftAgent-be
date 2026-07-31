import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from app.services import analytics_service
from app.core.auth import verify_analytics_secret_key
from app.core.cache import TTLCache

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_analytics_cache():
    """Reset the analytics in-memory cache before each test."""
    analytics_service.analytics_cache = TTLCache(default_ttl=300)
    yield
    analytics_service.analytics_cache = TTLCache(default_ttl=300)


@pytest.mark.asyncio
async def test_conversation_insights_fallback_data():
    """
    Test that when the database has zero conversations, get_conversation_insights
    returns complete fallback data matching all Figma mockup figures for Section 6.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        result = await analytics_service.get_conversation_insights(days=30)

        assert result.time_range == "LAST 30 DAYS"
        # Verify KPI Cards match exact Figma figures
        assert result.kpis.total_volume.value == 142847.0
        assert result.kpis.total_volume.change == 12.0
        assert result.kpis.total_volume.formatted == "142,847"

        assert result.kpis.top_intent.value == 18.2
        assert result.kpis.top_intent.formatted == "Password Reset (18.2%)"

        assert result.kpis.busiest_channel.value == 43.0
        assert result.kpis.busiest_channel.change == 4.0
        assert result.kpis.busiest_channel.formatted == "Live Chat (43%)"

        assert result.kpis.kb_usage_rate.value == 67.3
        assert result.kpis.kb_usage_rate.change == 5.1
        assert result.kpis.kb_usage_rate.formatted == "67.3%"

        assert result.kpis.system_uptime.value == 99.97
        assert result.kpis.system_uptime.change == 100.0
        assert result.kpis.system_uptime.formatted == "99.97%"

        # Verify 12-month Traffic Volume Trends
        assert len(result.traffic_trends) == 12
        assert result.traffic_trends[0].month == "Jan"
        assert result.traffic_trends[-1].month == "Dec"
        assert result.traffic_trends[-1].total_volume > 0

        # Verify Semantic Mapping (Top 5 Customer Intents)
        assert len(result.top_intents) == 5
        assert result.top_intents[0].rank == 1
        assert result.top_intents[0].intent_name == "Password Reset & Recovery"
        assert result.top_intents[0].volume == 26012
        assert result.top_intents[0].percentage == 18.2
        assert result.top_intents[0].formatted_label == "26,012 (18.2%)"

        assert result.top_intents[1].rank == 2
        assert result.top_intents[1].intent_name == "Subscription / Billing Issue"
        assert result.top_intents[1].volume == 21450
        assert result.top_intents[1].percentage == 15.0

        assert result.top_intents[2].rank == 3
        assert result.top_intents[2].intent_name == "Account Customization"
        assert result.top_intents[2].volume == 17141
        assert result.top_intents[2].percentage == 12.0

        assert result.top_intents[3].rank == 4
        assert result.top_intents[3].intent_name == "API Key Integration Help"
        assert result.top_intents[3].volume == 12500
        assert result.top_intents[3].percentage == 8.7

        assert result.top_intents[4].rank == 5
        assert result.top_intents[4].intent_name == "Webhook Configuration"
        assert result.top_intents[4].volume == 9200
        assert result.top_intents[4].percentage == 6.4

        # Verify Routing Analysis (Channel Distribution)
        assert result.channel_distribution.live_chat_percentage == 43.0
        assert result.channel_distribution.email_support_percentage == 32.1
        assert result.channel_distribution.ticketing_api_percentage == 24.9
        assert result.channel_distribution.busiest_channel_name == "Live Chat"
        assert result.channel_distribution.busiest_channel_percentage == 43.0


@pytest.mark.asyncio
async def test_conversation_insights_real_db_counts():
    """
    Test calculations when database has real conversation counts.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        async def mock_count(match):
            return 500

        mock_db.widget_conversations.count_documents = AsyncMock(side_effect=mock_count)
        mock_db.email_tickets.count_documents = AsyncMock(side_effect=mock_count)

        result = await analytics_service.get_conversation_insights(days=30, company_id="acme")

        assert result.time_range == "LAST 30 DAYS"
        assert result.kpis.total_volume.value == 1000.0
        assert len(result.top_intents) == 5
        assert result.channel_distribution.busiest_channel_name in ("Live Chat", "Email Support", "Ticketing / API")


@pytest.mark.asyncio
async def test_export_conversation_insights_csv_and_json():
    """
    Test CSV and JSON export output generation for Section 6.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        csv_content = await analytics_service.export_conversation_insights(format="csv", days=30)
        assert "SWIFT AGENTS - CONVERSATION INSIGHTS REPORT" in csv_content
        assert "Total Volume" in csv_content
        assert "142,847" in csv_content
        assert "SEMANTIC MAPPING - TOP 5 CUSTOMER INTENTS" in csv_content
        assert "ROUTING ANALYSIS - CHANNEL DISTRIBUTION" in csv_content

        json_content = await analytics_service.export_conversation_insights(format="json", days=30)
        assert '"total_volume"' in json_content
        assert '"Password Reset (18.2%)"' in json_content
        assert '43.0' in json_content


def test_conversation_insights_api_endpoints():
    """
    Test GET /api/v1/analytics/conversation-insights and export endpoint via HTTP TestClient.
    """
    app.dependency_overrides[verify_analytics_secret_key] = lambda: {"auth_type": "company_api_key", "company_id": "acme"}
    try:
        with patch("app.services.analytics_service.db") as mock_db:
            mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
            mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

            # Test main JSON dashboard endpoint
            response = client.get("/api/v1/analytics/conversation-insights?days=30")
            assert response.status_code == 200
            assert "Cache-Control" in response.headers
            assert "private, max-age=60" in response.headers["Cache-Control"]

            data = response.json()
            assert data["time_range"] == "LAST 30 DAYS"
            assert data["kpis"]["total_volume"]["value"] == 142847.0
            assert data["kpis"]["top_intent"]["formatted"] == "Password Reset (18.2%)"
            assert len(data["traffic_trends"]) == 12
            assert len(data["top_intents"]) == 5
            assert data["channel_distribution"]["live_chat_percentage"] == 43.0

            # Test CSV export endpoint
            response_csv = client.get("/api/v1/analytics/conversation-insights/export?format=csv")
            assert response_csv.status_code == 200
            assert "text/csv" in response_csv.headers["content-type"]
            assert "SWIFT AGENTS - CONVERSATION INSIGHTS REPORT" in response_csv.text

            # Test JSON export endpoint
            response_json = client.get("/api/v1/analytics/conversation-insights/export?format=json")
            assert response_json.status_code == 200
            assert "total_volume" in response_json.text
    finally:
        app.dependency_overrides.clear()
