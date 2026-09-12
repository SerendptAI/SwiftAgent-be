"""
Ticket Copilot Service — AI Draft Replies & Next-Best-Action Engine
===================================================================
Grounded in empirical Human-in-the-Loop research (Brynjolfsson et al., QJE 2025;
CRMArena ACL 2025).

Provides:
- 1-click context-aware, empathetic draft replies for human support agents.
- Grounding via ticket messages, structured handoff context, and Qdrant RAG docs.
- Next-best-action recommendations (status transitions, priority calibration, internal notes).
- 1-click action execution with audit logging.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.database import db
from app.services import (
    company_email_service,
    company_service,
    knowledge_service,
    ticket_service,
)
from app.services.graph.llm_factory import get_llm

logger = logging.getLogger(__name__)

COPILOT_SYSTEM_PROMPT = """You are an expert AI Copilot embedded inside a customer support ticketing dashboard.
Your goal is to assist human support agents by:
1. Drafting an empathetic, accurate, and professional response to the customer.
2. Recommending high-confidence Next-Best-Actions (e.g. status transition, SLA priority adjustment, internal notes).

Guidelines for the response:
- Personalize the greeting with the customer's name if available.
- Directly acknowledge their frustration, friction, or failed steps if indicated in the handoff context.
- Ground your answer in the provided company Knowledge Base context. If information is not in the context, do not hallucinate; offer a helpful path forward.
- Adapt your voice to the requested tone: {tone}.
- If the agent gave specific instructions, strictly follow them: {instruction}.

