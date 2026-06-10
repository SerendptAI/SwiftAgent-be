"""
SDK Router — API endpoints for third-party SDK consumers (mobile/web).
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, Request
from fastapi.responses import StreamingResponse

from app.core.security import create_access_token
from app.core.request_utils import get_client_ip
from app.core.sdk_auth import get_sdk_session, verify_api_key
from app.models.sdk_models import (
    SdkChatRequest,
    SdkConversationDetail,
    SdkConversationListResponse,
    SdkInitRequest,
    SdkInitResponse,
)
from app.services import sdk_service, anthropic_agent_service, openrouter_agent_service, gemini_agent_service
from app.api.routers.chat import _AGENT_MAP, _TITLE_MAP, _DEFAULT_AGENT, _sse, upload_chat_files
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


async def _sdk_chat_sse_generator(company_id: str, email: str, req: SdkChatRequest, visitor_ip: str | None = None, user_timestamp: str | None = None):
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
        agents_to_try = [agent_key]
        if agent_key == "anthropic" or (not company.get("ai_provider") and _DEFAULT_AGENT == "anthropic"):
            agents_to_try.append("openrouter")
            agents_to_try.append("gemini")

        response_text = ""
        
        # Handle AI generated chat subject
        conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": req.session_id})
        subject = conversation.get("subject") if conversation else None
        
        if not subject:
            for ak in agents_to_try:
                title_fn = _TITLE_MAP.get(ak, _TITLE_MAP[_DEFAULT_AGENT])
                subject = await title_fn(req.message)
                if subject and subject != "New Chat":
                    break
            
            if not subject:
                subject = "New Chat"

            yield _sse("subject", subject=subject)
            
            # Update/create the conversation document with sdk_user_email and source BEFORE calling agent
            await db.widget_conversations.update_one(
                 {"company_id": company_id, "session_id": req.session_id},
                 {
                     "$set": {
                         "sdk_user_email": email,
                         "source": "sdk",
                         "subject": subject,
                     },
                     "$setOnInsert": {
                         "avatar": get_random_avatar(),
                     }
                 },
                 upsert=True
            )
        else:
            yield _sse("subject", subject=subject)
            
            # Just ensure sdk_user_email is set
            await db.widget_conversations.update_one(
                 {"company_id": company_id, "session_id": req.session_id},
                 {
                     "$set": {
                         "sdk_user_email": email,
                         "source": "sdk",
                     }
                 }
            )

        # Link this conversation to the visitor (by IP) so the visitors
        # endpoint can attribute communication duration. Done pre-stream so it
        # persists even if the client disconnects after "done".
        if visitor_ip:
            await db.widget_conversations.update_one(
                {"company_id": company_id, "session_id": req.session_id},
                {"$set": {"visitor_ip": visitor_ip}},
                upsert=True,
            )

        # Create a modified user message that reminds the agent of the email address
        # This ensures the ticket creation tool has it without asking.
        # We don't save this prefix to the DB history, just pass it to the agent this turn.
        injected_message = f"[System Context: The current user's email address is {email}. Do NOT ask for their email address if you need to create a support ticket. Use this email address automatically.]\n\n{req.message}"

        # Serialize attachments for agent services
        attachments_raw = [a.model_dump() for a in req.attachments] if req.attachments else []
        
        stream_fns_to_try = [_AGENT_MAP.get(ak, _AGENT_MAP[_DEFAULT_AGENT]) for ak in agents_to_try]

        for idx, stream_fn in enumerate(stream_fns_to_try):
            try:
                response_text = ""
                # Call stream_fn with the injected message
                async for event in stream_fn(company_id, req.session_id, injected_message, user_id=None, attachments=attachments_raw, user_timestamp=user_timestamp):
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

        # Overwrite the last user message in the DB history to remove the injected context
        # so the user doesn't see the system prompt in their history.
        conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": req.session_id})
        if conversation and "messages" in conversation:
            messages = conversation["messages"]
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user" and "System Context:" in messages[i].get("content", ""):
                    # Restore original user message
                    messages[i]["content"] = req.message
                    break
            
            await db.widget_conversations.update_one(
                {"_id": conversation["_id"]},
                {"$set": {"messages": messages}}
            )

            # Format the session dict to return with 'done'
            from app.services.sdk_service import format_chat_session_dict
            session_dict = format_chat_session_dict(conversation)
            session_dict["created_at"] = session_dict["created_at"].isoformat()
            session_dict["updated_at"] = session_dict["updated_at"].isoformat()
            yield _sse("done", session=session_dict)
        else:
            yield _sse("done")

    except Exception as e:
        logger.exception(f"SDK Chat SSE error for company {company_id}")
        yield _sse("error", message="Something went wrong on our end. Please refresh and try again.")
        yield _sse("done")


@router.post("/{company_id}/chat", summary="Stream Chat Response (SDK)")
async def sdk_chat_endpoint(
    company_id: str,
    req: SdkChatRequest,
    request: Request,
    session: dict = Depends(get_sdk_session),
):
    """
    Stream agent chat responses as Server-Sent Events (SSE).
    Uses the authenticated SDK session token.
    """
    if session["company_id"] != company_id:
        raise HTTPException(status_code=403, detail="Your session is not authorized for this company. Please re-initialize the SDK.")

    email = session["email"]
    received_at = datetime.now(tz=timezone.utc).isoformat()
    return StreamingResponse(
        _sdk_chat_sse_generator(company_id, email, req, get_client_ip(request), received_at),
        media_type="text/event-stream",
    )


@router.post("/{company_id}/chat/upload", summary="Upload Files for SDK Chat")
async def sdk_upload_chat_files(
    company_id: str,
    request: Request,
    files: list[UploadFile] = File(..., description="One or more files to upload."),
    session: dict = Depends(get_sdk_session),
):
    """
    Upload files for SDK chat — returns attachment metadata.
    Uses the authenticated SDK session token.
    """
    if session["company_id"] != company_id:
        raise HTTPException(
            status_code=403, 
            detail="Your session is not authorized for this company. Please re-initialize the SDK."
        )

    # Re-use the core upload logic
    return await upload_chat_files(
        company_id=company_id,
        request=request,
        files=files,
        company={"id": company_id}
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
