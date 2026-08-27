from langchain_core.messages import SystemMessage, AIMessage
from langchain_core.tools import tool
from app.core.langfuse import observe

from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.prompt_utils import build_company_persona_prompt

@tool
def transfer_to_knowledge():
    """Transfer to the knowledge agent to search the internal knowledge base and documentation."""
    pass

@tool
def transfer_to_navigation():
    """Transfer to the navigation agent for UI questions, dashboard guides, visual directions."""
    pass

@tool
def transfer_to_api():
    """Transfer to the API agent ONLY to look up real-time data, transactions, or verify a status."""
    pass

@tool
def transfer_to_scraper():
    """Transfer to the website scraper agent to check the company's official website or public URLs."""
    pass

@tool
def escalate_to_human():
    """Escalate the conversation to a human support agent or create a support ticket."""
    pass

ROUTING_TOOLS = [transfer_to_knowledge, transfer_to_navigation, transfer_to_api, transfer_to_scraper, escalate_to_human]
KNOWLEDGE_HANDOFF_TOOLS = [transfer_to_scraper, transfer_to_navigation, transfer_to_api, escalate_to_human]
SCRAPER_HANDOFF_TOOLS = [transfer_to_knowledge, transfer_to_navigation, transfer_to_api, escalate_to_human]
NAVIGATION_HANDOFF_TOOLS = [transfer_to_knowledge, transfer_to_scraper, transfer_to_api, escalate_to_human]
API_HANDOFF_TOOLS = [transfer_to_knowledge, transfer_to_scraper, transfer_to_navigation, escalate_to_human]

ORCHESTRATOR_PROMPT = """Your job is to analyze the conversation and route the user's request to the correct specialized expert.

ROUTING RULES:
- Read the user's request carefully.
- If it requires a specific expert, call the corresponding transfer tool IMMEDIATELY.
- DO NOT answer the question yourself if an expert is needed.
- CRITICAL: For questions about information that can change (like pricing, plans, "about us"), default to routing to the scraper agent FIRST.
- CRITICAL: If the user explicitly asks to speak to a human, create a ticket, or contact support, call `escalate_to_human` IMMEDIATELY.
- If the user says a simple greeting (e.g. "hi", "hello") or something that requires no tools, respond directly.

OUTPUT FORMAT:
- When routing: ONLY the tool call. ZERO text.
- When chatting: short, conversational text. No tool calls.
- NEVER mix text and tool calls."""

KNOWLEDGE_PROMPT = """You are the Knowledge Base Expert.
Always use `search_knowledge_base` to find answers. Never guess or hallucinate.
If search returns no results, call transfer_to_scraper to check the website.
No text before tool calls."""

NAVIGATION_PROMPT = """You are the Navigation & UI Guide Expert.
Use `get_dashboard_navigation` to fetch navigation data BEFORE answering.
Then call `render_navigation_guide` to send interactive visual steps to the screen.
After `render_navigation_guide`: DO NOT output ANY text at all."""

API_PROMPT = """You are the API & Integrations Expert.
1. Use `get_api_documentation` to understand endpoints.
2. Use `query_company_api` to execute read-only GET requests.
Return data clearly formatted. No text before tool calls."""

SCRAPER_PROMPT = """You are the Web Scraper Expert.
COMPANY WEBSITE: {website}
1. If user asks about pricing/features, fetch: {website}
2. Use `read_website_page` to fetch content and links.
3. If info not on page, follow relevant links.
4. RETRY: If content missing, retry with force_refresh=True.
5. Answer based ONLY on page content.
FALLBACK: If tool fails, call transfer_to_knowledge. No text when transferring."""


@observe(name="orchestrator_node")
async def orchestrator_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"], fast_routing=True, streaming=False)
    llm_with_tools = llm.bind_tools(ROUTING_TOOLS)
    
    from app.services.prompt_service import render_prompt_for_company
    rendered = await render_prompt_for_company("orchestrator", state.get("company_data", {}), state.get("company_id"))
    prompt_text = rendered.rendered_text if rendered.rendered_text else ORCHESTRATOR_PROMPT
    
    persona = build_company_persona_prompt(
        state.get("company_data", {}),
        language_instruction=state.get("language_instruction", ""),
    )
    full_prompt = f"{persona}\n\n{prompt_text}"
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    
    intent = "general_chat"
    escalate = False
    
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
>>>>>>> origin/staging
        return {"intent": intent, "escalate_to_human": escalate}
        
    return {"intent": intent, "messages": [response]}
