"""
Conversation Intelligence Layer
================================
Post-conversation analysis pipeline that auto-extracts:
- Intent classification (billing, technical, general, etc.)
- Sentiment trajectory (improving/declining/stable)
- Resolution confidence score
- Auto-tags for searchability
- Suggested knowledge base gaps

Stored in MongoDB `conversation_intelligence` collection.
"""

import asyncio
import json
import time
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.core.database import db
from app.models.conversation_intelligence_models import (
    ConversationIntelligence,
    ConversationTag,
    IntentClassification,
    KnowledgeGap,
    MessageSentiment,
    ResolutionAnalysis,
    SentimentAnalysis,
    SentimentTrajectoryPoint,
)
from app.services.graph.llm_factory import get_llm

logger = logging.getLogger(__name__)


# ============================================================================
# SYSTEM PROMPTS
# ============================================================================

INTENT_CLASSIFICATION_PROMPT = """You are an intent classification system for customer support conversations.

Classify the following conversation into exactly ONE primary intent category.
Available categories:
- billing: Refunds, charges, invoices, payment issues, subscriptions, plans
- technical: Bugs, errors, outages, API issues, login problems, performance
- general: General questions, how-to, informational, greetings
- account: Account creation, deletion, settings, password, permissions
- feature_request: Feature suggestions, enhancements, new capabilities
- complaint: Formal complaints, dissatisfaction, escalation demands
- onboarding: Setup, getting started, configuration help

Return ONLY a JSON object with this exact schema:
{
  "primary": "<category>",
  "sub_intent": "<specific sub-intent or null>",
  "confidence": <float 0.0-1.0>,
  "secondary_intents": ["<other applicable categories>"]
}"""

SENTIMENT_ANALYSIS_PROMPT = """You are a sentiment analysis system for customer support conversations.

Analyze the sentiment trajectory of this conversation. For EACH user message, classify the sentiment.
Then assess the overall trajectory direction.

Return ONLY a JSON object with this exact schema:
{
  "initial_sentiment": "<positive|neutral|negative|frustrated|satisfied>",
  "final_sentiment": "<positive|neutral|negative|frustrated|satisfied>",
  "trajectory": "<improving|declining|stable_positive|stable_negative|neutral>",
  "trajectory_points": [
    {
      "turn_index": <int>,
      "role": "<user|assistant>",
      "sentiment": "<label>",
      "score": <float -1.0 to 1.0>
    }
  ],
  "average_score": <float -1.0 to 1.0>,
  "peak_negative_score": <float -1.0 to 0.0>,
  "risk_of_churn": <float 0.0-1.0>
}"""

RESOLUTION_ANALYSIS_PROMPT = """You are a resolution analysis system for customer support conversations.

Determine if the customer's issue was resolved and how.
Return ONLY a JSON object with this exact schema:
{
  "resolved": <boolean>,
  "confidence": <float 0.0-1.0>,
  "resolution_type": "<self_service|ai_resolved|agent_resolved|escalated|unresolved>",
  "resolution_turns": <int or null>,
  "requires_follow_up": <boolean>,
  "follow_up_reason": "<reason or null>
}"""

TAGGING_PROMPT = """You are a tagging system for customer support conversations.

Generate searchable tags that categorize this conversation. Each tag must have a category.

Available tag categories:
- topic: The main subject matter (e.g., "API", "billing", "dashboard")
- issue_type: The type of problem (e.g., "login_failure", "slow_response")
- product_area: The product area involved (e.g., "auth", "payments", "settings")
- urgency: Urgency level if detectable (e.g., "low", "normal", "high", "critical")
- feature_gap: Missing features the customer expected (e.g., "missing_dark_mode")

Return ONLY a JSON object with this exact schema:
{
  "tags": [
    {
      "tag": "<tag_name>",
      "category": "<category>",
      "confidence": <float 0.0-1.0>
    }
  ]
}

Generate 3-8 tags maximum."""

KNOWLEDGE_GAP_PROMPT = """You are a knowledge base gap analysis system for customer support conversations.

Identify topics where the customer had to ask because the knowledge base lacked coverage.
Only flag genuine gaps - topics where a KB article would have prevented the conversation.

Return ONLY a JSON object with this exact schema:
{
  "gaps": [
    {
      "topic": "<topic lacking coverage>",
      "suggested_title": "<suggested KB article title>",
      "description": "<what the article should cover>",
      "priority": "<high|medium|low>",
      "related_intent": "<primary intent that triggered this gap>"
    }
  ]
}

Return empty gaps array if the KB appears to have adequate coverage."""

