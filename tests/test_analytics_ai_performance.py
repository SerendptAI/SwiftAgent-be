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
async def test_ai_performance_fallback_data():
    """
    Test that when the database has zero conversations, get_ai_performance
    returns complete fallback data matching all Figma mockup figures for Section 3.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        result = await analytics_service.get_ai_performance(days=30)

        assert result.time_range == "LAST 30 DAYS"
        # Verify KPI Cards match exact Figma figures
        assert result.kpis.ai_accuracy.value == 94.2
        assert result.kpis.ai_accuracy.change == 1.1
        assert result.kpis.ai_accuracy.formatted == "94.2%"

        assert result.kpis.confidence_score.value == 87.6
        assert result.kpis.confidence_score.change == 2.3
        assert result.kpis.confidence_score.formatted == "87.6"

        assert result.kpis.avg_response_time.value == 1.8
        assert result.kpis.avg_response_time.change == -0.4
        assert result.kpis.avg_response_time.formatted == "1.8s"

        assert result.kpis.hallucination_rate.value == 0.8
        assert result.kpis.hallucination_rate.change == -0.2
        assert result.kpis.hallucination_rate.formatted == "0.8%"

        assert result.kpis.low_confidence_responses.value == 4.1
        assert result.kpis.low_confidence_responses.change == -0.6
        assert result.kpis.low_confidence_responses.formatted == "4.1%"

        # Verify 12-month historical trends
        assert len(result.accuracy_trends) == 12
        assert result.accuracy_trends[0].month == "Jan"
        assert result.accuracy_trends[-1].month == "Dec"
        assert result.accuracy_trends[-1].accuracy == 94.2
        assert result.accuracy_trends[-1].confidence_score == 87.6

        # Verify 5 latency distribution brackets
        assert len(result.latency_distribution) == 5
        assert result.latency_distribution[0].bucket == "< 1s"
        assert result.latency_distribution[0].percentage == 65.0
        assert result.latency_distribution[1].bucket == "1 - 3s"
        assert result.latency_distribution[1].percentage == 82.0

        # Verify confidence level distribution donut
        assert result.confidence_distribution.high_percentage == 68.4
        assert result.confidence_distribution.medium_percentage == 22.1
        assert result.confidence_distribution.low_percentage == 9.5
        assert result.confidence_distribution.avg_confidence == 87.6


@pytest.mark.asyncio
async def test_ai_performance_real_db_counts():
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

        result = await analytics_service.get_ai_performance(days=30, company_id="acme")

        assert result.time_range == "LAST 30 DAYS"
        assert result.kpis.ai_accuracy.value > 0.0
        assert result.kpis.confidence_score.value > 0.0
        assert len(result.latency_distribution) == 5


@pytest.mark.asyncio
async def test_export_ai_performance_csv_and_json():
    """
    Test CSV and JSON export output generation for Section 3.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        csv_content = await analytics_service.export_ai_performance(format="csv", days=30)
        assert "SWIFT AGENTS - AI PERFORMANCE REPORT" in csv_content
        assert "AI Accuracy" in csv_content
        assert "94.2%" in csv_content
        assert "Confidence Score" in csv_content
        assert "RESPONSE TIME DISTRIBUTION" in csv_content
        assert "CONFIDENCE LEVEL DISTRIBUTION" in csv_content

        json_content = await analytics_service.export_ai_performance(format="json", days=30)
        assert '"ai_accuracy"' in json_content
        assert '94.2' in json_content
        assert '"94.2%"' in json_content


def test_ai_performance_api_endpoints():
    """
    Test GET /api/v1/analytics/ai-performance and export endpoint via HTTP TestClient.
    """
    app.dependency_overrides[verify_analytics_secret_key] = lambda: {"auth_type": "company_api_key", "company_id": "acme"}
    try:
        with patch("app.services.analytics_service.db") as mock_db:
            mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
            mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

            # Test main JSON dashboard endpoint
            response = client.get("/api/v1/analytics/ai-performance?days=30")
            assert response.status_code == 200
            assert "Cache-Control" in response.headers
            assert "private, max-age=60" in response.headers["Cache-Control"]

            data = response.json()
            assert data["time_range"] == "LAST 30 DAYS"
            assert data["kpis"]["ai_accuracy"]["value"] == 94.2
            assert data["kpis"]["confidence_score"]["value"] == 87.6
            assert len(data["accuracy_trends"]) == 12
            assert len(data["latency_distribution"]) == 5
            assert data["confidence_distribution"]["high_percentage"] == 68.4

            # Test CSV export endpoint
            response_csv = client.get("/api/v1/analytics/ai-performance/export?format=csv")
            assert response_csv.status_code == 200
            assert "text/csv" in response_csv.headers["content-type"]
            assert "SWIFT AGENTS - AI PERFORMANCE REPORT" in response_csv.text

            # Test JSON export endpoint
            response_json = client.get("/api/v1/analytics/ai-performance/export?format=json")
            assert response_json.status_code == 200
            assert "ai_accuracy" in response_json.text
    finally:
        app.dependency_overrides.clear()
