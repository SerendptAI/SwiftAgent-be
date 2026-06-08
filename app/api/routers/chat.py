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

from fastapi import APIRouter, HTTPException, UploadFile, File, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator, Field
from typing import List, Literal, Optional

from app.core.database import db
from app.core.request_utils import get_client_ip
from app.core.sdk_auth import verify_api_key
from app.core.rate_limiter import rate_limit_chat
from app.core.plan_enforcement import enforce_chat_limit
from app.services import (
    anthropic_agent_service,
    openrouter_agent_service,
    gemini_agent_service,
    memory_service,
    cloudinary_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

AgentType = Literal["anthropic", "openrouter", "gemini"]


# Request / response schemas

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
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

_AGENT_MAP = {
    "openrouter": openrouter_agent_service.chat_stream,
    "anthropic": anthropic_agent_service.chat_stream,
    "gemini": gemini_agent_service.chat_stream,
}

_TITLE_MAP = {
    "openrouter": openrouter_agent_service.generate_chat_title,
    "anthropic": anthropic_agent_service.generate_chat_title,
    "gemini": gemini_agent_service.generate_chat_title,
}


def _resolve_agent(req: ChatRequest, company: dict):
    """
    Pick the streaming function to use.

    Priority (highest → lowest):
      1. req.agent   — explicitly set by the frontend/caller
      2. company.ai_provider — per-company setting stored in MongoDB
      3. _DEFAULT_AGENT — platform-wide fallback
    """
    agent_key = req.agent or company.get("ai_provider") or _DEFAULT_AGENT
    return _AGENT_MAP.get(agent_key, _AGENT_MAP[_DEFAULT_AGENT])


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
            for ak in agents_to_try:
                title_fn = _TITLE_MAP.get(ak, _TITLE_MAP[_DEFAULT_AGENT])
                subject = await title_fn(req.message)
                if subject and subject != "New Chat":
                    break
            
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
        if visitor_ip:
            await db.widget_conversations.update_one(
                {"company_id": company_id, "session_id": req.session_id},
                {"$set": {"visitor_ip": visitor_ip}},
                upsert=True,
            )

        stream_fns_to_try = [_AGENT_MAP.get(ak, _AGENT_MAP[_DEFAULT_AGENT]) for ak in agents_to_try]

        # Serialize attachments for agent services
        attachments_raw = [a.model_dump() for a in req.attachments] if req.attachments else []

        response_text = ""

        for idx, stream_fn in enumerate(stream_fns_to_try):
            try:
                response_text = ""
                async for event in stream_fn(
                    company_id, req.session_id, req.message,
                    req.user_id, req.page_url, attachments_raw,
                    user_timestamp=user_timestamp,
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
                if idx < len(stream_fns_to_try) - 1 and not response_text.strip():
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

        yield _sse("done")

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
