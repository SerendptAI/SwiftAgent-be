"""
SDK Router — API endpoints for third-party SDK consumers (mobile/web).
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status, UploadFile, File, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.encoders import jsonable_encoder

from app.core.security import create_access_token, decode_access_token
from app.core.request_utils import get_client_ip
from app.core.sdk_auth import get_sdk_session, verify_api_key
from app.core.plan_enforcement import enforce_chat_limit
from app.core.database import db
from app.models.sdk_models import (
    SdkChatRequest,
    SdkConversationDetail,
    SdkConversationListResponse,
    SdkInitRequest,
    SdkInitResponse,
)
from app.services import sdk_service, company_email_service
from app.services.sdk_service import format_chat_session_dict
from app.api.routers.chat import _DEFAULT_AGENT, _sse, upload_chat_files
from app.services.graph.executor import chat_stream_graph
from app.services.graph.title_generator import generate_chat_title
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

    await enforce_chat_limit(company)

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
            "enable_suggested_prompts": company.get("enable_suggested_prompts", True),
        },
        "user": user_data,
    }


async def _sdk_chat_sse_generator(company_id: str, email: str, req: SdkChatRequest, visitor_ip: str | None = None, user_timestamp: str | None = None):
    """SSE generator for SDK chat, injecting user email into context."""
    try:
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
        
        # Check if the session_id is a ticket_id or escalated chat
        ticket = await db.email_tickets.find_one({
            "company_id": company_id,
            "$or": [
                {"id": req.session_id},
                {"chat_session_id": req.session_id}
            ]
        })
        ticket_id = ticket["id"] if ticket else None
        
        is_escalated = False
        if conversation and conversation.get("escalated"):
            is_escalated = True
            if not ticket_id:
                ticket_id = conversation.get("ticket_id")

        attachments_raw = [a.model_dump() for a in req.attachments] if req.attachments else []
        now_utc = datetime.now(tz=timezone.utc)
        now_iso = now_utc.isoformat()

        subject = conversation.get("subject") if conversation else None
        
        update_set = {"updated_at": now_utc}
        update_setOnInsert = {
            "avatar": get_random_avatar(),
            "created_at": now_utc
        }
        push_op = {}

        if not subject:
            subject = await generate_chat_title(req.message, agent_key)
            if not subject:
                subject = "New Chat"
            yield _sse("subject", subject=subject)
            update_set["sdk_user_email"] = email
            update_set["source"] = "sdk"
            update_set["subject"] = subject
        else:
            yield _sse("subject", subject=subject)
            if not conversation or conversation.get("sdk_user_email") != email or conversation.get("source") != "sdk":
                update_set["sdk_user_email"] = email
                update_set["source"] = "sdk"

        if visitor_ip and (not conversation or conversation.get("visitor_ip") != visitor_ip):
            update_set["visitor_ip"] = visitor_ip

        # Save user message to widget_conversations if it's a chat that is NOT YET escalated
        if not is_escalated and not ticket:
            user_msg_doc = {
                "id": str(uuid4()),
                "role": "user",
                "content": req.message,
                "timestamp": user_timestamp or now_iso,
                "attachments": attachments_raw
            }
            push_op["messages"] = user_msg_doc

        update_payload = {"$set": update_set}
        if push_op:
            update_payload["$push"] = push_op
        if not conversation:
            update_payload["$setOnInsert"] = update_setOnInsert

        await db.widget_conversations.update_one(
             {"company_id": company_id, "session_id": req.session_id},
             update_payload,
             upsert=True
        )

        if company.get("route_to_human") or is_escalated or ticket_id:
            # User message was already pushed to widget_conversations above if applicable
            if ticket_id:
                try:
                    await company_email_service.add_inbound_ticket_message(
                        company_id=company_id,
                        ticket_id=ticket_id,
                        sender_email=email,
                        body_text=req.message,
                        timestamp=datetime.now(tz=timezone.utc),
                        attachments=attachments_raw
                    )
                except ValueError as e:
                    yield _sse("stream", message=f"Sorry, this ticket cannot be replied to: {e}")
                    yield _sse("done")
                    return
                    
                if not ticket or is_escalated:
                    # User is still in the chat view, so stream a quick confirmation.
                    reply_text = f"Your message has been sent to our human support team. We will continue to reach out to you at {email} shortly."
                    assistant_msg_doc = {
                        "id": str(uuid4()),
                        "role": "assistant",
                        "content": reply_text,
                        "timestamp": datetime.now(tz=timezone.utc).isoformat()
                    }
                    if not is_escalated:
                        await db.widget_conversations.update_one(
                            {"company_id": company_id, "session_id": req.session_id},
                            {
                                "$push": {"messages": assistant_msg_doc},
                                "$set": {"updated_at": datetime.now(tz=timezone.utc)}
                            },
                        )
                    yield _sse("stream", message=reply_text)
                    
            else:
                yield _sse("thinking", message="Routing to a human agent...")
                subject = conversation.get("subject", "New Chat") if conversation else "New Chat"
                chat_summary = req.message
                if conversation and "messages" in conversation:
                    history_texts = [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in conversation.get("messages", [])]
                    if not any(m.get("content") == req.message and m.get("role") == "user" for m in conversation.get("messages", [])):
                        history_texts.append(f"user: {req.message}")
                    chat_summary = "\n\n".join(history_texts)
                    
                new_ticket = await company_email_service.create_ticket(
                    company_id=company_id,
                    customer_email=email,
                    subject=subject,
                    chat_summary=chat_summary,
                    chat_session_id=req.session_id,
                    customer_name=None
                )
                
                await db.widget_conversations.update_one(
                    {"company_id": company_id, "session_id": req.session_id},
                    {"$set": {
                        "escalated": True, 
                        "ticket_id": new_ticket["id"],
                        "updated_at": datetime.now(tz=timezone.utc)
                    }}
                )
                
                reply_text = f"Thank you! Your chat has been escalated to our human support team as Ticket #{new_ticket['id']}. We will reach out to you at {email} shortly."
                
                assistant_msg_doc = {
                    "id": str(uuid4()),
                    "role": "assistant",
                    "content": reply_text,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat()
                }
                await db.widget_conversations.update_one(
                    {"company_id": company_id, "session_id": req.session_id},
                    {
                        "$push": {"messages": assistant_msg_doc},
                        "$set": {"updated_at": datetime.now(tz=timezone.utc)}
                    },
                )
                yield _sse("stream", message=reply_text)
                
            yield _sse("done")
            return

        # Create a modified user message that reminds the agent of the email address
        # This ensures the ticket creation tool has it without asking.
        # We don't save this prefix to the DB history, just pass it to the agent this turn.
        injected_message = f"[System Context: The current user's email address is {email}. Do NOT ask for their email address if you need to create a support ticket. Use this email address automatically.]\n\n{req.message}"

        # Attachments already serialized above
        # attachments_raw = [a.model_dump() for a in req.attachments] if req.attachments else []
        
        for idx, provider_key in enumerate(agents_to_try):
            try:
                response_text = ""
                # Call chat_stream_graph with the injected message
                async for event in chat_stream_graph(
                    company_id=company_id,
                    session_id=req.session_id,
                    message=req.message,
                    user_id=None,
                    page_url=None,
                    attachments=attachments_raw,
                    user_timestamp=user_timestamp,
                    agent_provider=provider_key,
                    sdk_user_email=email,
                    llm_message=injected_message,
                ):
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
                if idx < len(agents_to_try) - 1 and not response_text.strip():
                    logger.warning(f"SDK Agent stream failed: {e}. Falling back to next agent...")
                    yield _sse("thinking", message="Switching AI providers...")
                    continue
                
                logger.error(f"SDK Agent error for company {company_id}: {e}")
                friendly = "I'm having trouble right now. Please try again in a moment."
                if not response_text.strip():
                    response_text = friendly
                yield _sse("stream", message=friendly)
                break

        # Context scrubbing is now handled directly by the agent services,
        # so we don't need a redundant DB update here anymore.
        conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": req.session_id})
        if conversation:
            # Format the session dict to return with 'done'
            session_dict = format_chat_session_dict(conversation)
            session_dict["created_at"] = session_dict["created_at"].isoformat()
            session_dict["updated_at"] = session_dict["updated_at"].isoformat()
            last_msg_id = None
            if "messages" in conversation and len(conversation["messages"]) > 0:
                last_msg_id = conversation["messages"][-1].get("id")
            yield _sse("done", session=session_dict, message_id=last_msg_id)
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

    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    await enforce_chat_limit(company)

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


@router.post("/{company_id}/tickets/{ticket_id}/reopen")
async def sdk_reopen_ticket(
    company_id: str,
    ticket_id: str,
    session: dict = Depends(get_sdk_session),
):
    """
    Reopen a ticket from the SDK.
    """
    if session["company_id"] != company_id:
        raise HTTPException(status_code=403, detail="Your session is not authorized for this company. Please re-initialize the SDK.")

    try:
        result = await company_email_service.reopen_ticket(company_id, ticket_id)
        if not result:
            raise HTTPException(status_code=404, detail="Ticket not found or not resolved")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"status": "reopened", "ticket_id": ticket_id}

@router.websocket("/{company_id}/conversations/ws")
async def sdk_conversations_websocket(
    websocket: WebSocket,
    company_id: str,
    token: str = Query(...)
):
    """
    Real-time WebSocket for SDK conversations (chats and tickets).
    Requires a valid JWT token passed as a query parameter (?token=...).
    """
    await websocket.accept()

    try:
        payload = decode_access_token(token)
        if not payload or not payload.get("sub") or payload.get("type") != "sdk":
            await websocket.send_json({"type": "error", "message": "Invalid or missing SDK token"})
            await websocket.close(code=1008)
            return
            
        email = payload.get("sub")
        if payload.get("company_id") != company_id:
            await websocket.send_json({"type": "error", "message": "Token not authorized for this company"})
            await websocket.close(code=1008)
            return
            
    except Exception as e:
        logger.error(f"SDK WebSocket auth failed: {e}")
        await websocket.close(code=1008)
        return

    # Initial push of conversations
    try:
        result = await sdk_service.get_conversation_history(company_id, email)
        await websocket.send_json({
            "type": "init",
            "data": jsonable_encoder(result)
        })
    except Exception as e:
        logger.error(f"Error fetching initial conversations for SDK WS: {e}")
        await websocket.close()
        return
    try:
        # Watch for changes in widget_conversations and email_tickets
        # We use a loop with short timeouts or two separate change streams.
        # But we can just use two tasks.
        
        async def watch_chats():
            pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}]
            async with db.widget_conversations.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    full_doc = change.get("fullDocument")
                    if full_doc and full_doc.get("company_id") == company_id and full_doc.get("sdk_user_email") == email:
                        res = await sdk_service.get_conversation_history(company_id, email)
                        await websocket.send_json({"type": "update", "data": jsonable_encoder(res)})

        async def watch_tickets():
            pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}]
            async with db.email_tickets.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    # If this is an insert (i.e. ticket just created via AI agent tool), 
                    # we delay sending the update to the frontend by a few seconds.
                    # This allows the AI agent's ongoing SSE stream to finish sending its final message gracefully.
                    if change.get("operationType") == "insert":
                        await asyncio.sleep(2.5)
                        
                    full_doc = change.get("fullDocument")
                    if full_doc and full_doc.get("company_id") == company_id and full_doc.get("customer_email") == email:
                        res = await sdk_service.get_conversation_history(company_id, email)
                        await websocket.send_json({"type": "update", "data": jsonable_encoder(res)})

        chat_task = asyncio.create_task(watch_chats())
        ticket_task = asyncio.create_task(watch_tickets())

        # Keep alive loop
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            chat_task.cancel()
            ticket_task.cancel()

    except Exception as e:
        logger.error(f"SDK WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass

@router.websocket("/{company_id}/conversations/{conversation_id}/ws")
async def sdk_conversation_detail_websocket(
    websocket: WebSocket,
    company_id: str,
    conversation_id: str,
    token: str = Query(...)
):
    """
    Real-time WebSocket for a specific conversation's messages.
    """
    await websocket.accept()

    try:
        payload = decode_access_token(token)
        if not payload or not payload.get("sub") or payload.get("type") != "sdk":
            await websocket.send_json({"type": "error", "message": "Invalid or missing SDK token"})
            await websocket.close(code=1008)
            return
            
        email = payload.get("sub")
        if payload.get("company_id") != company_id:
            await websocket.send_json({"type": "error", "message": "Token not authorized for this company"})
            await websocket.close(code=1008)
            return
            
    except Exception as e:
        logger.error(f"SDK WebSocket auth failed: {e}")
        await websocket.close(code=1008)
        return

    # Initial push
    try:
        detail = await sdk_service.get_conversation_detail(company_id, email, conversation_id)
        if detail:
            await websocket.send_json({"type": "init", "data": jsonable_encoder(detail)})
    except Exception as e:
        logger.error(f"Error fetching initial conversation detail for SDK WS: {e}")
        await websocket.close()
        return

    try:
        async def watch_chat():
            pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}, "fullDocument.session_id": conversation_id}}]
            async with db.widget_conversations.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    full_doc = change.get("fullDocument")
                    if full_doc and full_doc.get("company_id") == company_id and full_doc.get("sdk_user_email") == email:
                        res = await sdk_service.get_conversation_detail(company_id, email, conversation_id)
                        await websocket.send_json({"type": "update", "data": jsonable_encoder(res)})

        async def watch_ticket():
            pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}, "$or": [{"fullDocument.id": conversation_id}, {"fullDocument.chat_session_id": conversation_id}]}}]
            async with db.email_tickets.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    # Delay sending the insert event so the AI agent's SSE stream can finish gracefully
                    if change.get("operationType") == "insert":
                        await asyncio.sleep(2.5)

                    full_doc = change.get("fullDocument")
                    if full_doc and full_doc.get("company_id") == company_id and full_doc.get("customer_email") == email:
                        res = await sdk_service.get_conversation_detail(company_id, email, conversation_id)
                        await websocket.send_json({"type": "update", "data": jsonable_encoder(res)})

        chat_task = asyncio.create_task(watch_chat())
        ticket_task = asyncio.create_task(watch_ticket())

        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            chat_task.cancel()
            ticket_task.cancel()

    except Exception as e:
        logger.error(f"SDK detail WebSocket change stream error: {e}")
        try:
            await websocket.close()
        except:
            pass
