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
async def test_business_impact_fallback_data():
    """
    Test that when the database has zero conversations, get_business_impact
    returns complete fallback data matching all Figma mockup figures for Section 5.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        result = await analytics_service.get_business_impact(days=30)

        assert result.time_range == "LAST 30 DAYS"
        # Verify KPI Cards match exact Figma figures
        assert result.kpis.human_hours_saved.value == 2340.0
        assert result.kpis.human_hours_saved.change == 18.0
        assert result.kpis.human_hours_saved.formatted == "2,340 hrs"

        assert result.kpis.estimated_cost_saved.value == 187200.0
        assert result.kpis.estimated_cost_saved.change == 22.0
        assert result.kpis.estimated_cost_saved.formatted == "$187,200"

        assert result.kpis.roi_metric.value == 340.0
        assert result.kpis.roi_metric.change == 15.0
        assert result.kpis.roi_metric.formatted == "340%"

        assert result.kpis.fte_equivalent_saved.value == 14.6
        assert result.kpis.fte_equivalent_saved.change == 2.1
        assert result.kpis.fte_equivalent_saved.formatted == "14.6 FTE"

        assert result.kpis.sla_compliance.value == 96.8
        assert result.kpis.sla_compliance.change == 1.2
        assert result.kpis.sla_compliance.formatted == "96.8%"

        # Verify 12-month Cumulative Savings Timeline
        assert len(result.savings_timeline) == 12
        assert result.savings_timeline[0].month == "Jan"
        assert result.savings_timeline[-1].month == "Dec"
        assert result.savings_timeline[-1].hours_saved == 234.0
        assert result.savings_timeline[-1].cost_saved == 18720.0
        assert result.savings_timeline[-1].cost_saved_k == 18.72

        # Verify Organizational Impact (Savings by Department)
        assert len(result.department_savings) == 4
        assert result.department_savings[0].department == "Customer Support"
        assert result.department_savings[0].saved_amount == 94500.0
        assert result.department_savings[0].saved_formatted == "$94,500 Saved"
        assert result.department_savings[0].percentage == 50.5

        assert result.department_savings[1].department == "Sales & Enablement"
        assert result.department_savings[1].saved_amount == 48120.0
        assert result.department_savings[1].saved_formatted == "$48,120 Saved"

        assert result.department_savings[2].department == "Business Operations"
        assert result.department_savings[2].saved_amount == 32400.0
        assert result.department_savings[2].saved_formatted == "$32,400 Saved"

        assert result.department_savings[3].department == "Product & Engineering"
        assert result.department_savings[3].saved_amount == 12180.0
        assert result.department_savings[3].saved_formatted == "$12,180 Saved"

        # Verify Efficiency Audit (ROI Calculator Summary)
        assert result.roi_summary.total_return == 241800.0
        assert result.roi_summary.total_return_formatted == "$241,800"
        assert result.roi_summary.initial_investment == 54600.0
        assert result.roi_summary.initial_investment_formatted == "On $54,600 Initial Investment"
        assert result.roi_summary.net_savings == 187200.0
        assert result.roi_summary.net_savings_formatted == "$187,200"
        assert result.roi_summary.saas_platform_license == 32000.0
        assert result.roi_summary.saas_platform_license_formatted == "$32,000"
        assert result.roi_summary.ops_and_maintenance == 22600.0
        assert result.roi_summary.ops_and_maintenance_formatted == "$22,600"
        assert result.roi_summary.roi_percentage == 340.0


@pytest.mark.asyncio
async def test_business_impact_real_db_counts():
    """
    Test calculations when database has real conversation counts.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        async def mock_count(match):
            if "escalated" in match:
                return 400
            return 500

        mock_db.widget_conversations.count_documents = AsyncMock(side_effect=mock_count)
        mock_db.email_tickets.count_documents = AsyncMock(side_effect=mock_count)

        result = await analytics_service.get_business_impact(days=30, company_id="acme")

        assert result.time_range == "LAST 30 DAYS"
        assert result.kpis.human_hours_saved.value > 0.0
        assert result.kpis.estimated_cost_saved.value > 0.0
        assert len(result.department_savings) == 4
        assert result.roi_summary.roi_percentage > 0.0


@pytest.mark.asyncio
async def test_export_business_impact_csv_and_json():
    """
    Test CSV and JSON export output generation for Section 5.
    """
    with patch("app.services.analytics_service.db") as mock_db:
        mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
        mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

        csv_content = await analytics_service.export_business_impact(format="csv", days=30)
        assert "SWIFT AGENTS - BUSINESS IMPACT REPORT" in csv_content
        assert "Human Hours Saved" in csv_content
        assert "2,340 hrs" in csv_content
        assert "ORGANIZATIONAL IMPACT - SAVINGS BY DEPARTMENT" in csv_content
        assert "EFFICIENCY AUDIT - ROI CALCULATOR SUMMARY" in csv_content

        json_content = await analytics_service.export_business_impact(format="json", days=30)
        assert '"human_hours_saved"' in json_content
        assert '187200.0' in json_content
        assert '"$241,800"' in json_content


def test_business_impact_api_endpoints():
    """
    Test GET /api/v1/analytics/business-impact and export endpoint via HTTP TestClient.
    """
    app.dependency_overrides[get_current_user] = lambda: {"user_id": "test", "company_id": "acme"}
    try:
        with patch("app.services.analytics_service.db") as mock_db:
            mock_db.widget_conversations.count_documents = AsyncMock(return_value=0)
            mock_db.email_tickets.count_documents = AsyncMock(return_value=0)

            # Test main JSON dashboard endpoint
            response = client.get("/api/v1/analytics/business-impact?days=30")
            assert response.status_code == 200
            assert "Cache-Control" in response.headers
            assert "private, max-age=60" in response.headers["Cache-Control"]

            data = response.json()
            assert data["time_range"] == "LAST 30 DAYS"
            assert data["kpis"]["human_hours_saved"]["value"] == 2340.0
            assert data["kpis"]["estimated_cost_saved"]["value"] == 187200.0
            assert len(data["savings_timeline"]) == 12
            assert len(data["department_savings"]) == 4
            assert data["roi_summary"]["total_return"] == 241800.0

            # Test CSV export endpoint
            response_csv = client.get("/api/v1/analytics/business-impact/export?format=csv")
            assert response_csv.status_code == 200
            assert "text/csv" in response_csv.headers["content-type"]
            assert "SWIFT AGENTS - BUSINESS IMPACT REPORT" in response_csv.text

            # Test JSON export endpoint
            response_json = client.get("/api/v1/analytics/business-impact/export?format=json")
            assert response_json.status_code == 200
            assert "human_hours_saved" in response_json.text
    finally:
        app.dependency_overrides.clear()