REVIEW_FLAG_PROMPT = """You are a quality review flagging system.

Determine if this conversation requires human review based on:
- Customer expressed extreme frustration or anger
- Legal threats or compliance mentions
- Sensitive data exposure concerns
- AI gave potentially incorrect information
- Customer explicitly requested human review
- Resolution confidence is very low
- Potential churn indicators

Return ONLY a JSON object:
{
  "requires_review": <boolean>,
  "reasons": ["<reason1>", "<reason2>"]
}"""

INLINE_SENTIMENT_PROMPT = """Analyze the sentiment of this single customer message. Return ONLY JSON:
{
  "sentiment": "<positive|neutral|negative|frustrated|satisfied>",
  "score": <float -1.0-1.0>,
  "emotions": ["<emotion>"],
  "urgency": "<low|normal|high|critical>",
  "escalation_triggered": <boolean>
}"""


# ============================================================================
# DB HELPERS
# ============================================================================




async def get_intelligence(session_id: str) -> Optional[ConversationIntelligence]:
    """Retrieve intelligence analysis for a session."""
    doc = await db.conversation_intelligence.find_one({"session_id": session_id})
    if doc:
        doc.pop("_id", None)
        return ConversationIntelligence(**doc)
    return None


async def get_company_intelligence(
    company_id: str,
    days: int = 30,
    intent_filter: Optional[str] = None,
    tag_filter: Optional[str] = None,
    requires_review: Optional[bool] = None,
    limit: int = 100,
) -> List[ConversationIntelligence]:
    """Retrieve intelligence analyses for a company with optional filters."""
    query: Dict = {"company_id": company_id}

    if intent_filter:
        query["intent.primary"] = intent_filter
    if tag_filter:
        query["tags"] = {"$elemMatch": {"tag": tag_filter}}
    if requires_review is not None:
        query["requires_human_review"] = requires_review

    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    query["analyzed_at"] = {"$gte": cutoff}

    cursor = (
        db.conversation_intelligence.find(query)
        .sort("analyzed_at", -1)
        .limit(limit)
    )
    results = []
    async for doc in cursor:
        doc.pop("_id", None)
        results.append(ConversationIntelligence(**doc))
    return results


# ============================================================================
# LLM ANALYSIS FUNCTIONS
# ============================================================================


def _format_messages_for_analysis(messages: List[Dict]) -> str:
    """Format message list into a readable conversation transcript."""
    lines = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "user").upper()
        content = msg.get("content", "")[:500]  # Truncate long messages
        lines.append(f"[{i}] {role}: {content}")
    return "\n".join(lines)


async def _invoke_llm_json(
    system_prompt: str,
    user_content: str,
    temperature: float = 0.1,
) -> Dict:
    """Invoke LLM and parse JSON response with error handling."""
    try:
        llm = get_llm("gemini", fast_routing=True, streaming=False)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]
        response = await llm.ainvoke(messages)
        text = response.content if isinstance(response.content, str) else str(response.content)
        text = text.strip()

        # Strip markdown code blocks if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        import json as json_module

        return json_module.loads(text)
    except Exception as e:
        logger.warning(f"LLM JSON parse failed: {e}")
        return {}


async def analyze_intent(messages: List[Dict]) -> Optional[IntentClassification]:
    """Classify the intent of a conversation."""
    conversation_text = _format_messages_for_analysis(messages)
    if not conversation_text.strip():
        return None

    result = await _invoke_llm_json(
        INTENT_CLASSIFICATION_PROMPT,
        conversation_text,
    )

    if not result or "primary" not in result:
        return None

    return IntentClassification(
        primary=result.get("primary", "general"),
        sub_intent=result.get("sub_intent"),
        confidence=result.get("confidence", 0.5),
        secondary_intents=result.get("secondary_intents", []),
    )


async def analyze_sentiment(messages: List[Dict]) -> Optional[SentimentAnalysis]:
    """Analyze sentiment trajectory across the conversation."""
    conversation_text = _format_messages_for_analysis(messages)
    if not conversation_text.strip():
        return None

    result = await _invoke_llm_json(
        SENTIMENT_ANALYSIS_PROMPT,
        conversation_text,
    )

    if not result:
        return None

    trajectory_points = []
    for point_data in result.get("trajectory_points", []):
        try:
            trajectory_points.append(SentimentTrajectoryPoint(
                turn_index=point_data.get("turn_index", 0),
                role=point_data.get("role", "user"),
                sentiment=point_data.get("sentiment", "neutral"),
                score=point_data.get("score", 0.0),
            ))
        except Exception:
            continue

    return SentimentAnalysis(
        initial_sentiment=result.get("initial_sentiment", "neutral"),
        final_sentiment=result.get("final_sentiment", "neutral"),
        trajectory=result.get("trajectory", "neutral"),
        trajectory_points=trajectory_points,
        average_score=result.get("average_score", 0.0),
        peak_negative_score=result.get("peak_negative_score", 0.0),
        risk_of_churn=result.get("risk_of_churn", 0.0),
    )


