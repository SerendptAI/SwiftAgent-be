"""
Chat SSE router — streaming text-based agent responses via Server-Sent Events.

POST /{company_id}/chat
  Body: {
    "session_id": "...",
    "message": "...",
    "user_id": null,
    "agent": "anthropic" | "openrouter"   # optional, defaults to company setting or "anthropic"
    "attachments": [...]                  # optional, from POST /{company_id}/chat/upload
  }
  Returns: text/event-stream

POST /{company_id}/chat/upload
  Body: multipart/form-data with files
  Returns: { "attachments": [...] }

SSE event format:
  event: message
  data: {"data": {"stage": "<stage>", ...}}

Stages:
  chat_details  – session metadata
  thinking      – agent is working (with a user-friendly label)
  tool          – a specific tool is being invoked
  stream        – final agent reply text
  sources       – knowledge-base sources / blockchain data
  navigation_guide – visual guide steps
  done          – stream complete
"""

import json
import logging
import magic
from datetime import datetime, timezone
import re
import asyncio

from fastapi import APIRouter, HTTPException, UploadFile, File, Depends, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, StreamingResponse
from app.core.utils import format_timestamp_iso
from pydantic import BaseModel, field_validator, Field
from typing import List, Literal, Optional

from app.core.database import db
from app.core.request_utils import get_client_ip
from app.core.sdk_auth import verify_api_key
from app.core.rate_limiter import rate_limit_chat
from app.core.plan_enforcement import enforce_chat_limit
from app.services import (
    memory_service,
    cloudinary_service,
    company_email_service,
    notification_service,
)
import logging
import asyncio
import re
from uuid import uuid4

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

AgentType = Literal["anthropic", "openrouter", "gemini"]


# Request / response schemas

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/heif", "image/heic"}
ALLOWED_DOC_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/csv",
}
ALLOWED_DOC_EXTENSIONS = {"pdf", "docx", "txt", "csv"}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB


class ChatAttachment(BaseModel):
    url: str = Field(..., description="Cloudinary URL of the uploaded file.")
    type: str = Field(..., description="'image' or 'document'.")
    mime_type: Optional[str] = Field(None, description="MIME type of the file.")
    filename: Optional[str] = Field(None, description="Original filename.")


class ChatRequest(BaseModel):
    session_id: str = Field(..., description="Unique identifier for the chat session.")
    message: str = Field(..., description="The user's input message.")
    user_id: str = Field(None, description="Optional ID of the user (used for memory generation).")
    user_email: Optional[str] = Field(None, description="Optional email address of the user. If provided, chats become aggregatable across SDK/Web widget.")
    # Optional — if omitted, falls back to company.ai_provider, then global default
    agent: Optional[AgentType] = Field(
        None, 
        description="Select the AI provider to use. Provide 'openrouter' to route through the OpenRouter service, or 'anthropic' for Claude. If omitted, falls back to the company's ai_provider setting or the platform default."
    )
    page_url: Optional[str] = Field(None, description="The URL the user is currently viewing.")
    attachments: Optional[List[ChatAttachment]] = Field(
        None,
        description="Optional list of file attachments from the upload endpoint. Maximum 5.",
        max_length=5,
    )

    @field_validator("message")
    @classmethod
    def message_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Message cannot be empty")
        return v.strip()


def _sse(stage: str, **kwargs) -> str:
    """Format a single SSE event line."""
    payload = {"data": {"stage": stage, **kwargs}}
    return f"event: message\ndata: {json.dumps(payload)}\n\n"


# Global default agent — change here to swap the platform default.
_DEFAULT_AGENT: AgentType = "anthropic"

from app.services.graph.executor import chat_stream_graph
from app.services.graph.title_generator import generate_chat_title





