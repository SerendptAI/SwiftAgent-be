from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.tools import tool
from app.core.langfuse import observe

from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.prompt_utils import build_company_persona_prompt

@tool
def transfer_to_knowledge():
    """Transfer to the knowledge agent to search the internal knowledge base and documentation. Use this whenever information is NOT found on the website, or for general policy/documentation questions."""
    pass

@tool
def transfer_to_navigation():
    """Transfer to the navigation agent for UI questions, dashboard guides, visual directions, and "how to" questions (e.g. "how do I generate an API key", "where do I find X"). Use this for ALL questions about navigating the dashboard or performing actions in the UI. DO NOT use this if the user is asking to create a support ticket or speak to a human."""
    pass

@tool
def transfer_to_api():
    """Transfer to the API agent ONLY to look up real-time data, transactions, or verify a status (e.g. "what is the status of my order"). Do NOT use this if the user is asking how to create/generate an API key in the UI."""
    pass

@tool
def transfer_to_scraper():
    """Transfer to the website scraper agent to check the company's official website or public URLs. MUST use this whenever information (such as use cases, features, pricing, plans, eligibility, or general company offerings) is NOT found in the knowledge base, or when the user asks about pricing, plans, or website content."""
    pass

@tool
def escalate_to_human():
    """Escalate the conversation to a human support agent or create a support ticket. Use this IMMEDIATELY if the user explicitly asks for a human, wants to contact support, or asks to create a ticket."""
    pass

ROUTING_TOOLS = [
    transfer_to_knowledge,
    transfer_to_navigation,
    transfer_to_api,
    transfer_to_scraper,
    escalate_to_human,
]

KNOWLEDGE_HANDOFF_TOOLS = [
    transfer_to_scraper,
    transfer_to_navigation,
    transfer_to_api,
    escalate_to_human,
]

SCRAPER_HANDOFF_TOOLS = [
    transfer_to_knowledge,
    transfer_to_navigation,
    transfer_to_api,
    escalate_to_human,
]

NAVIGATION_HANDOFF_TOOLS = [
    transfer_to_knowledge,
    transfer_to_scraper,
    transfer_to_api,
    escalate_to_human,
]

API_HANDOFF_TOOLS = [
    transfer_to_knowledge,
    transfer_to_scraper,
    transfer_to_navigation,
    escalate_to_human,
]


ORCHESTRATOR_PROMPT = """Your job is to analyze the conversation and route the user's request to the correct specialized expert.

ROUTING RULES:
- Read the user's request carefully.
- If it requires a specific expert, call the corresponding transfer tool IMMEDIATELY.
- DO NOT answer the question yourself if an expert is needed.
- CRITICAL: For questions about pricing or plans, default to routing to the scraper agent FIRST to check the website, since plans are updated frequently there.
- CRITICAL: If the user explicitly asks to speak to a human, create a ticket, or contact support, you MUST call the `escalate_to_human` tool IMMEDIATELY. Do NOT pretend to be a human, do NOT ask for their email address, and do NOT try to handle the ticket creation yourself.
- CRITICAL: If the user provides an email address (e.g. name@example.com), and the previous message asked them for their email to create a ticket, you MUST call the `escalate_to_human` tool IMMEDIATELY to complete the escalation.
- If the user says a simple greeting (e.g. "hi", "hello") or something that requires no tools, respond directly and conversationally.
- If the user's request is ambiguous, lacks necessary context, or you are confused about which expert to route to, DO NOT GUESS. Instead, respond directly by asking the user a clarifying question to better understand their needs.

OUTPUT FORMAT — ABSOLUTE RULE (VIOLATIONS WILL BREAK THE CUSTOMER EXPERIENCE):
- When routing: Your response MUST contain ONLY the tool call. Absolutely ZERO text, ZERO sentences, ZERO explanations. No "I've transferred you", no "Let me connect you", no "I'll route you" — NOTHING. Any text you produce is shown directly to the customer and will confuse them.
- When chatting (greetings/clarifications only): Respond with short, conversational text. No tool calls.
- NEVER mix text and tool calls in the same response.
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
