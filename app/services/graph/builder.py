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
        return "tools"
    return END

async def human_handoff_node(state: AgentState, config):
    from app.services import company_email_service
    from langchain_core.messages import AIMessage
    import logging
    
    logger = logging.getLogger(__name__)
    company_id = state.get("company_id")
    customer_email = state.get("sdk_user_email")
    session_id = state.get("session_id")
    
    # Try to extract email from the last user message if not already known
    if not customer_email and state.get("messages"):
        last_msg = state["messages"][-1]
        import re
        if getattr(last_msg, "type", None) == "human":
            match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', last_msg.content)
            if match:
                customer_email = match.group(0)
    
    if not customer_email:
        return {"messages": [AIMessage(content="I will escalate your request to our human support team. Please provide your email address below so we can create a ticket and get back to you shortly.")]}
        
    try:
        # Load chat history for summary
        from app.core.database import db
        conversation = await db.widget_conversations.find_one({"company_id": company_id, "session_id": session_id})
        subject = conversation.get("subject", "Support Request via Escalation") if conversation else "Support Request"
        chat_summary = "User escalated chat."
        if conversation and "messages" in conversation:
            history_texts = [f"{m.get('role', 'user')}: {m.get('content', '')}" for m in conversation.get("messages", [])]
            chat_summary = "\n\n".join(history_texts)
            
        ticket = await company_email_service.create_ticket(
            company_id=company_id,
            customer_email=customer_email,
            subject=subject,
            chat_summary=chat_summary,
            chat_session_id=session_id,
            customer_name=None,
        )
        msg = f"Thank you! Your chat has been escalated to our human support team as Ticket #{ticket['id']}. We will reach out to you at {customer_email} shortly."
    except Exception as e:
        logger.exception("Failed to create support ticket in graph")
        msg = "We are currently unable to create a ticket automatically. Please email our support team directly."
        
    return {"messages": [AIMessage(content=msg)], "escalate_to_human": True}


def build_graph():
    builder = StateGraph(AgentState)
    
    # Core nodes
    builder.add_node("orchestrator", orchestrator_node)
    builder.add_node("human_handoff", human_handoff_node)
    
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
    builder.add_conditional_edges("knowledge_agent", should_continue, {"tools": "knowledge_tools", END: END})
    builder.add_edge("knowledge_tools", "knowledge_agent")
    
    builder.add_conditional_edges("navigation_agent", should_continue, {"tools": "navigation_tools", END: END})
    builder.add_edge("navigation_tools", "navigation_agent")
    
    builder.add_conditional_edges("api_agent", should_continue, {"tools": "api_tools", END: END})
    builder.add_edge("api_tools", "api_agent")
    
    builder.add_conditional_edges("scraper_agent", should_continue, {"tools": "scraper_tools", END: END})
    builder.add_edge("scraper_tools", "scraper_agent")
    
    return builder.compile()

# Provide a compiled graph singleton for the application to import
compiled_graph = build_graph()