Respond ONLY with valid JSON in the exact structure below, with no markdown code blocks or surrounding text:
{{
  "suggested_reply": "string (the drafted email reply to the customer)",
  "suggested_subject": "string or null (subject line if helpful)",
  "customer_sentiment": "frustrated | negative | neutral | positive",
  "reasoning": "string (brief justification for human agent)",
  "suggested_actions": [
    {{
      "action_type": "status_change | priority_change | internal_note",
      "label": "string (clear human-facing button label, e.g. 'Mark as Awaiting Customer')",
      "confidence": 0.95,
      "parameters": {{}},
      "reason": "string"
    }}
  ]
}}
"""


def _extract_last_customer_query(messages: List[Dict[str, Any]], default_query: str) -> str:
    """Find the most recent customer query to use for RAG search."""
    for m in reversed(messages):
        role = m.get("role") or m.get("direction")
        if role in ("user", "inbound"):
            content = m.get("content") or m.get("body_text")
            if content and content.strip():
                return content.strip()
    return default_query


def _build_rule_based_fallback(
    ticket: Dict[str, Any],
    handoff_ctx: Dict[str, Any],
    tone: str,
    sources: List[str],
    instruction: Optional[str] = None,
) -> Dict[str, Any]:
    """Reliable fallback when LLM API keys are unconfigured or provider times out."""
    customer_name = ticket.get("customer_name") or "there"
    subject = ticket.get("subject") or "your support request"
    sentiment = handoff_ctx.get("customer_sentiment") or "neutral"
    summary = handoff_ctx.get("conversation_summary") or "your issue"
    suggested_actions_raw = handoff_ctx.get("suggested_actions") or []

    empathy_line = "Thank you for reaching out to our support team."
    if sentiment in ("frustrated", "negative"):
        empathy_line = (
            f"I truly apologize for the frustration and inconvenience you've experienced with {summary}."
        )

    action_details = ""
    if suggested_actions_raw:
        action_details = f"\n\nI have reviewed the previous attempts and am following up to {suggested_actions_raw[0].lower()}."

    instruction_line = f"\n\n{instruction}" if instruction else ""

    draft = (
        f"Hi {customer_name},\n\n"
        f"{empathy_line}"
        f"{action_details}"
        f"{instruction_line}\n\n"
        f"Please let me know if you need anything else, and I'll be more than happy to help.\n\n"
        f"Best regards,\nSupport Team"
    )

    actions: List[Dict[str, Any]] = []

    # Propose status transition
    current_status = ticket.get("status", "pending")
    if current_status in ("pending", "in_progress", "open"):
        actions.append({
            "action_type": "status_change",
            "label": "Move to 'Awaiting Customer'",
            "confidence": 0.90,
            "parameters": {"status": "awaiting_customer"},
            "reason": "Agent reply is queued; ticket will await customer response.",
        })

    # Propose priority upgrade if frustrated and priority is not already urgent
    current_priority = ticket.get("priority", "medium")
    if sentiment in ("frustrated", "negative") and current_priority != "urgent":
        actions.append({
            "action_type": "priority_change",
            "label": "Escalate Priority to 'Urgent'",
            "confidence": 0.85,
            "parameters": {"priority": "urgent"},
            "reason": f"Customer sentiment is {sentiment}; prioritizing SLA response.",
        })

    # Propose internal note
    actions.append({
        "action_type": "internal_note",
        "label": "Log Copilot Diagnostic Note",
        "confidence": 0.80,
        "parameters": {"note": f"Copilot drafted reply addressing {summary}."},
        "reason": "Leaves an audit record of diagnostic review.",
    })

    return {
        "suggested_reply": draft,
        "suggested_subject": f"Re: {subject}",
        "customer_sentiment": sentiment,
        "sources_used": sources,
        "suggested_actions": actions,
        "reasoning": f"Generated based on customer sentiment ({sentiment}) and handoff brief.",
    }


async def generate_copilot_reply(
    company_id: str,
    ticket_id: str,
    tone: str = "empathic_professional",
    instruction: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a context-grounded suggested reply and next actions for a ticket."""
    ticket_context = await company_email_service.get_ticket_with_chat(company_id, ticket_id)
    if not ticket_context or not ticket_context.get("ticket"):
        raise ValueError(f"Ticket {ticket_id} not found for company {company_id}")

    ticket = ticket_context["ticket"]
    attributed_chat = ticket_context.get("attributed_chat") or {}
    company = await company_service.get_company(company_id)
    company_name = company.get("name", "Support") if company else "Support"

    # Extract messages and handoff briefing
    messages = ticket.get("messages", [])
    handoff_ctx = ticket.get("handoff_context") or {}
    if not handoff_ctx and attributed_chat:
        handoff_ctx = attributed_chat.get("handoff_context") or {}

    customer_email = ticket.get("customer_email", "")
    customer_name = ticket.get("customer_name") or ""
    subject = ticket.get("subject", "")

    # Retrieve relevant knowledge base context
    search_query = _extract_last_customer_query(
        messages,
        default_query=handoff_ctx.get("conversation_summary") or subject or "help",
    )

    kb_snippets: List[str] = []
    sources_used: List[str] = []

    try:
        kb_result = await knowledge_service.search_knowledge(
            user_id=None,
            query=search_query,
            limit=3,
            threshold=0.45,
            company_id=company_id,
            record_gap=False,
        )
        for doc in kb_result.get("results", []):
            title = doc.get("title") or "Company Documentation"
            content = doc.get("page_content") or ""
            if title not in sources_used:
                sources_used.append(title)
            kb_snippets.append(f"Document: {title}\nContent: {content[:400]}")
    except Exception as e:
        logger.warning("Knowledge search failed in copilot for ticket %s: %s", ticket_id, e)

    # Build prompt for LLM
    provider = company.get("ai_provider") if company else "anthropic"
    if not provider:
        provider = "anthropic"

    transcript_items = []
    for m in messages[-6:]:
        dir_label = "Customer" if m.get("direction") == "inbound" else "Support Agent"
        body = m.get("body_text", "")
        transcript_items.append(f"{dir_label}: {body}")
    transcript_text = "\n\n".join(transcript_items) if transcript_items else f"Customer query: {subject}"

    user_prompt = (
        f"Company Name: {company_name}\n"
        f"Customer Email: {customer_email}\n"
        f"Customer Name: {customer_name}\n"
        f"Ticket Subject: {subject}\n"
        f"Customer Sentiment: {handoff_ctx.get('customer_sentiment', 'neutral')}\n"
        f"Classified Intent: {handoff_ctx.get('intent', 'general_support')}\n"
        f"Conversation Summary: {handoff_ctx.get('conversation_summary', 'Customer inquired about support.')}\n"
        f"Failure Points: {', '.join(handoff_ctx.get('ai_failure_points', [])) or 'None'}\n\n"
        f"=== RELEVANT KNOWLEDGE BASE DOCS ===\n"
        f"{chr(10).join(kb_snippets) if kb_snippets else 'No matching knowledge base documents found.'}\n\n"
        f"=== RECENT TICKET MESSAGES ===\n"
        f"{transcript_text}\n"
    )

    try:
        llm = get_llm(provider, fast_routing=False, streaming=False)
        sys_msg = COPILOT_SYSTEM_PROMPT.format(
            tone=tone,
            instruction=instruction or "None provided",
        )
        ai_resp = await llm.ainvoke([
            SystemMessage(content=sys_msg),
            HumanMessage(content=user_prompt),
        ])
        content_text = ai_resp.content.strip()

        # Strip markdown formatting if present
        if content_text.startswith("```"):
            content_text = re.sub(r"^```(?:json)?\n?", "", content_text)
            content_text = re.sub(r"\n?```$", "", content_text).strip()

        parsed = json.loads(content_text)
        suggested_reply = parsed.get("suggested_reply", "")
        if not suggested_reply:
            raise ValueError("LLM returned empty suggested reply")

        return {
            "suggested_reply": suggested_reply,
            "suggested_subject": parsed.get("suggested_subject") or f"Re: {subject}",
            "customer_sentiment": parsed.get("customer_sentiment") or handoff_ctx.get("customer_sentiment", "neutral"),
            "sources_used": sources_used,
            "suggested_actions": parsed.get("suggested_actions", []),
            "reasoning": parsed.get("reasoning"),
        }
    except Exception as e:
        logger.info("Falling back to deterministic copilot generator: %s", e)
        return _build_rule_based_fallback(
            ticket=ticket,
            handoff_ctx=handoff_ctx,
            tone=tone,
            sources=sources_used,
            instruction=instruction,
        )