async def _chat_sse_generator(
    company_id: str,
    req: ChatRequest,
    visitor_ip: str | None = None,
    user_timestamp: str | None = None,
):
    """SSE generator that wraps the chosen agent's streaming chat.

    ``user_timestamp`` is the request-arrival time (the moment the visitor sent
    the message). It is threaded into the agent so the persisted user message
    reflects true send-time rather than reply-completion time — otherwise a
    single-turn chat would record a near-zero communication duration.
    """
    try:
        company = await db.companies.find_one({"id": company_id})
        if not company:
            yield _sse("error", message="Company not found")
            yield _sse("done")
            return

        yield _sse("chat_details", session_id=req.session_id, company_id=company_id)

        agent_key = req.agent or company.get("ai_provider") or _DEFAULT_AGENT
        
        # Build a list of fallback agents to try
        agents_to_try = [agent_key]
        if agent_key == "anthropic" or (not req.agent and not company.get("ai_provider") and _DEFAULT_AGENT == "anthropic"):
            agents_to_try.append("openrouter")
            agents_to_try.append("gemini")

        # Handle AI generated chat subject
        conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": req.session_id})
        subject = conversation.get("subject") if conversation else None
        
        if not subject:
            subject = await generate_chat_title(req.message, agent_key)
            
            if not subject:
                subject = "New Chat"

            yield _sse("subject", subject=subject)
            await db.widget_conversations.update_one(
                {"company_id": company_id, "session_id": req.session_id},
                {"$set": {"subject": subject}},
                upsert=True
            )
        else:
            yield _sse("subject", subject=subject)

        # Link this conversation to the visitor (by IP) so the visitors
        # endpoint can attribute communication duration. Done pre-stream so it
        # runs reliably even if the client disconnects right after "done".
        update_fields = {}
        if visitor_ip and (not conversation or conversation.get("visitor_ip") != visitor_ip):
            update_fields["visitor_ip"] = visitor_ip
        if req.user_email and (not conversation or conversation.get("sdk_user_email") != req.user_email):
            update_fields["sdk_user_email"] = req.user_email

        if update_fields:
            await db.widget_conversations.update_one(
                {"company_id": company_id, "session_id": req.session_id},
                {"$set": update_fields},
                upsert=True,
            )

        if company.get("route_to_human"):
            yield _sse("thinking", message="Routing to a human agent...")
            
            email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', req.message)
            found_email = email_match.group(0) if email_match else None
            
            customer_email = found_email or req.user_email
            ticket_id = conversation.get("ticket_id") if conversation else None
            
            if not customer_email and conversation:
                customer_email = conversation.get("sdk_user_email")
                
            if not customer_email and ticket_id:
                ticket = await db.email_tickets.find_one({"id": ticket_id, "company_id": company_id})
                if ticket:
                    customer_email = ticket.get("customer_email")
                    
            now = datetime.now(tz=timezone.utc).isoformat()
            user_msg_doc = {
                "id": str(uuid4()),
                "role": "user",
                "content": req.message,
                "timestamp": user_timestamp or now
            }
            await db.widget_conversations.update_one(
                {"company_id": company_id, "session_id": req.session_id},
                {"$push": {"messages": user_msg_doc}},
                upsert=True
            )
            
            if not conversation:
                conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": req.session_id})
            
            if customer_email:
                
                if ticket_id:
                    inbound_msg = {
                        "direction": "inbound",
                        "body_text": req.message,
                        "body_html": None,
                        "sender_email": customer_email,
                        "message_id": None,
                        "timestamp": datetime.now(tz=timezone.utc),
                        "seen": False,
                    }
                    await db.email_tickets.update_one(
                        {"id": ticket_id, "company_id": company_id},
                        {
                            "$push": {"messages": inbound_msg},
                            "$set": {"status": "follow_up", "updated_at": datetime.now(tz=timezone.utc)},
                            "$inc": {"unseen_count": 1},
                        }
                    )
                    asyncio.create_task(
                        notification_service.notify_company(
                            company_id=company_id,
                            title="💬 New Ticket Reply",
                            body=f"Customer {customer_email} replied to Ticket #{ticket_id}",
                            type="ticket_reply",
                            data={"ticket_id": ticket_id}
                        )
                    )
                    ticket_doc = await db.email_tickets.find_one({"id": ticket_id, "company_id": company_id})
                    if ticket_doc:
                        asyncio.create_task(company_email_service._send_new_message_email(company, ticket_doc, inbound_msg))
                    
                    reply_text = f"Your message has been sent to our human support team. We will continue to reach out to you at {customer_email} shortly."
                else:
                    subject = conversation.get("subject", "New Chat") if conversation else "New Chat"
                    chat_summary = req.message
                    if conversation and "messages" in conversation:
                        history_texts = [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in conversation.get("messages", [])]
                        if not any(m.get("content") == req.message and m.get("role") == "user" for m in conversation.get("messages", [])):
                            history_texts.append(f"user: {req.message}")
                        chat_summary = "\n\n".join(history_texts)
                        
                    ticket = await company_email_service.create_ticket(
                        company_id=company_id,
                        customer_email=customer_email,
                        subject=subject,
                        chat_summary=chat_summary,
                        chat_session_id=req.session_id,
                        customer_name=None
                    )
                    reply_text = f"Thank you! Your chat has been escalated to our human support team as Ticket #{ticket['id']}. We will reach out to you at {customer_email} shortly."
                    
                assistant_msg_doc = {
                    "id": str(uuid4()),
                    "role": "assistant",
                    "content": reply_text,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat()
                }
                await db.widget_conversations.update_one(
                    {"company_id": company_id, "session_id": req.session_id},
                    {"$push": {"messages": assistant_msg_doc}},
                )
                
                yield _sse("stream", message=reply_text)
                yield _sse("done", message_id=assistant_msg_doc["id"])
                return
            else:
                reply_text = "You are speaking with our human support team! Please provide your email address below so we can track your request and get back to you shortly."
                assistant_msg_doc = {
                    "id": str(uuid4()),
                    "role": "assistant",
                    "content": reply_text,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat()
                }
                await db.widget_conversations.update_one(
                    {"company_id": company_id, "session_id": req.session_id},
                    {"$push": {"messages": assistant_msg_doc}},
                )
                yield _sse("stream", message=reply_text)
                yield _sse("done", message_id=assistant_msg_doc["id"])
                return

        # Serialize attachments for agent services
        attachments_raw = [a.model_dump() for a in req.attachments] if req.attachments else []

        actual_message_to_send = req.message
        if req.user_email:
            actual_message_to_send = f"[System Context: The current user's email address is {req.user_email}. Do NOT ask for their email address if you need to create a support ticket. Use this email address automatically.]\n\n{req.message}"

        response_text = ""

        for idx, provider_key in enumerate(agents_to_try):
            try:
                response_text = ""
                async for event in chat_stream_graph(
                    company_id=company_id,
                    session_id=req.session_id,
                    message=actual_message_to_send,
                    user_id=req.user_id,
                    page_url=req.page_url,
                    attachments=attachments_raw,
                    user_timestamp=user_timestamp,
                    agent_provider=provider_key,
                    sdk_user_email=req.user_email,
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
                        yield _sse(
                            "sources",
                            sources=event.get("sources", []),
                            blockchain_data=event.get("blockchain_data"),
                        )

                    elif event_type == "navigation_guide":
                        guide = event.get("guide", {})
                        yield _sse(
                            "navigation_guide",
                            steps=guide.get("steps", []),
                            path_summary=guide.get("path_summary", []),
                        )

                    elif event_type == "error":
                        raise Exception(event.get('message') or "Agent stream yielded an error")

                # If successful, break the retry loop
                break

            except Exception as e:
                # If we have another fallback agent and haven't sent real text yet
                if idx < len(agents_to_try) - 1 and not response_text.strip():
                    logger.warning(f"Agent stream failed: {e}. Falling back to next agent...")
                    yield _sse("thinking", message="Switching AI providers...")
                    continue
                
                # Final failure
                logger.error(f"Agent error for company {company_id}: {e}")
                friendly = "I'm having trouble right now. Please try again in a moment."
                if not response_text.strip():
                    response_text = friendly
                yield _sse("stream", message=friendly)
                break

        last_msg_id = None
        convo_doc = await db.widget_conversations.find_one({"company_id": company_id, "session_id": req.session_id})
        if convo_doc and "messages" in convo_doc and len(convo_doc["messages"]) > 0:
            last_msg_id = convo_doc["messages"][-1].get("id")
        yield _sse("done", message_id=last_msg_id)

        # generate session memory summary after conversation ends
        if req.user_id:
            conversation = await db.widget_conversations.find_one(
                {"company_id": company_id, "session_id": req.session_id}
            )
            if conversation and len(conversation.get("messages", [])) > 2:
                history = conversation.get("messages", [])
                await memory_service.generate_session_summary(
                    session_id=req.session_id,
                    company_id=company_id,
                    user_id=req.user_id,
                    messages=history,
                    tools_used=[],
                    outcome="completed",
                )
                await memory_service.delete_working_memory(req.session_id)

    except Exception:
        logger.exception(f"Chat SSE error for company {company_id}")
        yield _sse("error", message="Something went wrong on our end. Please refresh and try again.")
        yield _sse("done")

# Endpoints

@router.post(
    "/{company_id}/chat/upload",
    summary="Upload Files for Chat",
    description=(
        "Upload one or more files (images or documents) for use in a chat message. "
        "Files are stored temporarily in Cloudinary (auto-deleted after 2 hours). "
        "Returns attachment metadata to include in the chat request body.\n\n"
        "**Supported image types:** JPEG, PNG, GIF, WebP\n"
        "**Supported document types:** PDF, DOCX, TXT, CSV\n"
        "**Max file size:** 10 MB per file"
    ),
)
async def upload_chat_files(
    company_id: str,
    request: Request,
    files: List[UploadFile] = File(..., description="One or more files to upload."),
    company: dict = Depends(verify_api_key),
):
    """Upload files for chat — returns attachment metadata."""
    await rate_limit_chat(request)
    if company["id"] != company_id:
        raise HTTPException(status_code=403, detail="API key does not belong to the requested company_id")

    if len(files) > 5:
        raise HTTPException(status_code=400, detail="Maximum of 5 files can be uploaded at once.")

    attachments = []
    for file in files:
        contents = await file.read()
        if len(contents) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"File '{file.filename}' exceeds the 10 MB size limit.",
            )

        content_type = file.content_type or ""
        ext = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""

        # Validate magic bytes
        detected_mime = magic.from_buffer(contents, mime=True)
        if detected_mime not in ALLOWED_IMAGE_TYPES and detected_mime not in ALLOWED_DOC_TYPES:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported or malformed file: '{file.filename}'. "
                       f"Detected type '{detected_mime}' is not allowed.",
            )

        if detected_mime in ALLOWED_IMAGE_TYPES:
            await file.seek(0)
            result = await cloudinary_service.upload_chat_image(
                file, folder=f"chat/{company_id}"
            )
            attachments.append({
                "url": result["secure_url"],
                "type": "image",
                "mime_type": detected_mime,
                "filename": file.filename,
            })

        elif detected_mime in ALLOWED_DOC_TYPES or ext in ALLOWED_DOC_EXTENSIONS:
            result = await cloudinary_service.upload_chat_document(
                contents, file.filename, folder=f"chat/{company_id}"
            )
            attachments.append({
                "url": result["secure_url"],
                "type": "document",
                "mime_type": detected_mime,
                "filename": file.filename,
            })

        else:
            raise HTTPException(
                status_code=415,
                detail=f"Unsupported file type: '{detected_mime}' for file '{file.filename}'. "
                       f"Supported: images (JPEG, PNG, GIF, WebP) and documents (PDF, DOCX, TXT, CSV).",
            )

    return {"attachments": attachments}


