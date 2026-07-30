import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient

from main import app
from app.services import analytics_service
from app.core.auth import get_current_user
from app.core.cache import TTLCache

client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_analytics_cache():
    """Reset the analytics in-memory cache before each test."""
    analytics_service.analytics_cache = TTLCache(default_ttl=300)
    yield
    analytics_service.analytics_cache = TTLCache(default_ttl=300)


@pytest.mark.asyncio
async def test_resolution_performance_fallback_data():
    """
    Test that when the database has zero conversations, get_resolution_performance
    returns complete fallback data matching all Figma mockup figures for Section 2.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)
        mock_db.form_submissions.count_documents = AsyncMock(return_value=0)

        result = await analytics_service.get_resolution_performance(days=30)

        assert result.time_range == "LAST 30 DAYS"
        # Verify KPI Cards match exact Figma figures
        assert result.kpis.arr_trend.value == 82.1
        assert result.kpis.arr_trend.change == 2.4
        assert result.kpis.arr_trend.formatted == "82.1%"

        assert result.kpis.escalation_rate.value == 17.9
        assert result.kpis.escalation_rate.change == -1.8
        assert result.kpis.escalation_rate.formatted == "17.9%"

        assert result.kpis.first_contact_resolution.value == 71.3
        assert result.kpis.first_contact_resolution.change == 0.9
        assert result.kpis.first_contact_resolution.formatted == "71.3%"

        assert result.kpis.avg_resolution_time.value == 4.2
        assert result.kpis.avg_resolution_time.change == -12.0
        assert result.kpis.avg_resolution_time.formatted == "4.2 min"

        assert result.kpis.repeat_contact_rate.value == 8.7
        assert result.kpis.repeat_contact_rate.change == -0.5
        assert result.kpis.repeat_contact_rate.formatted == "8.7%"

        # Verify 12-month historical trends
        assert len(result.historical_trends) == 12
        assert result.historical_trends[0].month == "Jan"
        assert result.historical_trends[-1].month == "Dec"
        assert result.historical_trends[-1].arr == 82.1

        # Verify 4-week outcome breakdown
        assert len(result.weekly_breakdown) == 4
        assert result.weekly_breakdown[0].week == "Week 1"
        assert result.weekly_breakdown[-1].week == "Week 4"
        assert result.weekly_breakdown[-1].ai_resolved_percentage == 81.4

        # Verify 4 channels table
        assert len(result.channel_metrics) == 4
        assert result.channel_metrics[0].channel == "Live Chat"
        assert result.channel_metrics[0].avg_time_formatted == "1.8 min"
        assert result.channel_metrics[0].volume_formatted == "64,250"


@pytest.mark.asyncio
async def test_resolution_performance_real_db_counts():
    """
    Test formulas for ARR trend, escalation rate, FCR, and weekly breakdown
    when DB returns non-zero conversation counts.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        async def mock_count(match):
            if "escalated" in match:
                return 80  # 80 autonomous out of 100
            if "status" in match:
                return 80
            return 100

        mock_db.widget_conversations.count_documents = AsyncMock(side_effect=mock_count)
        mock_db.email_tickets.count_documents = AsyncMock(side_effect=mock_count)
        mock_db.form_submissions.count_documents = AsyncMock(side_effect=mock_count)

        result = await analytics_service.get_resolution_performance(days=30, company_id="acme")

        assert result.time_range == "LAST 30 DAYS"
        assert result.kpis.arr_trend.value > 0.0
        assert result.kpis.escalation_rate.value >= 0.0
        assert len(result.weekly_breakdown) == 4


@pytest.mark.asyncio
async def test_export_resolution_performance_csv_and_json():
    """
    Test CSV and JSON export output generation for Section 2.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)
        mock_db.form_submissions.count_documents = AsyncMock(return_value=0)

        csv_content = await analytics_service.export_resolution_performance(format="csv", days=30)
        assert "SWIFT AGENTS - RESOLUTION PERFORMANCE REPORT" in csv_content
        assert "ARR Trend" in csv_content
        assert "82.1%" in csv_content
        assert "Escalation Rate" in csv_content
        assert "17.9%" in csv_content
        assert "WEEKLY BREAKDOWN (OUTCOMES)" in csv_content

        json_content = await analytics_service.export_resolution_performance(format="json", days=30)
        assert '"arr_trend"' in json_content
        assert '82.1' in json_content
        assert '"82.1%"' in json_content


def test_resolution_performance_api_endpoints():
    """
    Test GET /api/v1/analytics/resolution-performance and export endpoint via HTTP TestClient.
    """
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "test", "company_id": "acme"}
    try:
        with patch("app.services.analytics_service.db") as mock_db:
            mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
            mock_db.email_tickets.count_documents = AsyncMock(return_value=0)
            mock_db.form_submissions.count_documents = AsyncMock(return_value=0)

            # Test main JSON dashboard endpoint
            response = client.get("/api/v1/analytics/resolution-performance?days=30")
            assert response.status_code == 200
            assert "Cache-Control" in response.headers
            assert "private, max-age=60" in response.headers["Cache-Control"]

            data = response.json()
            assert data["time_range"] == "LAST 30 DAYS"
            assert data["kpis"]["arr_trend"]["value"] == 82.1
            assert data["kpis"]["escalation_rate"]["value"] == 17.9
            assert len(data["historical_trends"]) == 12
            assert len(data["weekly_breakdown"]) == 4
            assert len(data["channel_metrics"]) == 4

            # Test CSV export endpoint
            response_csv = client.get("/api/v1/analytics/resolution-performance/export?format=csv")
            assert response_csv.status_code == 200
            assert "text/csv" in response_csv.headers["content-type"]
            assert "SWIFT AGENTS - RESOLUTION PERFORMANCE REPORT" in response_csv.text

            # Test JSON export endpoint
            response_json = client.get("/api/v1/analytics/resolution-performance/export?format=json")
            assert response_json.status_code == 200
            assert "arr_trend" in response_json.text
    finally:
        app.dependency_overrides.clear()
