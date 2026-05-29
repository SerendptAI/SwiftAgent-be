"""
SDK Router — API endpoints for third-party SDK consumers (mobile/web).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.core.security import create_access_token
from app.core.sdk_auth import get_sdk_session, verify_api_key
from app.models.sdk_models import (
    SdkChatRequest,
    SdkConversationDetail,
    SdkConversationListResponse,
    SdkInitRequest,
    SdkInitResponse,
)
from app.services import sdk_service, anthropic_agent_service, openrouter_agent_service, gemini_agent_service
from app.api.routers.chat import _AGENT_MAP, _DEFAULT_AGENT, _sse
from app.core.utils import get_random_avatar

logger = logging.getLogger(__name__)

router = APIRouter(tags=["SDK"])


@router.post("/{company_id}/init", response_model=SdkInitResponse)
async def init_sdk(
    company_id: str,
    req: SdkInitRequest,
    company: dict = Depends(verify_api_key),
):
    """
    Initialize the SDK session for an end-user.
    Requires a valid X-API-Key header.
    Returns a short-lived session token (JWT) to authenticate further requests.
    """
    if company["id"] != company_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key does not belong to the requested company_id",
        )

    # Find or create SDK user
    user_data = await sdk_service.init_sdk_user(company_id, req.email)

    # Generate SDK Session Token (JWT)
    # Expiry: e.g. 24 hours (can be customized via expires_delta if needed)
    token_claims = {
        "sub": req.email,
        "company_id": company_id,
        "type": "sdk",
    }
    session_token = create_access_token(data=token_claims)

    return {
        "session_token": session_token,
        "company": {
            "name": company.get("name", "Support"),
            "logo_url": company.get("logo_url"),
            "suggested_ai_prompts": company.get("suggested_ai_prompts", []),
        },
        "user": user_data,
    }


async def _sdk_chat_sse_generator(company_id: str, email: str, req: SdkChatRequest):
    """SSE generator for SDK chat, injecting user email into context."""
    try:
        from app.core.database import db
        company = await db.companies.find_one({"id": company_id})
        if not company:
            yield _sse("error", message="Company not found")
            yield _sse("done")
            return

        yield _sse("chat_details", session_id=req.session_id, company_id=company_id)

        # Inject SDK email into context string so the agent knows it implicitly
        # This allows the agent's create_support_ticket tool to use it automatically.
        agent_key = company.get("ai_provider") or _DEFAULT_AGENT
        
        # Build a list of fallback agents to try
        stream_fns_to_try = [_AGENT_MAP.get(agent_key, _AGENT_MAP[_DEFAULT_AGENT])]
        if agent_key == "anthropic" or (not company.get("ai_provider") and _DEFAULT_AGENT == "anthropic"):
            stream_fns_to_try.append(_AGENT_MAP["gemini"])
            stream_fns_to_try.append(_AGENT_MAP["openrouter"])

        response_text = ""
        
        # Update/create the conversation document with sdk_user_email and source BEFORE calling agent
        # so that it exists in the DB for the get_conversation_history query immediately.
        await db.widget_conversations.update_one(
             {"company_id": company_id, "session_id": req.session_id},
             {
                 "$set": {
                     "sdk_user_email": email,
                     "source": "sdk",
                 },
                 "$setOnInsert": {
                     "avatar": get_random_avatar(),
                 }
             },
             upsert=True
        )

        # Create a modified user message that reminds the agent of the email address
        # This ensures the ticket creation tool has it without asking.
        # We don't save this prefix to the DB history, just pass it to the agent this turn.
        injected_message = f"[System Context: The current user's email address is {email}. Do NOT ask for their email address if you need to create a support ticket. Use this email address automatically.]\n\n{req.message}"

        for idx, stream_fn in enumerate(stream_fns_to_try):
            try:
                response_text = ""
                # Call stream_fn with the injected message
                async for event in stream_fn(company_id, req.session_id, injected_message, user_id=None):
                    event_type = event.get("type")

                    if event_type == "thinking":
                        yield _sse("thinking", message=event.get("message", ""))
                    elif event_type == "tool":
                        yield _sse("tool", name=event.get("name", ""), label=event.get("label", ""))
                    elif event_type == "text":
                        content = event.get("content", "")
                        response_text += content
                        yield _sse("stream", message=content)
                    elif event_type == "sources":
                        yield _sse("sources", sources=event.get("sources", []), blockchain_data=event.get("blockchain_data"))
                    elif event_type == "navigation_guide":
                        guide = event.get("guide", {})
                        yield _sse("navigation_guide", steps=guide.get("steps", []), path_summary=guide.get("path_summary", []))
                    elif event_type == "error":
                        raise Exception(event.get('message') or "Agent stream yielded an error")

                # If successful, break the retry loop
                break

            except Exception as e:
                if idx < len(stream_fns_to_try) - 1 and not response_text.strip():
                    logger.warning(f"SDK Agent stream failed: {e}. Falling back to next agent...")
                    yield _sse("thinking", message="Switching AI providers...")
                    continue
                
                logger.error(f"SDK Agent error for company {company_id}: {e}")
                friendly = "I'm having trouble right now. Please try again in a moment."
                if not response_text.strip():
                    response_text = friendly
                yield _sse("stream", message=friendly)
                break

        yield _sse("done")

        # Overwrite the last user message in the DB history to remove the injected context
        # so the user doesn't see the system prompt in their history.
        conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": req.session_id})
        if conversation and "messages" in conversation:
            messages = conversation["messages"]
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user" and "System Context:" in messages[i].get("content", ""):
                    messages[i]["content"] = req.message
                    break
            
            await db.widget_conversations.update_one(
                {"_id": conversation["_id"]},
                {"$set": {"messages": messages}}
            )


    except Exception as e:
        logger.exception(f"SDK Chat SSE error for company {company_id}")
        yield _sse("error", message="Something went wrong on our end. Please refresh and try again.")
        yield _sse("done")


@router.post("/{company_id}/chat", summary="Stream Chat Response (SDK)")
async def sdk_chat_endpoint(
    company_id: str,
    req: SdkChatRequest,
    session: dict = Depends(get_sdk_session),
):
    """
    Stream agent chat responses as Server-Sent Events (SSE).
    Uses the authenticated SDK session token.
    """
    if session["company_id"] != company_id:
        raise HTTPException(status_code=403, detail="Your session is not authorized for this company. Please re-initialize the SDK.")

    email = session["email"]
    return StreamingResponse(
        _sdk_chat_sse_generator(company_id, email, req),
        media_type="text/event-stream",
    )


@router.get("/{company_id}/conversations", response_model=SdkConversationListResponse)
async def list_sdk_conversations(
    company_id: str,
    limit: int = Query(20, ge=1, le=50),
    cursor: Optional[str] = Query(None, description="ISO datetime string of the last item's updated_at"),
    session: dict = Depends(get_sdk_session),
):
    """
    Get a unified list of the user's conversation history (both chats and tickets).
    """
    if session["company_id"] != company_id:
        raise HTTPException(status_code=403, detail="Your session is not authorized for this company. Please re-initialize the SDK.")

    result = await sdk_service.get_conversation_history(
        company_id=company_id,
        email=session["email"],
        limit=limit,
        cursor=cursor
    )
    return result


@router.get("/{company_id}/conversations/{conversation_id}", response_model=SdkConversationDetail)
async def get_sdk_conversation_detail(
    company_id: str,
    conversation_id: str,
    session: dict = Depends(get_sdk_session),
):
    """
    Get full details (message thread) for a specific conversation (chat or ticket).
    """
    if session["company_id"] != company_id:
        raise HTTPException(status_code=403, detail="Your session is not authorized for this company. Please re-initialize the SDK.")

    detail = await sdk_service.get_conversation_detail(
        company_id=company_id,
        email=session["email"],
        conversation_id=conversation_id
    )
    
    if not detail:
        raise HTTPException(status_code=404, detail="Conversation not found")
        
    return detail
