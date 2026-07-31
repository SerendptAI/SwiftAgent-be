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
async def test_customer_experience_fallback_data():
    """
    Test that when the database has zero conversations, get_customer_experience
    returns complete fallback data matching all Figma mockup figures for Section 4.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        result = await analytics_service.get_customer_experience(days=30)

        assert result.time_range == "LAST 30 DAYS"
        # Verify KPI Cards match exact Figma figures
        assert result.kpis.csat_score.value == 4.6
        assert result.kpis.csat_score.change == 0.2
        assert result.kpis.csat_score.formatted == "4.6 / 5.0"

        assert result.kpis.positive_sentiment.value == 78.0
        assert result.kpis.positive_sentiment.change == 3.0
        assert result.kpis.positive_sentiment.formatted == "78.0%"

        assert result.kpis.nps_score.value == 62.0
        assert result.kpis.nps_score.change == 5.0
        assert result.kpis.nps_score.formatted == "62"

        assert result.kpis.ces_score.value == 2.1
        assert result.kpis.ces_score.change == -0.3
        assert result.kpis.ces_score.formatted == "2.1 / 5.0"

        assert result.kpis.feedback_volume.value == 12847.0
        assert result.kpis.feedback_volume.change == 15.0
        assert result.kpis.feedback_volume.formatted == "12,847"

        # Verify 12-month historical trends
        assert len(result.csat_nps_trends) == 12
        assert result.csat_nps_trends[0].month == "Jan"
        assert result.csat_nps_trends[-1].month == "Dec"
        assert result.csat_nps_trends[-1].csat_score == 4.6
        assert result.csat_nps_trends[-1].nps_score == 62.0

        # Verify Customer Sentiment Breakdown donut chart
        assert result.sentiment_breakdown.positive_percentage == 78.0
        assert result.sentiment_breakdown.neutral_percentage == 14.5
        assert result.sentiment_breakdown.negative_percentage == 7.5
        assert result.sentiment_breakdown.total_feedback_count == 12847

        # Verify Top Feedback Themes table
        assert len(result.top_feedback_themes) == 4
        assert result.top_feedback_themes[0].theme == "Instant Resolution / No Queue"
        assert result.top_feedback_themes[0].mentions_formatted == "3,142 mentions"
        assert result.top_feedback_themes[0].sentiment == "POSITIVE"


@pytest.mark.asyncio
async def test_customer_experience_real_db_counts():
    """
    Test calculations when database has real conversation counts.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        async def mock_count(match):
            if "escalated" in match:
                return 80  # 80 autonomous out of 100
            return 100

        mock_db.widget_conversations.count_documents = AsyncMock(side_effect=mock_count)
        mock_db.email_tickets.count_documents = AsyncMock(side_effect=mock_count)

        result = await analytics_service.get_customer_experience(days=30, company_id="acme")

        assert result.time_range == "LAST 30 DAYS"
        assert result.kpis.csat_score.value > 0.0
        assert result.kpis.positive_sentiment.value > 0.0
        assert len(result.top_feedback_themes) == 4


@pytest.mark.asyncio
async def test_export_customer_experience_csv_and_json():
    """
    Test CSV and JSON export output generation for Section 4.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        csv_content = await analytics_service.export_customer_experience(format="csv", days=30)
        assert "SWIFT AGENTS - CUSTOMER EXPERIENCE REPORT" in csv_content
        assert "CSAT Score" in csv_content
        assert "4.6 / 5.0" in csv_content
        assert "CUSTOMER SENTIMENT BREAKDOWN" in csv_content
        assert "TOP FEEDBACK THEMES" in csv_content

        json_content = await analytics_service.export_customer_experience(format="json", days=30)
        assert '"csat_score"' in json_content
        assert '62.0' in json_content
        assert '"78.0%"' in json_content


def test_customer_experience_api_endpoints():
    """
    Test GET /api/v1/analytics/customer-experience and export endpoint via HTTP TestClient.
    """
    app.dependency_overrides[verify_analytics_secret_key] = lambda: {"auth_type": "company_api_key", "company_id": "acme"}
    try:
        with patch("app.services.analytics_service.db") as mock_db:
            mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
            mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

            # Test main JSON dashboard endpoint
            response = client.get("/api/v1/analytics/customer-experience?days=30")
            assert response.status_code == 200
            assert "Cache-Control" in response.headers
            assert "private, max-age=60" in response.headers["Cache-Control"]

            data = response.json()
            assert data["time_range"] == "LAST 30 DAYS"
            assert data["kpis"]["csat_score"]["value"] == 4.6
            assert data["kpis"]["positive_sentiment"]["value"] == 78.0
            assert len(data["csat_nps_trends"]) == 12
            assert data["sentiment_breakdown"]["positive_percentage"] == 78.0
            assert len(data["top_feedback_themes"]) == 4

            # Test CSV export endpoint
            response_csv = client.get("/api/v1/analytics/customer-experience/export?format=csv")
            assert response_csv.status_code == 200
            assert "text/csv" in response_csv.headers["content-type"]
            assert "SWIFT AGENTS - CUSTOMER EXPERIENCE REPORT" in response_csv.text

            # Test JSON export endpoint
            response_json = client.get("/api/v1/analytics/customer-experience/export?format=json")
            assert response_json.status_code == 200
            assert "csat_score" in response_json.text
    finally:
        app.dependency_overrides.clear()
