"""
Chat SSE router — streaming text-based agent responses via Server-Sent Events.

POST /{company_id}/chat
  Body: {
    "session_id": "...",
    "message": "...",
    "user_id": null,
    "agent": "anthropic" | "openrouter"   # optional, defaults to company setting or "anthropic"
  }
  Returns: text/event-stream

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

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from typing import Literal, Optional

from app.core.database import db
from app.services import anthropic_agent_service, openrouter_agent_service, memory_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

AgentType = Literal["anthropic", "openrouter"]


class ChatRequest(BaseModel):
    session_id: str
    message: str
    user_id: str = None
    # Optional — if omitted, falls back to company.ai_provider, then global default
    agent: Optional[AgentType] = None

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


async def _chat_sse_generator(company_id: str, req: ChatRequest):
    """SSE generator that wraps the chosen agent's streaming chat."""
    try:
        company = await db.companies.find_one({"id": company_id})
        if not company:
            yield _sse("error", message="Company not found")
            yield _sse("done")
            return

        yield _sse("chat_details", session_id=req.session_id, company_id=company_id)

        stream_fn = _resolve_agent(req, company)
        response_text = ""

        async for event in stream_fn(company_id, req.session_id, req.message, req.user_id):
            event_type = event.get("type")

            if event_type == "thinking":
                yield _sse("thinking", message=event.get("message", ""))

            elif event_type == "tool":
                yield _sse("tool", name=event.get("name", ""), label=event.get("label", ""))

            elif event_type == "text":
                content = event.get("content", "")
                response_text = content
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
                logger.error(f"Agent error for company {company_id}: {event.get('message')}")
                friendly = "I'm having trouble right now. Please try again in a moment."
                if not response_text.strip():
                    response_text = friendly
                yield _sse("stream", message=friendly)

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
        yield _sse("error", message="An internal error occurred")
        yield _sse("done")


@router.post("/{company_id}/chat")
async def chat_endpoint(company_id: str, req: ChatRequest):
    """Stream agent chat responses as Server-Sent Events."""
    if not company_id or not company_id.strip():
        raise HTTPException(status_code=400, detail="company_id is required")
    return StreamingResponse(
        _chat_sse_generator(company_id, req),
        media_type="text/event-stream",
    )
