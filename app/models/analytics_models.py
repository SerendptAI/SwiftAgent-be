from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class ArrVelocityPoint(BaseModel):
    """Single data point for the 30-day North Star Metric sparkline."""
    date: str = Field(..., description="Date label in YYYY-MM-DD format")
    rate: float = Field(..., description="Autonomous Resolution Rate for this day (percentage)")


class ArrNorthStarMetric(BaseModel):
    """North Star Metric banner data (Autonomous Resolution Rate)."""
    current_arr: float = Field(..., description="Current Autonomous Resolution Rate percentage (e.g., 78.4)")
    arr_change: float = Field(..., description="Percentage point change compared to previous period (e.g., 3.2)")
    velocity_30d: List[ArrVelocityPoint] = Field(default_factory=list, description="Daily ARR velocity series")


class MetricCard(BaseModel):
    """Standardized KPI card structure."""
    value: float = Field(..., description="Numeric value of the KPI")
    change: float = Field(..., description="Percentage change compared to previous period")
    formatted: str = Field(..., description="Human-readable formatted string (e.g., '$187,200', '2,340 hrs')")
    max_value: Optional[float] = Field(None, description="Maximum scale value if applicable (e.g., 5.0 for CSAT)")


class ExecutiveSummaryKpis(BaseModel):
    """The 5 KPI summary cards shown on the Executive Summary page."""
    total_conversations: MetricCard
    human_hours_saved: MetricCard
    est_cost_savings: MetricCard
    csat_score: MetricCard
    active_companies: MetricCard


class HistoricalArrMonth(BaseModel):
    """Monthly data point for the 12-month Historical Performance ARR trend chart."""
    month: str = Field(..., description="Month label (e.g., 'Jan', 'Feb')")
    arr: float = Field(..., description="Autonomous Resolution Rate percentage for the month")
    target_goal: float = Field(default=80.0, description="Target ARR goal line percentage")


class ChannelResolutionItem(BaseModel):
    """Load distribution breakdown item by channel."""
    channel: str = Field(..., description="Channel name ('Live Chat', 'Email', 'Ticketing', 'Forms')")
    resolved_count: int = Field(..., description="Number of resolved items on this channel")
    percentage: float = Field(..., description="Percentage of total resolutions on this channel")


class TriageSplitData(BaseModel):
    """Direct Triage Split donut chart data (AI vs. Human Resolution)."""
    autonomous_ai_count: int = Field(..., description="Conversations resolved autonomously by AI")
    autonomous_ai_percentage: float = Field(..., description="Percentage resolved by AI")
    escalated_human_count: int = Field(..., description="Conversations escalated to human agents")
    escalated_human_percentage: float = Field(..., description="Percentage resolved by human agents")
    total_resolved: int = Field(..., description="Total resolved conversations")


class TopCompanyItem(BaseModel):
    """Row item in the Top Companies by Conversation Volume table."""
    company_id: str
    company_name: str
    logo_url: Optional[str] = None
    conversations: int = Field(..., description="Total conversation volume")
    arr: float = Field(..., description="Autonomous Resolution Rate percentage for this company")
    csat_score: float = Field(..., description="CSAT Score out of 5.0")
    csat_max: float = Field(default=5.0, description="Maximum CSAT score")


class ExecutiveSummaryResponse(BaseModel):
    """Full payload for GET /api/v1/analytics/executive-summary."""
    time_range: str = Field(..., description="Label indicating the active time filter (e.g. 'LAST 30 DAYS')")
    start_date: datetime
    end_date: datetime
    north_star: ArrNorthStarMetric
    kpis: ExecutiveSummaryKpis
    historical_arr_12m: List[HistoricalArrMonth]
    resolution_by_channel: List[ChannelResolutionItem]
    triage_split: TriageSplitData
    top_companies: List[TopCompanyItem]


# =====================================================================
# SECTION 2: RESOLUTION PERFORMANCE SCHEMAS
# =====================================================================


class ResolutionPerformanceKpis(BaseModel):
    """The 5 KPI summary cards shown on the Resolution Performance page."""
    arr_trend: MetricCard
    escalation_rate: MetricCard
    first_contact_resolution: MetricCard
    avg_resolution_time: MetricCard
    repeat_contact_rate: MetricCard


class ArrAndEscalationTrendMonth(BaseModel):
    """Monthly data point for the 12-month ARR & Escalation Rate trend chart."""
    month: str = Field(..., description="Month label ('Jan', 'Feb', etc.)")
    arr: float = Field(..., description="Autonomous Resolution Rate percentage")
    escalation_rate: float = Field(..., description="Human Escalation Rate percentage")