@router.post(
    "/{company_id}/chat",
    summary="Stream Chat Response",
    description=(
        "Stream agent chat responses as Server-Sent Events (SSE).\n\n"
        "**Using OpenRouter:**\n"
        "To explicitly use the **OpenRouter** model (e.g., Llama 3.3 70B), include `\"agent\": \"openrouter\"` in the request body. "
        "If the `agent` field is omitted, it will fall back to the company's configured `ai_provider` or the platform default.\n\n"
        "**File Attachments:**\n"
        "Upload files first via `POST /{company_id}/chat/upload`, then include the returned "
        "`attachments` array in this request body."
    )
)
async def chat_endpoint(
    company_id: str,
    req: ChatRequest,
    request: Request,
    company: dict = Depends(verify_api_key),
):
    """Stream agent chat responses as Server-Sent Events."""
    await rate_limit_chat(request)
    if company["id"] != company_id:
        raise HTTPException(status_code=403, detail="API key does not belong to the requested company_id")
    if not company_id or not company_id.strip():
        raise HTTPException(status_code=400, detail="company_id is required")
        
    await enforce_chat_limit(company)

    # Stamp send-time at request arrival so duration reflects real talk-time.
    received_at = datetime.now(tz=timezone.utc).isoformat()

    return StreamingResponse(
        _chat_sse_generator(company_id, req, get_client_ip(request), received_at),
        media_type="text/event-stream",
    )


