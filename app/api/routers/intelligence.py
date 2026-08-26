from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.core.auth import verify_analytics_secret_key
from app.core.database import db
from app.services.conversation_intelligence_service import (
    get_intelligence,
    get_company_intelligence,
    get_company_intelligence_summary,
    analyze_conversation,
)
from app.models.conversation_intelligence_models import (
    ConversationIntelligence,
)

router = APIRouter(tags=["Conversation Intelligence"])


@router.get("/{session_id}", response_model=ConversationIntelligence)
async def get_conversation_intelligence(
    session_id: str,
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Retrieve full conversation intelligence analysis for a specific session.
    
    Returns intent classification, sentiment trajectory, resolution analysis,
    auto-tags, and knowledge base gaps.
    """
    response.headers["Cache-Control"] = "private, max-age=300"
    
    intelligence = await get_intelligence(session_id)
    if not intelligence:
        raise HTTPException(
            status_code=404,
            detail=f"No intelligence analysis found for session {session_id}. "
                   f"The conversation may still be in progress or analysis failed."
        )
    
    return intelligence


@router.get("/company/{company_id}/summary")
async def get_company_summary(
    response: Response,
    company_id: str,
    days: int = Query(default=30, ge=1, le=365, description="Number of days in reporting window"),
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Get aggregated conversation intelligence summary for a company.
    
    Returns total analyzed count, average sentiment, top intents, top tags,
    knowledge base gaps count, and human review queue count.
    """
    response.headers["Cache-Control"] = "private, max-age=300"
    
    try:
        summary = await get_company_intelligence_summary(
            company_id=company_id,
            days=days,
        )
        return summary
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch company intelligence summary: {str(e)}"
        )


@router.get("/company/{company_id}/conversations")
async def get_company_conversations(
    response: Response,
    company_id: str,
    days: int = Query(default=30, ge=1, le=365),
    intent_filter: Optional[str] = Query(None, description="Filter by primary intent"),
    tag_filter: Optional[str] = Query(None, description="Filter by tag"),
    requires_review: Optional[bool] = Query(None, description="Filter by human review flag"),
    limit: int = Query(default=100, ge=1, le=500),
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    List conversation intelligence records for a company with optional filters.
    Useful for exploring specific intents, tags, or review queues.
    """
    response.headers["Cache-Control"] = "private, max-age=120"
    
    try:
        conversations = await get_company_intelligence(
            company_id=company_id,
            days=days,
            intent_filter=intent_filter,
            tag_filter=tag_filter,
            requires_review=requires_review,
            limit=limit,
        )
        return {
            "count": len(conversations),
            "company_id": company_id,
            "conversations": [c.model_dump() for c in conversations],
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch company intelligence: {str(e)}"
        )


@router.get("/company/{company_id}/knowledge-gaps")
async def get_company_knowledge_gaps(
    response: Response,
    company_id: str,
    days: int = Query(default=30, ge=1, le=365),
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Get aggregated knowledge base gaps for a company.
    Returns all detected gaps from conversations in the reporting period.
    """
    response.headers["Cache-Control"] = "private, max-age=300"
    
    conversations = await get_company_intelligence(
        company_id=company_id,
        days=days,
        limit=500,
    )
    
    all_gaps = []
    for conv in conversations:
        for gap in conv.knowledge_gaps:
            all_gaps.append(gap.model_dump())
    
    return {
        "company_id": company_id,
        "total_gaps": len(all_gaps),
        "gaps": all_gaps,
    }


@router.get("/company/{company_id}/review-queue")
async def get_review_queue(
    response: Response,
    company_id: str,
    days: int = Query(default=7, ge=1, le=90),
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Get conversations flagged for human review.
    Shows conversations with reasons for review (churn risk, escalation, etc.)
    """
    response.headers["Cache-Control"] = "private, max-age=60"
    
    conversations = await get_company_intelligence(
        company_id=company_id,
        days=days,
        requires_review=True,
        limit=50,
    )
    
    return {
        "company_id": company_id,
        "count": len(conversations),
        "conversations": [
            {
                "session_id": c.session_id,
                "analyzed_at": c.analyzed_at,
                "review_reasons": c.review_reasons,
                "intent": c.intent.primary if c.intent else None,
                "sentiment": c.sentiment.trajectory if c.sentiment else None,
                "risk_of_churn": c.sentiment.risk_of_churn if c.sentiment else 0.0,
            }
            for c in conversations
        ],
    }


@router.post("/{session_id}/reanalyze")
async def reanalyze_conversation(
    session_id: str,
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Manually re-run intelligence analysis on a session.
    Useful after the conversation has been updated (e.g., human agent resolved it).
    """
    conversation = await db.widget_conversations.find_one({"session_id": session_id})
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    messages = conversation.get("messages", [])
    if len(messages) < 2:
        raise HTTPException(status_code=400, detail="Not enough messages to analyze")
    
    intelligence = await analyze_conversation(
        session_id=session_id,
        company_id=conversation.get("company_id"),
        user_id=conversation.get("user_id"),
        messages=messages,
    )
    
    if not intelligence:
        raise HTTPException(status_code=500, detail="Analysis failed")
    
    return intelligence
