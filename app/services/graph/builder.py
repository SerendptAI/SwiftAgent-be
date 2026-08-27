from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

from app.services.graph.state import AgentState
from app.services.graph.orchestrator import orchestrator_node
from app.services.graph.agents.knowledge_agent import knowledge_agent_node
from app.services.graph.agents.navigation_agent import navigation_agent_node
from app.services.graph.agents.api_agent import api_agent_node
from app.services.graph.agents.scraper_agent import scraper_agent_node

from app.services.graph.tools import (
    search_knowledge_base, scrape_documentation_link,
    get_dashboard_navigation, get_full_dashboard_documentation,
    get_api_documentation, query_company_api,
    read_website_page, render_navigation_guide
)


def route_from_orchestrator(state: AgentState):
    intent = state.get("intent")
    if intent == "knowledge":
        return "knowledge_agent"
    elif intent == "navigation":
        return "navigation_agent"
    elif intent == "api":
        return "api_agent"
    elif intent == "scraper":
        return "scraper_agent"
    elif intent == "human_escalation":
        return "human_handoff"
    
    return END

def should_continue(state: AgentState):
    """Return 'tools' if the agent requested a tool call, else END."""
    messages = state["messages"]
    last_message = messages[-1]
    if getattr(last_message, "tool_calls", None):
        tool_name = last_message.tool_calls[0]["name"]
        if tool_name.startswith("transfer_to_") or tool_name == "escalate_to_human":
            return "handoff"
        return "tools"
    return END

def handle_handoff_node(state: AgentState):
    from langchain_core.messages import ToolMessage
    messages = state["messages"]
    last_message = messages[-1]
    tool_call = last_message.tool_calls[0]
    tool_name = tool_call["name"]
    tool_call_id = tool_call["id"]
    
    intent = "general_chat"
    escalate = False
    
    if tool_name == "transfer_to_knowledge":
        intent = "knowledge"
    elif tool_name == "transfer_to_navigation":
        intent = "navigation"
    elif tool_name == "transfer_to_api":
        intent = "api"
    elif tool_name == "transfer_to_scraper":
        intent = "scraper"
    elif tool_name == "escalate_to_human":
        intent = "human_escalation"
        escalate = True
        
    tool_msg = ToolMessage(content=f"Transferred to {intent} expert successfully.", tool_call_id=tool_call_id)
    return {"intent": intent, "escalate_to_human": escalate, "messages": [tool_msg]}

def route_from_handoff(state: AgentState):
    intent = state.get("intent")
    if intent == "knowledge":
        return "knowledge_agent"
    elif intent == "navigation":
        return "navigation_agent"
    elif intent == "api":
        return "api_agent"
    elif intent == "scraper":
        return "scraper_agent"
    elif intent == "human_escalation":
        return "human_handoff"
    
    return "orchestrator"