async def analyze_resolution(messages: List[Dict]) -> Optional[ResolutionAnalysis]:
    """Assess whether and how the conversation was resolved."""
    conversation_text = _format_messages_for_analysis(messages)
    if not conversation_text.strip():
        return None

    result = await _invoke_llm_json(
        RESOLUTION_ANALYSIS_PROMPT,
        conversation_text,
    )

    if not result:
        return None

    return ResolutionAnalysis(
        resolved=result.get("resolved", False),
        confidence=result.get("confidence", 0.5),
        resolution_type=result.get("resolution_type", "unresolved"),
        resolution_turns=result.get("resolution_turns"),
        requires_follow_up=result.get("requires_follow_up", False),
        follow_up_reason=result.get("follow_up_reason"),
    )


async def generate_tags(messages: List[Dict]) -> List[ConversationTag]:
    """Generate auto-tags for searchability."""
    conversation_text = _format_messages_for_analysis(messages)
    if not conversation_text.strip():
        return []

    result = await _invoke_llm_json(
        TAGGING_PROMPT,
        conversation_text,
    )

    if not result or "tags" not in result:
        return []

    tags = []
    for tag_data in result.get("tags", [])[:8]:
        try:
            tags.append(ConversationTag(
                tag=tag_data.get("tag", "uncategorized"),
                category=tag_data.get("category", "topic"),
                confidence=tag_data.get("confidence", 0.5),
                source="llm",
            ))
        except Exception:
            continue
    return tags


async def detect_knowledge_gaps(messages: List[Dict]) -> List[KnowledgeGap]:
    """Detect knowledge base gaps from the conversation."""
    conversation_text = _format_messages_for_analysis(messages)
    if not conversation_text.strip():
        return []

    result = await _invoke_llm_json(
        KNOWLEDGE_GAP_PROMPT,
        conversation_text,
    )

    if not result or "gaps" not in result:
        return []

    gaps = []
    for gap_data in result.get("gaps", [])[:5]:
        try:
            gaps.append(KnowledgeGap(
                topic=gap_data.get("topic", "unknown"),
                suggested_title=gap_data.get("suggested_title", "Suggested Article"),
                description=gap_data.get("description", ""),
                priority=gap_data.get("priority", "medium"),
                related_intent=gap_data.get("related_intent"),
            ))
        except Exception:
            continue
    return gaps


async def check_review_required(messages: List[Dict]) -> tuple[bool, List[str]]:
    """Determine if a conversation requires human review."""
    conversation_text = _format_messages_for_analysis(messages)
    if not conversation_text.strip():
        return False, []

    result = await _invoke_llm_json(
        REVIEW_FLAG_PROMPT,
        conversation_text,
    )

    if not result:
        return False, []

    return (
        result.get("requires_review", False),
        result.get("reasons", []),
    )


# ============================================================================
# MAIN ANALYSIS PIPELINE
# ============================================================================


async def analyze_conversation(
    session_id: str,
    company_id: str,
    user_id: Optional[str],
    messages: List[Dict],
) -> Optional[ConversationIntelligence]:
    """
    Run the full conversation intelligence pipeline.

    This is the main entry point. Called post-conversation (fire-and-forget)
    from the chat router. It runs all analysis stages and persists results.

    Returns the intelligence result or None if analysis fails.
    """
    start_time = time.time()

    if not messages or len(messages) < 2:
        logger.debug(f"Skipping intelligence for session {session_id}: too few messages")
        return None

    try:
        # Run all analyses (some can be parallelized)
        intent, sentiment, resolution, tags, gaps, (needs_review, review_reasons) = (
            await asyncio.gather(
                analyze_intent(messages),
                analyze_sentiment(messages),
                analyze_resolution(messages),
                generate_tags(messages),
                detect_knowledge_gaps(messages),
                check_review_required(messages),
            )
        )

        processing_time = (time.time() - start_time) * 1000

        user_message_count = sum(1 for m in messages if m.get("role") == "user")

        intelligence = ConversationIntelligence(
            session_id=session_id,
            company_id=company_id,
            user_id=user_id,
            message_count=len(messages),
            user_message_count=user_message_count,
            intent=intent,
            sentiment=sentiment,
            resolution=resolution,
            tags=tags,
            knowledge_gaps=gaps,
            model_used="gemini-flash",
            processing_time_ms=processing_time,
            analyzed_at=datetime.now(timezone.utc),
            requires_human_review=needs_review,
            review_reasons=review_reasons,
        )

        # Upsert to handle re-analysis gracefully
        await db.conversation_intelligence.update_one(
            {"session_id": session_id},
            {"$set": intelligence.model_dump()},
            upsert=True,
        )

        logger.info(
            f"Conversation intelligence for {session_id}: "
            f"intent={intent.primary if intent else '?'} "
            f"SENTIMENT={sentiment.trajectory if sentiment else '?'} "
            f"RESOLVED={resolution.resolved if resolution else '?'} "
            f"PROCESSING={processing_time:.0f}ms"
        )

        return intelligence

    except Exception as e:
        logger.exception(f"Failed to analyze conversation {session_id}: {e}")
        return None