class WeeklyResolutionOutcome(BaseModel):
    """Weekly breakdown of resolution outcomes ('Week 1' to 'Week 4')."""
    week: str = Field(..., description="Week label (e.g. 'Week 1')")
    ai_resolved: int = Field(..., description="Number of AI resolved conversations")
    escalated: int = Field(..., description="Number of escalated conversations")
    pending: int = Field(..., description="Number of pending conversations")
    ai_resolved_percentage: float = Field(..., description="Percentage AI resolved")
    escalated_percentage: float = Field(..., description="Percentage escalated")
    pending_percentage: float = Field(..., description="Percentage pending")


class ChannelResolutionTimeItem(BaseModel):
    """Row item in the Resolution Time by Channel table."""
    channel: str = Field(..., description="Channel name ('Live Chat', 'Email', 'Ticketing', 'Forms')")
    avg_time_minutes: float = Field(..., description="Average resolution time in minutes")
    avg_time_formatted: str = Field(..., description="Formatted string (e.g. '1.8 min')")
    volume: int = Field(..., description="Total volume of conversations on this channel")
    volume_formatted: str = Field(..., description="Formatted string (e.g. '64,250')")


class ResolutionPerformanceResponse(BaseModel):
    """Full payload for GET /api/v1/analytics/resolution-performance."""
    time_range: str = Field(..., description="Label indicating the active time filter (e.g. 'LAST 30 DAYS')")
    start_date: datetime
    end_date: datetime
    kpis: ResolutionPerformanceKpis
    historical_trends: List[ArrAndEscalationTrendMonth]
    weekly_breakdown: List[WeeklyResolutionOutcome]
    channel_metrics: List[ChannelResolutionTimeItem]


# =====================================================================
# SECTION 3: AI PERFORMANCE SCHEMAS
# =====================================================================


class AiPerformanceKpis(BaseModel):
    """The 5 KPI summary cards shown on the AI Performance page."""
    ai_accuracy: MetricCard
    confidence_score: MetricCard
    avg_response_time: MetricCard
    hallucination_rate: MetricCard
    low_confidence_responses: MetricCard


class AccuracyAndConfidenceTrendMonth(BaseModel):
    """Monthly data point for the 12-month AI Accuracy & Confidence Trends chart."""
    month: str = Field(..., description="Month label ('Jan', 'Feb', etc.)")
    accuracy: float = Field(..., description="AI Accuracy Rate percentage")
    confidence_score: float = Field(..., description="Average Confidence Score (0-100)")


class ResponseTimeDistributionBucket(BaseModel):
    """Row item in the Response Time Distribution bar chart."""
    bucket: str = Field(..., description="Latency bracket label ('< 1s', '1 - 3s', etc.)")
    percentage: float = Field(..., description="Percentage value displayed on bar")
    count: int = Field(..., description="Total response count in this latency bracket")


class ConfidenceLevelDistribution(BaseModel):
    """Model Health donut chart data (Confidence Level Distribution)."""
    high_count: int = Field(..., description="Number of high confidence responses (>90%)")
    high_percentage: float = Field(..., description="Percentage of high confidence responses")
    medium_count: int = Field(..., description="Number of medium confidence responses (70-90%)")
    medium_percentage: float = Field(..., description="Percentage of medium confidence responses")
    low_count: int = Field(..., description="Number of low confidence responses (<70%)")
    low_percentage: float = Field(..., description="Percentage of low confidence responses")
    total_responses: int = Field(..., description="Total AI responses evaluated")
    avg_confidence: float = Field(..., description="Average confidence percentage displayed in center")


class AiPerformanceResponse(BaseModel):
    """Full payload for GET /api/v1/analytics/ai-performance."""
    time_range: str = Field(..., description="Label indicating the active time filter (e.g. 'LAST 30 DAYS')")
    start_date: datetime
    end_date: datetime
    kpis: AiPerformanceKpis
    accuracy_trends: List[AccuracyAndConfidenceTrendMonth]
    latency_distribution: List[ResponseTimeDistributionBucket]
    confidence_distribution: ConfidenceLevelDistribution


# =====================================================================
# SECTION 4: CUSTOMER EXPERIENCE TAB MODELS
# =====================================================================

class CustomerExperienceKpis(BaseModel):
    """5 KPI summary cards at the top of Section 4 (Customer Experience)."""
    csat_score: MetricCard = Field(..., description="CSAT score out of 5.0")
    positive_sentiment: MetricCard = Field(..., description="Positive sentiment percentage")
    nps_score: MetricCard = Field(..., description="Net Promoter Score (NPS)")
    ces_score: MetricCard = Field(..., description="Customer Effort Score (CES) out of 5.0")
    feedback_volume: MetricCard = Field(..., description="Total feedback volume count")