async def human_handoff_node(state: AgentState, config):
    from app.services import company_email_service
    from langchain_core.messages import AIMessage
    import logging
    import re
    
    logger = logging.getLogger(__name__)
    company_id = state.get("company_id")
    customer_email = state.get("sdk_user_email")
    session_id = state.get("session_id")
    
    # Try to extract email from the last user message if not already known
    if not customer_email and state.get("messages"):
        last_msg = state["messages"][-1]
        if getattr(last_msg, "type", None) == "human":
            match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', last_msg.content)
            if match:
                customer_email = match.group(0)
    
    if not customer_email:
        return {"messages": [AIMessage(content="I will escalate your request to our human support team. Please provide your email address below so we can create a ticket and get back to you shortly.")]}
    
    try:
        # Load conversation for enhanced handoff
        from app.core.database import db
        from app.services.handoff_service import create_enhanced_handoff
        
        conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": session_id})
        subject = conversation.get("subject", "Support Request via Escalation") if conversation else "Support Request"
        messages = conversation.get("messages", []) if conversation else []
        
        # Build rich handoff context
        handoff_data = await create_enhanced_handoff(
            session_id=session_id,
            company_id=company_id,
            messages=messages,
            escalation_reason="human_request",
            customer_email=customer_email,
            intent=state.get("intent") or "general_chat",
            page_url=state.get("page_url"),
        )
        
        # Create ticket with handoff context
        chat_summary = handoff_data["handoff_context"]["conversation_summary"]
        
        ticket = await company_email_service.create_ticket(
            company_id=company_id,
            customer_email=customer_email,
            subject=subject,
            chat_summary=chat_summary,
            chat_session_id=session_id,
            customer_name=None,
            handoff_context=handoff_data["handoff_context"],
        )
        
        await db.widget_conversations.update_one(
            {"company_id": company_id, "session_id": session_id},
            {"$set": {"escalated": True, "ticket_id": ticket["id"]}}
        )
        
        # Build customer-facing message with agent name and wait time
        wait_time = handoff_data["wait_time_estimate"]
        available_agents = handoff_data["available_agents"]
        
        if available_agents and wait_time["has_agents_available"]:
            agent_name = available_agents[0]["name"]
            wait_minutes = wait_time["estimated_wait_minutes"]
            if wait_minutes <= 5:
                msg = f"I'm connecting you to {agent_name} from our support team. They'll be with you shortly (estimated wait: under 5 minutes). Your ticket is #{ticket['id']}."
            else:
                msg = f"I'm connecting you to {agent_name} from our support team. Estimated wait time: {wait_minutes} minutes. Your ticket is #{ticket['id']}."
        else:
            msg = f"Thank you! Your chat has been escalated to our human support team as Ticket #{ticket['id']}. We will reach out to you at {customer_email} shortly."
        
        # Store handoff context in state for the SSE generator
        return {
            "messages": [AIMessage(content=msg)],
            "escalate_to_human": True,
            "handoff_context": handoff_data,
            "handoff_initiated": True,
        }
    except Exception as e:
        logger.exception("Failed to create support ticket in graph")
        msg = "We are currently unable to create a ticket automatically. Please email our support team directly."
        
    return {"messages": [AIMessage(content=msg)], "escalate_to_human": True}


def build_graph():
    builder = StateGraph(AgentState)
    
    # Core nodes
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("human_handoff", human_handoff_node)
    builder.add_node("handle_handoff", handle_handoff_node)
    
    # Worker agents
    builder.add_node("knowledge_agent", knowledge_agent_node)
    builder.add_node("navigation_agent", navigation_agent_node)
    builder.add_node("api_agent", api_agent_node)
    builder.add_node("scraper_agent", scraper_agent_node)
    
    # Tool nodes tailored for each agent (solves routing back ambiguity)
    builder.add_node("knowledge_tools", ToolNode([search_knowledge_base, scrape_documentation_link]))
    builder.add_node("navigation_tools", ToolNode([get_dashboard_navigation, get_full_dashboard_documentation, render_navigation_guide]))
    builder.add_node("api_tools", ToolNode([get_api_documentation, query_company_api]))
    builder.add_node("scraper_tools", ToolNode([read_website_page]))
    
    # Routing from Orchestrator
    builder.add_edge(START, "orchestrator")
    builder.add_conditional_edges("orchestrator", route_from_orchestrator)
    
    # Routing for human handoff
    builder.add_edge("human_handoff", END)
    
    # Routing for agents to their specific tools and back
    builder.add_conditional_edges("knowledge_agent", should_continue, {"tools": "knowledge_tools", "handoff": "handle_handoff", END: END})
    builder.add_edge("knowledge_tools", "knowledge_agent")
    
    builder.add_conditional_edges("navigation_agent", should_continue, {"tools": "navigation_tools", "handoff": "handle_handoff", END: END})
    builder.add_edge("navigation_tools", "navigation_agent")
    
    builder.add_conditional_edges("api_agent", should_continue, {"tools": "api_tools", "handoff": "handle_handoff", END: END})
    builder.add_edge("api_tools", "api_agent")
    
    builder.add_conditional_edges("scraper_agent", should_continue, {"tools": "scraper_tools", "handoff": "handle_handoff", END: END})
    builder.add_edge("scraper_tools", "scraper_agent")

    builder.add_conditional_edges("handle_handoff", route_from_handoff)
    
    return builder.compile()

# Provide a compiled graph singleton for the application to import
compiled_graph = build_graph()
