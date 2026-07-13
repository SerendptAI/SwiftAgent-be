import logging
import json
from uuid import uuid4
from datetime import datetime, timezone
import zoneinfo
from langchain_core.messages import HumanMessage, AIMessage

from app.core.database import db
from app.services.graph.builder import compiled_graph

logger = logging.getLogger(__name__)


async def chat_stream_graph(
    company_id: str,
    session_id: str,
    message: str,
    user_id: str | None = None,
    page_url: str | None = None,
    attachments: list[dict] | None = None,
    user_timestamp: str | None = None,
    agent_provider: str = "anthropic",
    sdk_user_email: str | None = None,
    user_timezone: str | None = None,
):
    try:
        # Load conversation
        convo = await db.widget_conversations.find_one({
            "company_id": company_id,
            "session_id": session_id,
        })
        
        # Load company data
        company = await db.companies.find_one({"id": company_id}) or {}
        
        # Determine local time based on user or company timezone
        tz_str = user_timezone or company.get("timezone") or "UTC"
        try:
            tz = zoneinfo.ZoneInfo(tz_str)
        except Exception:
            tz = timezone.utc
            
        now = datetime.now(tz)
        current_date = now.strftime("%A, %B %d, %Y")
        current_time_str = now.strftime("%I:%M %p %Z")

        company_data = {
            "name": company.get("name", "Unknown Company"),
            "description": company.get("description", ""),
            "industry": company.get("industry", ""),
            "brand_tone": company.get("brand_tone", "professional"),
            "voice_style": company.get("voice_style", "professional"),
            "primary_language": company.get("primary_language", "English"),
            "answer_boundaries": company.get("answer_boundaries", []),
            "current_date": current_date,
            "current_time": current_time_str
        }
        
        # We need to construct LangChain messages from DB
        langchain_messages = []
        if convo and "messages" in convo:
            for m in convo["messages"]:
                if m.get("role") == "user":
                    langchain_messages.append(HumanMessage(content=m.get("content", "")))
                elif m.get("role") == "assistant":
                    langchain_messages.append(AIMessage(content=m.get("content", "")))
                    
        # Append current user message
        langchain_messages.append(HumanMessage(content=message))
        
        # Insert the message into the DB
        now = datetime.now(tz=timezone.utc).isoformat()
        user_msg_doc = {
            "id": str(uuid4()),
            "role": "user",
            "content": message,
            "timestamp": user_timestamp or now
        }
        await db.widget_conversations.update_one(
            {"company_id": company_id, "session_id": session_id},
            {"$push": {"messages": user_msg_doc}},
            upsert=True
        )

        state = {
            "messages": langchain_messages,
            "session_id": session_id,
            "company_id": company_id,
            "company_data": company_data,
            "user_id": user_id,
            "sdk_user_email": sdk_user_email,
            "page_url": page_url,
            "attachments": attachments or [],
            "agent_provider": agent_provider,
            "intent": None,
            "escalate_to_human": False
        }
        
        config = {"configurable": {"state": state}}
        
        final_text = ""
        
        async for event in compiled_graph.astream_events(state, config, version="v2"):
            kind = event["event"]
            
            if kind == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                if chunk.content:
                    text_content = ""
                    if isinstance(chunk.content, str):
                        text_content = chunk.content
                    elif isinstance(chunk.content, list):
                        text_parts = [c.get("text", "") for c in chunk.content if isinstance(c, dict) and c.get("type") == "text"]
                        text_content = "".join(text_parts)
                        
                    if text_content:
                        final_text += text_content
                        yield {"type": "text", "content": text_content}
                    
            elif kind == "on_tool_start":
                name = event["name"]
                if name.startswith("transfer_"):
                    yield {"type": "thinking", "message": f"Routing to {name.replace('transfer_to_', '').replace('_', ' ')} expert..."}
                else:
                    yield {"type": "tool", "name": name, "label": name.replace('_', ' ').title() + "..."}
                    
            elif kind == "on_tool_end":
                name = event["name"]
                result = event["data"].get("output", {})
                
                try:
                    if hasattr(result, "content"):
                        result = json.loads(result.content)
                    elif isinstance(result, str):
                        result = json.loads(result)
                except Exception:
                    pass
                    
                if isinstance(result, dict):
                    if name == "search_knowledge_base" and "results" in result:
                        yield {"type": "sources", "sources": result["results"]}
                    elif name in ["get_dashboard_navigation", "get_full_dashboard_documentation"]:
                        guide = {}
                        if "navigation_report" in result:
                            guide["steps"] = result.get("navigation_report")
                        if "navigation_steps" in result:
                            guide["steps"] = result.get("navigation_steps")
                        if guide:
                            yield {"type": "navigation_guide", "guide": guide}

            elif kind == "on_chain_end":
                name = event["name"]
                if name in ["human_handoff", "orchestrator"]:
                    output = event["data"].get("output", {})
                    if isinstance(output, dict) and "messages" in output:
                        msgs = output["messages"]
                        if msgs and not final_text:
                            content = msgs[-1].content
                            text_content = ""
                            if isinstance(content, str):
                                text_content = content
                            elif isinstance(content, list):
                                text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                                text_content = "".join(text_parts)
                                
                            if text_content:
                                final_text += text_content
                                yield {"type": "text", "content": text_content}

        # Store assistant message in DB
        if final_text:
            assistant_msg_doc = {
                "id": str(uuid4()),
                "role": "assistant",
                "content": final_text,
                "timestamp": datetime.now(tz=timezone.utc).isoformat()
            }
            await db.widget_conversations.update_one(
                {"company_id": company_id, "session_id": session_id},
                {"$push": {"messages": assistant_msg_doc}}
            )

    except Exception as e:
        logger.exception("Graph execution failed")
        yield {"type": "error", "message": str(e)}