class CsatAndNpsTrendMonth(BaseModel):
    """Single month item for Monthly CSAT & NPS Trends chart."""
    month: str = Field(..., description="Month label (e.g., 'Jan')")
    csat_score: float = Field(..., description="CSAT score for the month")
    nps_score: float = Field(..., description="NPS score for the month")


class CustomerSentimentBreakdown(BaseModel):
    """Donut chart breakdown of customer sentiment."""
    positive_count: int = Field(..., description="Count of positive feedback items")
    positive_percentage: float = Field(..., description="Percentage of positive feedback")
    neutral_count: int = Field(..., description="Count of neutral feedback items")
    neutral_percentage: float = Field(..., description="Percentage of neutral feedback")
    negative_count: int = Field(..., description="Count of negative feedback items")
    negative_percentage: float = Field(..., description="Percentage of negative feedback")
    total_feedback_count: int = Field(..., description="Total feedback items analyzed")


class FeedbackThemeItem(BaseModel):
    """Row item in Top Feedback Themes text analytics list."""
    theme: str = Field(..., description="Feedback theme or topic text")
    mentions_count: int = Field(..., description="Number of mentions")
    mentions_formatted: str = Field(..., description="Formatted mentions label (e.g., '3,142 mentions')")
    sentiment: str = Field(..., description="Sentiment classification badge ('POSITIVE', 'NEUTRAL', 'NEGATIVE')")


class CustomerExperienceResponse(BaseModel):
    """Full payload for GET /api/v1/analytics/customer-experience."""
    time_range: str = Field(..., description="Label indicating the active time filter (e.g. 'LAST 30 DAYS')")
    start_date: datetime
    end_date: datetime
    kpis: CustomerExperienceKpis
    csat_nps_trends: List[CsatAndNpsTrendMonth]
    sentiment_breakdown: CustomerSentimentBreakdown
    top_feedback_themes: List[FeedbackThemeItem]


# =====================================================================
# SECTION 5: BUSINESS IMPACT TAB MODELS
# =====================================================================

class BusinessImpactKpis(BaseModel):
    """5 KPI summary cards at the top of Section 5 (Business Impact)."""
    human_hours_saved: MetricCard = Field(..., description="Human hours saved by AI resolution")
    estimated_cost_saved: MetricCard = Field(..., description="Estimated cost saved in USD")
    roi_metric: MetricCard = Field(..., description="Return on Investment (ROI) percentage")
    fte_equivalent_saved: MetricCard = Field(..., description="Full-Time Equivalent (FTE) savings")
    sla_compliance: MetricCard = Field(..., description="SLA compliance percentage")


class CostAndHoursTrendMonth(BaseModel):
    """Single month item for Cumulative Savings Timeline chart (Cost Saved vs. Hours Saved Trends)."""
    month: str = Field(..., description="Month label (e.g., 'Jan')")
    cost_saved: float = Field(..., description="Total cost saved in USD for the month")
    cost_saved_k: float = Field(..., description="Cost saved in thousands ($k)")
    hours_saved: float = Field(..., description="Total hours saved for the month")


class DepartmentSavingsItem(BaseModel):
    """Row item in Organizational Impact bar chart (Savings by Department)."""
    department: str = Field(..., description="Department name")
    saved_amount: float = Field(..., description="Dollar amount saved")
    saved_formatted: str = Field(..., description="Formatted savings label (e.g., '$94,500 Saved')")
    percentage: float = Field(..., description="Percentage of total net savings")


class RoiCalculatorSummary(BaseModel):
    """Efficiency Audit ROI Calculator summary card and breakdown."""
    total_return: float = Field(..., description="Total return dollar amount")
    total_return_formatted: str = Field(..., description="Formatted total return (e.g., '$241,800')")
    initial_investment: float = Field(..., description="Initial investment cost")
    initial_investment_formatted: str = Field(..., description="Formatted investment label (e.g., 'On $54,600 Initial Investment')")
    net_savings: float = Field(..., description="Net savings dollar amount")
    net_savings_formatted: str = Field(..., description="Formatted net savings (e.g., '$187,200')")
    saas_platform_license: float = Field(..., description="SaaS license cost")
    saas_platform_license_formatted: str = Field(..., description="Formatted SaaS license cost (e.g., '$32,000')")
    ops_and_maintenance: float = Field(..., description="Operations & maintenance cost")
    ops_and_maintenance_formatted: str = Field(..., description="Formatted Ops cost (e.g., '$22,600')")
    roi_percentage: float = Field(..., description="Calculated ROI percentage")