async def execute_copilot_action(
    company_id: str,
    ticket_id: str,
    action_type: str,
    parameters: Dict[str, Any],
    actor: str,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute a single-click next-best-action proposed by the Copilot."""
    if action_type == "status_change":
        target_status = parameters.get("status")
        if not target_status:
            raise ValueError("Target 'status' is required for status_change action")
        updated = await ticket_service.transition_ticket(
            company_id=company_id,
            ticket_id=ticket_id,
            to_status=target_status,
            actor=actor,
            note=reason,
        )
        return {"success": True, "action_type": action_type, "ticket": updated}

    elif action_type == "priority_change":
        target_priority = parameters.get("priority")
        if not target_priority:
            raise ValueError("Target 'priority' is required for priority_change action")
        updated = await ticket_service.set_priority(
            company_id=company_id,
            ticket_id=ticket_id,
            priority=target_priority,
            actor=actor,
        )
        return {"success": True, "action_type": action_type, "ticket": updated}

    elif action_type == "internal_note":
        note_content = parameters.get("note") or reason
        if not note_content:
            raise ValueError("Note content is required for internal_note action")
        updated = await ticket_service.add_internal_note(
            company_id=company_id,
            ticket_id=ticket_id,
            note=note_content,
            actor=actor,
        )
        return {"success": True, "action_type": action_type, "ticket": updated}

    else:
        raise ValueError(f"Unsupported action type: {action_type}")
