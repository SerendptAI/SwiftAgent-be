from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from app.core.auth import verify_analytics_secret_key
from app.core.database import db
from app.services.handoff_service import (
    create_enhanced_handoff,
    estimate_wait_time,
    get_available_agents,
    build_conversation_summary,
    extract_ai_attempts,
    identify_failure_points,
    determine_customer_sentiment,
    generate_suggested_actions,
    HandoffContextResponse,
)
from app.services.graph.builder import compiled_graph
from app.core.langfuse import observe

router = APIRouter(tags=["Handoff"])


@router.get("/handoff/{session_id}/context", response_model=HandoffContextResponse)
async def get_handoff_context(
    session_id: str,
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Get the full handoff context for a session.
    Returns conversation summary, AI attempted solutions, failure points,
    suggested actions, and available agents.
    """
    response.headers["Cache-Control"] = "private, max-age=0"

    # Get the conversation
    conversation = await db.widget_conversations.find_one({"session_id": session_id})
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    company_id = conversation.get("company_id")
    if not company_id:
        raise HTTPException(status_code=400, detail="No company_id on conversation")

    messages = conversation.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages in conversation")

    # Build handoff context
    handoff_result = await create_enhanced_handoff(
        session_id=session_id,
        company_id=company_id,
        messages=messages,
        escalation_reason="human_request",
        customer_email=conversation.get("sdk_user_email"),
        intent="general_chat",
        agents_visited=[],
        page_url=None,
    )

    return HandoffContextResponse(
        session_id=session_id,
        company_id=company_id,
        escalation_reason=handoff_result["handoff_context"]["escalation_reason"],
        escalation_reason_text=handoff_result["handoff_context"]["escalation_reason_text"],
        conversation_summary=handoff_result["handoff_context"]["conversation_summary"],
        messages=messages,
        ai_attempted_solutions=[a.model_dump() if hasattr(a, 'model_dump') else a for a in handoff_result["handoff_context"]["ai_attempted_solutions"]],
        ai_failure_points=handoff_result["handoff_context"]["ai_failure_points"],
        customer_sentiment=handoff_result["handoff_context"]["customer_sentiment"],
        suggested_actions=handoff_result["handoff_context"]["suggested_actions"],
        escalated_at=handoff_result["handoff_context"]["escalated_at"],
        wait_time_estimate=handoff_result["wait_time_estimate"],
        available_agents=handoff_result["available_agents"],
    )


@router.get("/handoff/company/{company_id}/queue-status")
async def get_queue_status(
    company_id: str,
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Get the current queue status for a company.
    Used by the customer-facing widget to show estimated wait time.
    """
    response.headers["Cache-Control"] = "private, max-age=30"

    wait_time = await estimate_wait_time(company_id)
    available_agents = await get_available_agents(company_id)

    return {
        "company_id": company_id,
        "estimated_wait_minutes": wait_time["estimated_wait_minutes"],
        "queue_position": wait_time["queue_position"],
        "pending_tickets": wait_time["pending_tickets"],
        "online_agents": wait_time["online_agents"],
        "has_agents_available": wait_time["has_agents_available"],
        "available_agents": available_agents,
    }


@router.post("/handoff/{session_id}/initiate")
async def initiate_handoff(
    session_id: str,
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Initiate a handoff for a session.
    Called by the AI agent or admin when escalation is needed.
    Returns the handoff context and creates a pending ticket.
    """
    response.headers["Cache-Control"] = "private, max-age=0"

    conversation = await db.widget_conversations.find_one({"session_id": session_id})
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = conversation.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages to hand off")

    # Build handoff context
    company_id = conversation.get("company_id")
    customer_email = conversation.get("sdk_user_email")

    handoff_result = await create_enhanced_handoff(
        session_id=session_id,
        company_id=company_id,
        messages=messages,
        escalation_reason="human_request",
        customer_email=customer_email,
        intent="general_chat",
    )

    return {
        "status": "handoff_initiated",
        "session_id": session_id,
        **handoff_result,
    }
