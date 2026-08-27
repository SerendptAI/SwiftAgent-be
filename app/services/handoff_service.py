"""
Enhanced AI-to-Human Handoff Service
=====================================
Passes full conversation context to human agents during escalation.
Shows AI's attempted solutions and why they failed.
Provides estimated wait time and agent routing.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from app.core.database import db
from app.models.prompt_models import (
    PromptTemplate,
    PromptTemplateCreate,
    PromptTemplateUpdate,
    PromptVariable,
    PromptTemplateVersion,
    PromptTemplateVersionHistory,
    PromptDiffResponse,
    PromptPreviewRequest,
    PromptPreviewResponse,
    RenderedPrompt,
    CompanyPromptOverride,
)

logger = logging.getLogger(__name__)
# HANDOFF CONTEXT BUILDER
# ============================================================================


class AIAttemptedSolution(BaseModel):
    """Record of what the AI tried and what happened."""
    tool_used: str
    tool_input: Optional[str] = None
    result_summary: str
    success: bool
    error: Optional[str] = None


class HandoffContext(BaseModel):
    """Rich context passed to human agent during escalation."""
    session_id: str
    company_id: str
    customer_email: Optional[str]
    escalation_reason: str  # human_request, ai_fallback, repeated_failures, idle_unanswered
    escalation_reason_text: str  # Human-readable explanation

    # Conversation summary
    conversation_summary: str  # 2-3 sentence summary of what the customer needed
    message_count: int
    user_message_count: int
    conversation_duration_minutes: float

    # AI attempt tracking
    ai_attempted_solutions: List[AIAttemptedSolution] = []
    ai_failure_points: List[str] = []  # What went wrong

    # Customer context
    customer_sentiment: str  # positive, neutral, negative, frustrated
    customer_language: str
    page_url: Optional[str] = None

    # Routing context
    intent: str  # What the orchestrator classified
    agents_visited: List[str] = []  # Which specialist agents were tried

    # Suggested next steps for human agent
    suggested_actions: List[str] = []

    # Timestamps
    escalated_at: datetime


# ============================================================================
# CONVERSATION ANALYSIS
# ============================================================================


def build_conversation_summary(messages: List[Dict]) -> str:
    """Build a 2-3 sentence summary of the conversation."""
    if not messages:
        return "Empty conversation."

    user_messages = [m for m in messages if m.get("role") == "user"]
    assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    # Extract key topics from user messages
    topics = []
    for msg in user_messages[:5]:
        content = msg.get("content", "")
        if len(content) > 10:
            # Take first 100 chars of each user message as topic hint
            topics.append(content[:100].strip())

    summary_parts = []
    if topics:
        summary_parts.append(f"Customer asked about: {topics[0]}")
        if len(topics) > 1:
            summary_parts.append(f"Follow-up topics: {', '.join(t[:50] for t in topics[1:3])}")

    summary_parts.append(f"Total messages: {len(user_messages)} user, {len(assistant_messages)} assistant")

    return ". ".join(summary_parts)


def extract_ai_attempts(messages: List[Dict]) -> List[AIAttemptedSolution]:
    """Extract what tools the AI used and whether they succeeded."""
    attempts = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue

        # Check for tool calls in the message
        tool_calls = msg.get("tool_calls", [])
        for tc in tool_calls:
            tool_name = tc.get("name", "unknown")
            tool_input = tc.get("arguments", "")

            # Look for the corresponding tool result
            result_summary = "Tool executed"
            success = True
            error = None

            # Find the next tool result message
            if i + 1 < len(messages):
                next_msg = messages[i + 1]
                if next_msg.get("role") == "tool":
                    result_content = next_msg.get("content", "")
                    if len(result_content) > 200:
                        result_summary = result_content[:200] + "..."
                    else:
                        result_summary = result_content
                    # Check for errors
                    if "error" in result_content.lower() or "failed" in result_content.lower():
                        success = False
                        error = result_content[:200]

            attempts.append(AIAttemptedSolution(
                tool_used=tool_name,
                tool_input=tool_input[:200] if tool_input else None,
                result_summary=result_summary,
                success=success,
                error=error,
            ))

    return attempts


def identify_failure_points(messages: List[Dict], attempts: List[AIAttemptedSolution]) -> List[str]:
    """Identify what went wrong during the conversation."""
    failures = []

    # Check for fallback messages
    fallback_count = 0
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", "").strip()
            if "I'm sorry" in content or "I wasn't able to find" in content or "trouble right now" in content:
                fallback_count += 1

    if fallback_count > 0:
        failures.append(f"AI gave fallback response {fallback_count} time(s)")

    # Check for failed tool calls
    failed_tools = [a for a in attempts if not a.success]
    if failed_tools:
        failures.append(f"Tool failures: {', '.join(a.tool_used for a in failed_tools)}")

    # Check for repeated similar questions (customer not getting answer)
    user_messages = [m for m in messages if m.get("role") == "user"]
    if len(user_messages) >= 3:
        # Simple heuristic: if user sent 3+ messages without a successful AI response
        recent = user_messages[-3:]
        if all(len(m.get("content", "")) > 20 for m in recent):
            failures.append("Customer sent multiple messages without resolution")

    # Check for escalation keywords
    escalation_keywords = ["human", "agent", "person", "speak to", "talk to", "representative", "manager"]
    for msg in user_messages:
        content = msg.get("content", "").lower()
        if any(kw in content for kw in escalation_keywords):
            failures.append("Customer explicitly requested human agent")
            break

    return failures


def determine_customer_sentiment(messages: List[Dict]) -> str:
    """Simple sentiment analysis based on message content."""
    user_messages = [m for m in messages if m.get("role") == "user"]
    if not user_messages:
        return "neutral"

    negative_keywords = ["angry", "frustrated", "terrible", "awful", "worst", "hate", "useless", "broken", "not working", "bad", "poor", "disappointed", "unacceptable"]
    positive_keywords = ["thanks", "thank you", "great", "awesome", "perfect", "excellent", "good", "helpful", "amazing"]

    negative_count = 0
    positive_count = 0

    for msg in user_messages:
        content = msg.get("content", "").lower()
        negative_count += sum(1 for kw in negative_keywords if kw in content)
        positive_count += sum(1 for kw in positive_keywords if kw in content)

    if negative_count > positive_count + 1:
        return "frustrated"
    elif negative_count > 0:
        return "negative"
    elif positive_count > 0:
        return "positive"
    return "neutral"


def generate_suggested_actions(messages: List[Dict], intent: str) -> List[str]:
    """Generate suggested next steps for the human agent."""
    actions = []

    # Based on intent
    if intent == "knowledge":
        actions.append("Check knowledge base for relevant articles")
        actions.append("Verify if the answer has changed recently")
    elif intent == "navigation":
        actions.append("Confirm the UI steps are still accurate")
        actions.append("Check if there are any UI changes")
    elif intent == "api":
        actions.append("Verify the API endpoint is working")
        actions.append("Check if the data is up to date")
    elif intent == "scraper":
        actions.append("Verify the website content is current")
        actions.append("Check if the page structure has changed")

    # Based on conversation content
    user_messages = [m for m in messages if m.get("role") == "user"]
    for msg in user_messages:
        content = msg.get("content", "").lower()
        if "refund" in content:
            actions.append("Review refund policy before responding")
        if "cancel" in content:
            actions.append("Check cancellation policy and process")
        if "bug" in content or "error" in content:
            actions.append("Escalate to engineering if bug is confirmed")

    return actions[:5]  # Max 5 suggestions


# ============================================================================
# WAIT TIME ESTIMATION
# ============================================================================


async def estimate_wait_time(company_id: str) -> Dict[str, Any]:
    """Estimate wait time for a human agent based on current queue."""
    now = datetime.now(timezone.utc)

    # Count pending tickets
    pending_count = await db.email_tickets.count_documents({
        "company_id": company_id,
        "status": {"$in": ["pending", "in_progress"]},
    })

    # Count tickets awaiting customer response
    awaiting_customer = await db.email_tickets.count_documents({
        "company_id": company_id,
        "status": "awaiting_customer",
    })

    # Count online agents (agents who have been active in the last 10 minutes)
    ten_min_ago = now - timedelta(minutes=10)
    online_agents = await db.users.count_documents({
        "company_id": company_id,
        "last_active": {"$gte": ten_min_ago},
        "role": {"$in": ["admin", "agent"]},
    })

    # Calculate estimated wait
    if online_agents == 0:
        estimated_wait_minutes = 30  # No agents online
        queue_position = pending_count + 1
    else:
        # Simple formula: pending tickets / online agents * avg handling time
        avg_handling_minutes = 15
        estimated_wait_minutes = (pending_count / max(online_agents, 1)) * avg_handling_minutes
        queue_position = pending_count + 1

    return {
        "estimated_wait_minutes": round(estimated_wait_minutes),
        "queue_position": queue_position,
        "pending_tickets": pending_count,
        "awaiting_customer": awaiting_customer,
        "online_agents": online_agents,
        "has_agents_available": online_agents > 0,
    }


# ============================================================================
# AGENT AVAILABILITY
# ============================================================================


async def get_available_agents(company_id: str) -> List[Dict[str, str]]:
    """Get list of available human agents for a company."""
    now = datetime.now(timezone.utc)
    five_min_ago = now - timedelta(minutes=5)

    # Get company members who are agents
    company = await db.companies.find_one({"id": company_id})
    if not company:
        return []

    members = company.get("members", [])
    agent_ids = [m.get("user_id") for m in members if m.get("role") in ["admin", "agent", "manager"]]

    # Check which agents are online (active in last 5 minutes)
    online_agents = []
    for agent_id in agent_ids:
        user = await db.users.find_one({"user_id": agent_id})
        if user:
            last_active = user.get("last_active")
            if last_active:
                if isinstance(last_active, str):
                    last_active = datetime.fromisoformat(last_active.replace("Z", "+00:00"))
                if last_active >= five_min_ago:
                    online_agents.append({
                        "user_id": agent_id,
                        "name": user.get("name", "Agent"),
                        "role": user.get("role", "agent"),
                    })

    return online_agents


# ============================================================================
# MAIN HANDOFF ORCHESTRATOR
# ============================================================================


async def create_enhanced_handoff(
    session_id: str,
    company_id: str,
    messages: List[Dict],
    escalation_reason: str,
    customer_email: Optional[str] = None,
    intent: str = "general_chat",
    agents_visited: Optional[List[str]] = None,
    page_url: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create a rich handoff context and return it for use in ticket creation.
    Returns a dict with handoff_context and wait_time_estimate.
    """
    now = datetime.now(timezone.utc)

    # Build conversation summary
    conversation_summary = build_conversation_summary(messages)

    # Extract AI attempts
    ai_attempts = extract_ai_attempts(messages)

    # Identify failure points
    failure_points = identify_failure_points(messages, ai_attempts)

    # Determine sentiment
    sentiment = determine_customer_sentiment(messages)

    # Generate suggested actions
    suggested_actions = generate_suggested_actions(messages, intent)

    # Calculate conversation duration
    user_messages = [m for m in messages if m.get("role") == "user"]
    conversation_duration = 0.0
    if user_messages:
        first_ts = user_messages[0].get("timestamp")
        last_ts = user_messages[-1].get("timestamp")
        if first_ts and last_ts:
            if isinstance(first_ts, str):
                first_ts = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            if isinstance(last_ts, str):
                last_ts = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            conversation_duration = (last_ts - first_ts).total_seconds() / 60.0

    # Map escalation reason to human-readable text
    reason_map = {
        "human_request": "Customer requested human agent",
        "ai_fallback_response": "AI could not find an answer",
        "repeated_ai_failures": "AI failed multiple times",
        "idle_unanswered": "Customer waiting too long for response",
    }
    escalation_reason_text = reason_map.get(escalation_reason, "Escalated to human agent")

    # Build handoff context
    handoff_context = HandoffContext(
        session_id=session_id,
        company_id=company_id,
        customer_email=customer_email,
        escalation_reason=escalation_reason,
        escalation_reason_text=escalation_reason_text,
        conversation_summary=conversation_summary,
        message_count=len(messages),
        user_message_count=len(user_messages),
        conversation_duration_minutes=round(conversation_duration, 1),
        ai_attempted_solutions=ai_attempts,
        ai_failure_points=failure_points,
        customer_sentiment=sentiment,
        customer_language="en",  # TODO: detect from state
        page_url=page_url,
        intent=intent,
        agents_visited=agents_visited or [],
        suggested_actions=suggested_actions,
        escalated_at=now,
    )

    # Get wait time estimate
    wait_time = await estimate_wait_time(company_id)

    # Get available agents
    available_agents = await get_available_agents(company_id)

    return {
        "handoff_context": handoff_context.model_dump(),
        "wait_time_estimate": wait_time,
        "available_agents": available_agents,
    }


# ============================================================================
# API ENDPOINT SCHEMAS
# ============================================================================


class HandoffContextResponse(BaseModel):
    """Response for the handoff context API endpoint."""
    session_id: str
    company_id: str
    escalation_reason: str
    escalation_reason_text: str
    conversation_summary: str
    messages: List[Dict]  # Full conversation transcript
    ai_attempted_solutions: List[Dict]
    ai_failure_points: List[str]
    customer_sentiment: str
    suggested_actions: List[str]
    escalated_at: datetime
    wait_time_estimate: Dict[str, Any]
    available_agents: List[Dict[str, str]]


# Import here to avoid circular import
from pydantic import BaseModel
