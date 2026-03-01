"""
Widget API router — unauthenticated endpoints for the embeddable chat widget.
These endpoints are called from the customer-facing widget embedded on company websites.
"""
from fastapi import APIRouter, HTTPException
from app.models.agent_models import WidgetChatRequest, WidgetChatResponse
from app.services import agent_service
from app.core.database import db

router = APIRouter(tags=["Widget"])


@router.post("/{company_id}/chat", response_model=WidgetChatResponse)
async def widget_chat(company_id: str, request: WidgetChatRequest):
    """
    Send a chat message from the widget and receive an AI-powered response.
    No authentication required — sessions tracked by widget-generated session_id.
    """
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    if not company.get("setup_complete"):
        raise HTTPException(status_code=400, detail="Company setup is not complete")

    result = await agent_service.chat(company_id, request.session_id, request.message)

    return WidgetChatResponse(
        session_id=request.session_id,
        reply=result["reply"],
        sources=result.get("sources", []),
        blockchain_data=result.get("blockchain_data"),
    )


@router.get("/{company_id}/config")
async def widget_config(company_id: str):
    """
    Get company branding info for the widget (name, logo, colors).
    No authentication required.
    """
    company = await db.companies.find_one(
        {"id": company_id},
        {"name": 1, "logo_url": 1, "brand_tone": 1, "description": 1, "primary_language": 1, "_id": 0},
    )
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    return {
        "company_id": company_id,
        "name": company.get("name", "Support"),
        "logo_url": company.get("logo_url"),
        "brand_tone": company.get("brand_tone", "professional"),
        "primary_language": company.get("primary_language", "English"),
    }


@router.get("/{company_id}/history/{session_id}")
async def widget_history(company_id: str, session_id: str):
    """
    Load conversation history for a session (used when widget reopens).
    No authentication required.
    """
    convo = await db.widget_conversations.find_one({
        "company_id": company_id,
        "session_id": session_id,
    })
    if not convo:
        return {"messages": []}

    # Only return user/assistant messages, not internal data
    messages = [
        {"role": m["role"], "content": m["content"], "timestamp": m.get("timestamp")}
        for m in convo.get("messages", [])
        if m["role"] in ("user", "assistant")
    ]
    return {"messages": messages}