class BusinessImpactResponse(BaseModel):
    """Full payload for GET /api/v1/analytics/business-impact."""
    time_range: str = Field(..., description="Label indicating the active time filter (e.g. 'LAST 30 DAYS')")
    start_date: datetime
    end_date: datetime
    kpis: BusinessImpactKpis
    savings_timeline: List[CostAndHoursTrendMonth]
    department_savings: List[DepartmentSavingsItem]
    roi_summary: RoiCalculatorSummary


# =====================================================================
# SECTION 6: CONVERSATION INSIGHTS TAB MODELS
# =====================================================================

class ConversationInsightsKpis(BaseModel):
    """5 KPI summary cards at the top of Section 6 (Conversation Insights)."""
    total_volume: MetricCard = Field(..., description="Total conversation volume across all channels")
    top_intent: MetricCard = Field(..., description="Top customer intent with share percentage")
    busiest_channel: MetricCard = Field(..., description="Busiest support channel with share percentage")
    kb_usage_rate: MetricCard = Field(..., description="Knowledge base lookup usage percentage")
    system_uptime: MetricCard = Field(..., description="System uptime percentage")


class TrafficVolumeTrendMonth(BaseModel):
    """Single month item for Traffic Volume Trends chart (Conversation Volumes Over Last 12 Months)."""
    month: str = Field(..., description="Month label (e.g., 'Jan')")
    live_chat_volume: int = Field(..., description="Live Chat conversation volume for the month")
    email_volume: int = Field(..., description="Email ticket volume for the month")
    ticketing_volume: int = Field(..., description="Ticketing & API forms volume for the month")
    total_volume: int = Field(..., description="Total combined volume for the month")


class CustomerIntentItem(BaseModel):
    """Row item in Semantic Mapping horizontal bar chart (Top 5 Customer Intents)."""
    rank: int = Field(..., description="Rank from 1 to 5")
    intent_name: str = Field(..., description="Customer intent topic name")
    volume: int = Field(..., description="Conversation volume for this intent")
    percentage: float = Field(..., description="Percentage of total categorized intents")
    formatted_label: str = Field(..., description="Formatted intent label (e.g., '26,012 (18.2%)')")


class ChannelDistributionBreakdown(BaseModel):
    """Routing Analysis donut chart (Channel Distribution)."""
    live_chat_percentage: float = Field(..., description="Live Chat percentage share")
    email_support_percentage: float = Field(..., description="Email Support percentage share")
    ticketing_api_percentage: float = Field(..., description="Ticketing / API percentage share")
    busiest_channel_name: str = Field(..., description="Name of the busiest channel")
    busiest_channel_percentage: float = Field(..., description="Percentage of the busiest channel")


class ConversationInsightsResponse(BaseModel):
    """Full payload for GET /api/v1/analytics/conversation-insights."""
    time_range: str = Field(..., description="Label indicating the active time filter (e.g. 'LAST 30 DAYS')")
    start_date: datetime
    end_date: datetime
    kpis: ConversationInsightsKpis
    traffic_trends: List[TrafficVolumeTrendMonth]
    top_intents: List[CustomerIntentItem]
    channel_distribution: ChannelDistributionBreakdown


class FeedbackSubmitRequest(BaseModel):
    """Payload for submitting customer feedback on a conversation or ticket."""
    session_id: Optional[str] = Field(None, description="Widget conversation session ID")
    ticket_id: Optional[str] = Field(None, description="Email ticket ID")
    csat_score: Optional[float] = Field(None, ge=1.0, le=5.0, description="Customer satisfaction rating 1.0 to 5.0")
    nps_score: Optional[int] = Field(None, ge=0, le=10, description="Net Promoter Score 0 to 10")
    ces_score: Optional[int] = Field(None, ge=1, le=7, description="Customer Effort Score 1 to 7")
    sentiment: Optional[str] = Field(None, description="Explicit sentiment: positive, neutral, or negative")
    feedback_theme: Optional[str] = Field(None, description="Tag for theme, e.g. 'Unhelpful AI Response', 'Slow Resolution'")
    comment: Optional[str] = Field(None, max_length=2000, description="Optional textual feedback")
    company_id: Optional[str] = Field(None, description="Target company ID")


class FeedbackSubmitResponse(BaseModel):
    """Response returned upon successful submission of customer feedback."""
    success: bool
    message: str
    feedback_id: str
    updated_at: datetime






