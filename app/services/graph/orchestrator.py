from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.tools import tool
from app.core.langfuse import observe

from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.prompt_utils import build_company_persona_prompt

@tool
def transfer_to_knowledge():
    """Transfer to the knowledge agent for questions about policies, pricing, features, and general information."""
    pass

@tool
def transfer_to_navigation():
    """Transfer to the navigation agent for UI questions, dashboard guides, visual directions, and "how to" questions (e.g. "how do I generate an API key", "where do I find X"). Use this for ALL questions about navigating the dashboard or performing actions in the UI."""
    pass

@tool
def transfer_to_api():
    """Transfer to the API agent ONLY to look up real-time data, transactions, or verify a status (e.g. "what is the status of my order"). Do NOT use this if the user is asking how to create/generate an API key in the UI."""
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

ORCHESTRATOR_PROMPT = """Your job is to analyze the conversation and route the user's request to the correct specialized expert.

ROUTING RULES:
- Read the user's request carefully.
- If it requires a specific expert, call the corresponding transfer tool IMMEDIATELY.
- DO NOT answer the question yourself if an expert is needed.
- If the user says a simple greeting (e.g. "hi", "hello") or something that requires no tools, respond directly and conversationally.
- CRITICAL: If the user's request is ambiguous, lacks necessary context, or you are confused about which expert to route to, DO NOT GUESS. Instead, respond directly by asking the user a clarifying question to better understand their needs.
"""

@observe(name="orchestrator_node")
async def orchestrator_node(state: AgentState, config):
    # Use fast routing model for the orchestrator. Streaming is disabled to prevent
    # pre-tool conversational filler from leaking to the frontend before a handoff.
    llm = get_llm(state["agent_provider"], fast_routing=True, streaming=False)
    llm_with_tools = llm.bind_tools(ROUTING_TOOLS)
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\\n\\n{ORCHESTRATOR_PROMPT}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    
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
