from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


# ============================================================================
# INTENT CLASSIFICATION
# ============================================================================


class IntentClassification(BaseModel):
    """Classified intent for a conversation."""
    primary: str = Field(
        ...,
        description="Primary intent category",
        examples=["billing", "technical", "general", "account", "feature_request"],
    )
    sub_intent: Optional[str] = Field(
        None,
        description="More specific sub-intent",
        examples=["refund_request", "login_issue", "api_key_generation"],
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Classification confidence score",
    )
    secondary_intents: List[str] = Field(
        default_factory=list,
        description="Other applicable intent categories",
    )


# ============================================================================
# SENTIMENT ANALYSIS
# ============================================================================


class SentimentTrajectoryPoint(BaseModel):
    """A single sentiment measurement along the conversation timeline."""
    turn_index: int = Field(..., description="0-based index of the message turn")
    role: str = Field(..., description="'user' or 'assistant'")
    sentiment: str = Field(
        ...,
        description="Sentiment label",
        examples=["positive", "neutral", "negative", "frustrated", "satisfied"],
    )
    score: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="Sentiment score from -1.0 (most negative) to 1.0 (most positive)",
    )


class SentimentAnalysis(BaseModel):
    """Sentiment trajectory and overall assessment for a conversation."""
    initial_sentiment: str = Field(
        ...,
        description="Customer sentiment at conversation start",
    )
    final_sentiment: str = Field(
        ...,
        description="Customer sentiment at conversation end",
    )
    trajectory: str = Field(
        ...,
        description="Overall sentiment direction",
        examples=["improving", "declining", "stable_positive", "stable_negative", "neutral"],
    )
    trajectory_points: List[SentimentTrajectoryPoint] = Field(
        default_factory=list,
        description="Per-turn sentiment measurements",
    )
    average_score: float = Field(
        ...,
        ge=-1.0,
        le=1.0,
        description="Weighted average sentiment score across all user turns",
    )
    peak_negative_score: float = Field(
        default=0.0,
        ge=-1.0,
        le=0.0,
        description="Most negative score observed",
    )
    risk_of_churn: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Probability the customer will churn based on sentiment",
    )


# ============================================================================
# RESOLUTION ANALYSIS
# ============================================================================


class ResolutionAnalysis(BaseModel):
    """Assessment of whether and how the conversation was resolved."""
    resolved: bool = Field(
        ...,
        description="Whether the customer's issue was resolved",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence in the resolution assessment",
    )
    resolution_type: str = Field(
        ...,
        description="How the conversation was resolved",
        examples=["self_service", "ai_resolved", "agent_resolved", "escalated", "unresolved"],
    )
    resolution_turns: Optional[int] = Field(
        None,
        description="Number of turns until resolution (null if unresolved)",
    )
    requires_follow_up: bool = Field(
        default=False,
        description="Whether this conversation needs follow-up",
    )
    follow_up_reason: Optional[str] = Field(
        None,
        description="Reason for required follow-up",
    )


# ============================================================================
# AUTO-TAGGING
# ============================================================================


class ConversationTag(BaseModel):
    """A single auto-generated tag with metadata."""
    tag: str = Field(..., description="Tag name")
    category: str = Field(
        ...,
        description="Tag category",
        examples=["topic", "issue_type", "product_area", "urgency", "feature_gap"],
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Tag relevance confidence",
    )
    source: str = Field(
        default="llm",
        description="How the tag was derived",
        examples=["llm", "rule", "keyword"],
    )


# ============================================================================
# KNOWLEDGE BASE GAP
# ============================================================================


class KnowledgeGap(BaseModel):
    """A suggested knowledge base gap derived from conversation analysis."""
    topic: str = Field(..., description="Topic that lacks coverage")
    suggested_title: str = Field(..., description="Suggested KB article title")
    description: str = Field(..., description="What the article should cover")
    priority: str = Field(
        ...,
        description="Gap priority",
        examples=["high", "medium", "low"],
    )
    related_intent: Optional[str] = Field(
        None,
        description="Primary intent that triggered this gap",
    )


# ============================================================================
# TOP-LEVEL ANALYSIS RESULT
# ============================================================================


class ConversationIntelligence(BaseModel):
    """Full conversation intelligence analysis result."""
    session_id: str
    company_id: str
    user_id: Optional[str] = None
    message_count: int = Field(default=0, description="Total messages in conversation")
    user_message_count: int = Field(default=0, description="User messages only")

    # Core analysis outputs
    intent: Optional[IntentClassification] = None
    sentiment: Optional[SentimentAnalysis] = None
    resolution: Optional[ResolutionAnalysis] = None
    tags: List[ConversationTag] = Field(
        default_factory=list,
        description="Auto-generated searchability tags",
    )
    knowledge_gaps: List[KnowledgeGap] = Field(
        default_factory=list,
        description="Suggested knowledge base gaps",
    )

    # LLM metadata
    model_used: Optional[str] = Field(None, description="LLM model used for analysis")
    processing_time_ms: Optional[float] = Field(
        None, description="Total LLM processing time in milliseconds"
    )

    # Timestamps
    analyzed_at: datetime = Field(default_factory=datetime.utcnow)

    # Flags
    requires_human_review: bool = Field(
        default=False,
        description="Whether this conversation needs human review",
    )
    review_reasons: List[str] = Field(
        default_factory=list,
        description="Reasons for requiring human review",
    )


# ============================================================================
# BATCH ANALYTICS AGGREGATION
# ============================================================================


class IntentVolume(BaseModel):
    """Aggregated intent volume for analytics."""
    intent_name: str
    count: int = 0
    avg_sentiment_score: float = 0.0
    avg_resolution_rate: float = 0.0
    total_messages: int = 0


class CompanyIntelligenceSummary(BaseModel):
    """Summary of conversation intelligence for a company."""
    company_id: str
    total_analyzed: int = 0
    avg_sentiment_score: float = 0.0
    avg_resolution_confidence: float = 0.0
    top_intents: List[IntentVolume] = Field(default_factory=list)
    top_tags: List[ConversationTag] = Field(default_factory=list)
    total_knowledge_gaps: int = 0
    requires_review_count: int = 0
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None


# ============================================================================
# MESSAGE-LEVEL SENTIMENT (for per-message inline analysis)
# ============================================================================


class MessageSentiment(BaseModel):
    """Sentiment analysis result for a single message."""
    sentiment: str
    score: float = Field(..., ge=-1.0, le=1.0)
    emotions: List[str] = Field(
        default_factory=list,
        description="Detected emotions",
        examples=["frustrated", "confused", "relieved", "angry", "happy"],
    )
    urgency: str = Field(
        default="normal",
        description="Urgency level",
        examples=["low", "normal", "high", "critical"],
    )
    escalation_triggered: bool = Field(
        default=False,
        description="Whether this message should trigger escalation",
    )