async def get_chat_history(company_id: str, session_id: str):
    """
    Internal helper to fetch the full history of a chat session, including any escalated ticket replies.
    Used by the chat WebSocket to push history state.
    """
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    chat = await db.widget_conversations.find_one({
        "company_id": company_id,
        "session_id": session_id
    })
    
    if not chat:
        raise HTTPException(status_code=404, detail="Chat session not found")

    ai_name = company.get("name") if company else "AI Assistant"
    ai_avatar_url = company.get("logo_url") if company else None

    formatted_messages = []
    
    for m in chat.get("messages", []):
        role = m.get("role", "user")
        is_human_agent = bool(m.get("agent_name"))
        formatted_messages.append({
            "role": role,
            "content": m.get("content", ""),
            "timestamp": format_timestamp_iso(m.get("timestamp")),
            "attachments": m.get("attachments"),
            "author_name": m.get("agent_name") if is_human_agent else (ai_name if role == "assistant" else None),
            "avatar_url": m.get("agent_avatar_url") if is_human_agent else (ai_avatar_url if role == "assistant" else None)
        })

    # 2. Add messages from the escalated ticket (if any)
    ticket_id = chat.get("ticket_id")
    resolved = False
    
    if ticket_id:
        ticket = await db.email_tickets.find_one({
            "company_id": company_id,
            "id": ticket_id
        })
        if ticket:
            resolved = ticket.get("status") == "resolved"
            for m in ticket.get("messages", []):
                direction = m.get("direction", "user")
                
                # Skip the initial system message containing the chat summary
                if direction == "system":
                    continue
                    
                # In tickets, direction="inbound" is the user, "outbound" is agent
                role = "assistant" if direction == "outbound" else "user"
                    
                formatted_messages.append({
                    "role": role,
                    "content": m.get("body_text", ""),
                    "timestamp": format_timestamp_iso(m.get("timestamp")),
                    "attachments": m.get("attachments"),
                    "author_name": m.get("agent_name") or (ai_name if role == "assistant" else None),
                    "avatar_url": m.get("agent_avatar_url") or (ai_avatar_url if role == "assistant" else None)
                })

    return {
        "session_id": session_id,
        "ticket_id": ticket_id,
        "resolved": resolved,
        "messages": formatted_messages
    }


