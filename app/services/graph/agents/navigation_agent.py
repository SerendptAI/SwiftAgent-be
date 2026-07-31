from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import get_dashboard_navigation, get_full_dashboard_documentation, render_navigation_guide
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import NAVIGATION_HANDOFF_TOOLS

NAVIGATION_PROMPT = """You are the Navigation & UI Guide Expert.
Your job is to guide users through the dashboard UI.
CRITICAL: You MUST use the `get_dashboard_navigation` tool to fetch the navigation data BEFORE you attempt to answer the user's question. NEVER recite navigation steps from memory or prior context.
Once you know the steps the user needs to take, you MUST call the `render_navigation_guide` tool to send the interactive visual steps to their screen.

CRITICAL RULES FOR TOOL CALLING:
1. You MUST NOT output ANY text alongside your tool call. ONLY return the tool call itself. Do not say "Based on the navigation data..." or "Here are the steps...". Just call the tool.
2. DO NOT output the steps as JSON in your text response.
3. AFTER `render_navigation_guide` completes successfully: DO NOT output ANY text at all. No "I have displayed the guide", no "Here you go", no confirmation sentences. The user already sees the visual guide rendered on their screen instantly — any text from you is redundant and confusing. Simply end your turn with no output.
4. ONLY respond with text after the tool completes if the tool returned an error or the navigation data was not found.
If the user specifically asks for the FULL documentation, use `get_full_dashboard_documentation` and then pass the results to `render_navigation_guide`.
Do not guess where things are. Always rely on the tool output. If the requested UI feature is not found, use the available transfer tools to hand off the task to another appropriate agent instead of concluding.
CRITICAL RULE: When you need to call a tool (including handoff/transfer tools), you MUST NOT output ANY conversational text or "thinking" before the tool call! ONLY return the tool call itself."""

@observe(name="navigation_agent_node")
async def navigation_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([get_dashboard_navigation, get_full_dashboard_documentation, render_navigation_guide] + NAVIGATION_HANDOFF_TOOLS)
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{NAVIGATION_PROMPT}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
