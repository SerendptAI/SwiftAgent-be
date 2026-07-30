from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.core.auth import get_current_user
from app.models.analytics_models import (
    ExecutiveSummaryResponse,
    ResolutionPerformanceResponse,
    AiPerformanceResponse,
    CustomerExperienceResponse,
    BusinessImpactResponse,
    ConversationInsightsResponse,
)
from app.services import analytics_service

router = APIRouter(tags=["Analytics Dashboard"])


@router.get("/executive-summary", response_model=ExecutiveSummaryResponse)
async def get_executive_summary(
    response: Response,
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter. Omitting returns platform-wide aggregate."),
    current_user: dict = Depends(get_current_user),
):
    """
    Get all Executive Summary metrics, KPIs, historical ARR, channel load distribution,
    direct triage split, and top companies table for the Analytics Dashboard.
    Includes 5-minute server-side TTL caching and HTTP Cache-Control headers.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    try:
        return await analytics_service.get_executive_summary(
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch executive summary: {str(e)}")


@router.get("/executive-summary/export")
async def export_executive_summary(
    format: str = Query(default="csv", description="Export format: 'csv' or 'json'"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter"),
    current_user: dict = Depends(get_current_user),
):
    """
    Export the Executive Summary report as a downloadable CSV or JSON file.
    """
    if format.lower() not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    try:
        content = await analytics_service.export_executive_summary(
            format=format,
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export executive summary: {str(e)}")

    if format.lower() == "json":
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=executive-summary.json"},
        )
    else:
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=executive-summary.csv"},
        )


@router.get("/resolution-performance", response_model=ResolutionPerformanceResponse)
async def get_resolution_performance(
    response: Response,
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter. Omitting returns platform-wide aggregate."),
    current_user: dict = Depends(get_current_user),
):
    """
    Get Section 2 Resolution Performance metrics, KPI cards, historical trend chart,
    weekly outcomes breakdown, and channel average resolution times.
    Includes 5-minute server-side TTL caching and HTTP Cache-Control headers.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    try:
        return await analytics_service.get_resolution_performance(
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch resolution performance: {str(e)}")


@router.get("/resolution-performance/export")
async def export_resolution_performance(
    format: str = Query(default="csv", description="Export format: 'csv' or 'json'"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter"),
    current_user: dict = Depends(get_current_user),
):
    """
    Export the Section 2 Resolution Performance report as a downloadable CSV or JSON file.
    """
    if format.lower() not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    try:
        content = await analytics_service.export_resolution_performance(
            format=format,
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export resolution performance: {str(e)}")

    if format.lower() == "json":
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=resolution-performance.json"},
        )
    else:
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=resolution-performance.csv"},
        )


@router.get("/ai-performance", response_model=AiPerformanceResponse)
async def get_ai_performance(
    response: Response,
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter. Omitting returns platform-wide aggregate."),
    current_user: dict = Depends(get_current_user),
):
    """
    Get Section 3 AI Performance metrics, KPI cards, 12-month accuracy/confidence trends,
    latency distribution bar chart, and model health confidence level distribution.
    Includes 5-minute server-side TTL caching and HTTP Cache-Control headers.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    try:
        return await analytics_service.get_ai_performance(
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch AI performance: {str(e)}")


@router.get("/ai-performance/export")
async def export_ai_performance(
    format: str = Query(default="csv", description="Export format: 'csv' or 'json'"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter"),
    current_user: dict = Depends(get_current_user),
):
    """
    Export the Section 3 AI Performance report as a downloadable CSV or JSON file.
    """
    if format.lower() not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    try:
        content = await analytics_service.export_ai_performance(
            format=format,
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export AI performance: {str(e)}")

    if format.lower() == "json":
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=ai-performance.json"},
        )
    else:
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=ai-performance.csv"},
        )


@router.get("/customer-experience", response_model=CustomerExperienceResponse)
async def get_customer_experience(
    response: Response,
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter. Omitting returns platform-wide aggregate."),
    current_user: dict = Depends(get_current_user),
):
    """
    Get Section 4 Customer Experience metrics, KPI cards (CSAT, Positive Sentiment,
    NPS, CES, Feedback Volume), 12-month CSAT & NPS trends, sentiment donut breakdown,
    and top feedback themes list. Includes 5-minute TTL caching and HTTP Cache-Control headers.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    try:
        return await analytics_service.get_customer_experience(
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch customer experience: {str(e)}")


@router.get("/customer-experience/export")
async def export_customer_experience(
    format: str = Query(default="csv", description="Export format: 'csv' or 'json'"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter"),
    current_user: dict = Depends(get_current_user),
):
    """
    Export the Section 4 Customer Experience report as a downloadable CSV or JSON file.
    """
    if format.lower() not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    try:
        content = await analytics_service.export_customer_experience(
            format=format,
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export customer experience: {str(e)}")

    if format.lower() == "json":
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=customer-experience.json"},
        )
    else:
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=customer-experience.csv"},
        )


@router.get("/business-impact", response_model=BusinessImpactResponse)
async def get_business_impact(
    response: Response,
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter. Omitting returns platform-wide aggregate."),
    current_user: dict = Depends(get_current_user),
):
    """
    Get Section 5 Business Impact metrics, KPI cards (Human Hours Saved, Estimated Cost Saved,
    ROI Metric, FTE Equivalent Saved, SLA Compliance), 12-month Cumulative Savings Timeline,
    Organizational Impact department savings bar chart, and Efficiency Audit ROI calculator summary.
    Includes 5-minute TTL caching and HTTP Cache-Control headers.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    try:
        return await analytics_service.get_business_impact(
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch business impact: {str(e)}")


@router.get("/business-impact/export")
async def export_business_impact(
    format: str = Query(default="csv", description="Export format: 'csv' or 'json'"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter"),
    current_user: dict = Depends(get_current_user),
):
    """
    Export the Section 5 Business Impact report as a downloadable CSV or JSON file.
    """
    if format.lower() not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    try:
        content = await analytics_service.export_business_impact(
            format=format,
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export business impact: {str(e)}")

    if format.lower() == "json":
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=business-impact.json"},
        )
    else:
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=business-impact.csv"},
        )


@router.get("/conversation-insights", response_model=ConversationInsightsResponse)
async def get_conversation_insights(
    response: Response,
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter. Omitting returns platform-wide aggregate."),
    current_user: dict = Depends(get_current_user),
):
    """
    Get Section 6 Conversation Insights metrics, KPI cards (Total Volume, Top Intent,
    Busiest Channel, KB Usage Rate, System Uptime), 12-month Traffic Volume Trends,
    Semantic Mapping Top 5 Customer Intents, and Routing Analysis Channel Distribution.
    Includes 5-minute TTL caching and HTTP Cache-Control headers.
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    try:
        return await analytics_service.get_conversation_insights(
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch conversation insights: {str(e)}")


@router.get("/conversation-insights/export")
async def export_conversation_insights(
    format: str = Query(default="csv", description="Export format: 'csv' or 'json'"),
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    start_date: Optional[datetime] = Query(default=None, description="Optional start date in ISO format"),
    end_date: Optional[datetime] = Query(default=None, description="Optional end date in ISO format"),
    company_id: Optional[str] = Query(default=None, description="Optional company ID filter"),
    current_user: dict = Depends(get_current_user),
):
    """
    Export the Section 6 Conversation Insights report as a downloadable CSV or JSON file.
    """
    if format.lower() not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Invalid format. Supported formats are 'csv' and 'json'.")

    try:
        content = await analytics_service.export_conversation_insights(
            format=format,
            days=days,
            start_date=start_date,
            end_date=end_date,
            company_id=company_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export conversation insights: {str(e)}")

    if format.lower() == "json":
        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=conversation-insights.json"},
        )
    else:
        return Response(
            content=content,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=conversation-insights.csv"},
        )





