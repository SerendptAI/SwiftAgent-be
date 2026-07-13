from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.tools import tool
from app.core.langfuse import observe

from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm

@tool
def transfer_to_knowledge():
    """Transfer to the knowledge agent for questions about policies, pricing, features, and general information."""
    pass

@tool
def transfer_to_navigation():
    """Transfer to the navigation agent for UI questions, "how to", screenshots, visual directions, and dashboard guides. THIS IS CRITICAL FOR ALL "WHERE IS X" QUESTIONS."""
    pass

@tool
def transfer_to_api():
    """Transfer to the API agent to look up real-time data, transactions, or verify a status."""
    pass

@tool
def transfer_to_scraper():
    """Transfer to the scraper agent to read and extract text from a specific public website link."""
    pass

@tool
def escalate_to_human():
    """Escalate the conversation to a human support agent. Use this if the user explicitly asks for a human."""
    pass

ROUTING_TOOLS = [
    transfer_to_knowledge,
    transfer_to_navigation,
    transfer_to_api,
    transfer_to_scraper,
    escalate_to_human,
]

ORCHESTRATOR_PROMPT = """You are the central Orchestrator for an AI customer support platform.
Your job is to analyze the conversation and route the user's request to the correct specialized expert.

ROUTING RULES:
- Read the user's request carefully.
- If it requires a specific expert, call the corresponding transfer tool IMMEDIATELY.
- DO NOT answer the question yourself if an expert is needed.
- If the user says a simple greeting (e.g. "hi", "hello") or something that requires no tools, respond directly and conversationally.
"""

@observe(as_type="generation")
async def orchestrator_node(state: AgentState, config):
    # Use fast routing model for the orchestrator
    llm = get_llm(state["agent_provider"], fast_routing=True, streaming=True)
    llm_with_tools = llm.bind_tools(ROUTING_TOOLS)
    
    messages = [SystemMessage(content=ORCHESTRATOR_PROMPT)] + state["messages"]
    
    response = await llm_with_tools.ainvoke(messages, config)
    
    intent = "general_chat"
    escalate = False
    
    # Check if a routing tool was called
    if getattr(response, "tool_calls", None):
        tool_call = response.tool_calls[0]
        tool_name = tool_call["name"]
        
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
            
        # We don't append the AIMessage to state if it's just a handoff, 
        # so the worker agent gets the original user message as the last message!
        return {"intent": intent, "escalate_to_human": escalate}
        
    # If no tool was called, the orchestrator is just chatting
    return {"intent": intent, "messages": [response]}