# ============================================================================
# INLINE / REAL-TIME ANALYSIS (for per-message use)
# ============================================================================


async def analyze_message_sentiment(content: str) -> Optional[MessageSentiment]:
    """Analyze sentiment of a single message in real-time."""
    if not content or not content.strip():
        return None

    result = await _invoke_llm_json(INLINE_SENTIMENT_PROMPT, content[:500])

    if not result:
        return None

    return MessageSentiment(
        sentiment=result.get("sentiment", "neutral"),
        score=result.get("score", 0.0),
        emotions=result.get("emotions", []),
        urgency=result.get("urgency", "normal"),
        escalation_triggered=result.get("escalation_triggered", False),
    )


# ============================================================================
# ANALYTICS AGGREGATION
# ============================================================================


async def get_company_intelligence_summary(
    company_id: str,
    days: int = 30,
) -> Dict:
    """Get aggregated intelligence summary for a company."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    pipeline = [
        {"$match": {"company_id": company_id, "analyzed_at": {"$gte": cutoff}}},
        {"$group": {
            "_id": None,
            "total_analyzed": {"$sum": 1},
            "avg_sentiment": {"$avg": "$sentiment.average_score"},
            "avg_resolution_confidence": {"$avg": "$resolution.confidence"},
            "avg_risk_of_churn": {"$avg": "$sentiment.risk_of_churn"},
            "review_count": {
                "$sum": {"$cond": ["$requires_human_review", 1, 0]}
            },
            "total_kb_gaps": {"$sum": {"$size": {"$ifNull": ["$knowledge_gaps", []]}}},
        }},
    ]

    result = await db.conversation_intelligence.aggregate(pipeline).to_list(1)

    if not result:
        return {
            "company_id": company_id,
            "total_analyzed": 0,
            "avg_sentiment_score": 0.0,
            "avg_resolution_confidence": 0.0,
            "top_intents": [],
            "top_tags": [],
            "total_knowledge_gaps": 0,
            "requires_review_count": 0,
        }

    summary = result[0]

    # Get top intents
    intent_pipeline = [
        {"$match": {"company_id": company_id, "analyzed_at": {"$gte": cutoff}, "intent": {"$ne": None}}},
        {"$group": {"_id": "$intent.primary", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    intents = await db.conversation_intelligence.aggregate(intent_pipeline).to_list(10)
    intent_volumes = [
        {"intent_name": i["_id"], "count": i["count"]}
        for i in intents
        if i.get("_id")
    ]

    # Get top tags
    tag_pipeline = [
        {"$match": {"company_id": company_id, "analyzed_at": {"$gte": cutoff}}},
        {"$unwind": "$tags"},
        {"$group": {"_id": "$tags.tag", "count": {"$sum": 1}, "category": {"$first": "$tags.category"}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    tags = await db.conversation_intelligence.aggregate(tag_pipeline).to_list(10)

    return {
        "company_id": company_id,
        "total_analyzed": summary.get("total_analyzed") or 0,
        "avg_sentiment_score": round(summary.get("avg_sentiment") or 0.0, 3),
        "avg_resolution_confidence": round(summary.get("avg_resolution_confidence") or 0.0, 3),
        "avg_risk_of_churn": round(summary.get("avg_risk_of_churn") or 0.0, 3),
        "top_intents": intent_volumes,
        "top_tags": [{"tag": t["_id"], "count": t["count"], "category": t.get("category", "topic")} for t in tags],
        "total_knowledge_gaps": summary.get("total_kb_gaps") or 0,
        "requires_review_count": summary.get("review_count") or 0,
    }