@router.websocket("/{company_id}/chat/{session_id}/ws")
async def chat_websocket(
    websocket: WebSocket,
    company_id: str,
    session_id: str
):
    """
    Real-time WebSocket for a specific chat session on the unauthenticated widget.
    Pushes the full chat history when new messages are appended to the chat or its escalated ticket.
    """
    await websocket.accept()

    company = await db.companies.find_one({"id": company_id})
    if not company:
        await websocket.close(code=1008)
        return

    # Initial push
    try:
        res = await get_chat_history(company_id, session_id)
        await websocket.send_json({"type": "init", "data": res})
    except Exception as e:
        logger.error(f"Error fetching initial history for chat WS: {e}")
        await websocket.close(code=1011)
        return

    try:
        async def watch_chat():
            pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}]
            async with db.widget_conversations.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    full_doc = change.get("fullDocument")
                    if full_doc and full_doc.get("company_id") == company_id and full_doc.get("session_id") == session_id:
                        res = await get_chat_history(company_id, session_id)
                        await websocket.send_json({"type": "update", "data": res})

        async def watch_ticket():
            pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}]
            async with db.email_tickets.watch(pipeline, full_document="updateLookup") as stream:
                async for change in stream:
                    full_doc = change.get("fullDocument")
                    if full_doc and full_doc.get("company_id") == company_id and full_doc.get("chat_session_id") == session_id:
                        res = await get_chat_history(company_id, session_id)
                        await websocket.send_json({"type": "update", "data": res})

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
        logger.error(f"Chat WS error: {e}")
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass
