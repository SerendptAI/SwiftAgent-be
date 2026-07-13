from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import get_dashboard_navigation, get_full_dashboard_documentation, render_navigation_guide
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe

NAVIGATION_PROMPT = """You are the Navigation & UI Guide Expert.
Your job is to guide users through the dashboard UI.
CRITICAL: You MUST use the `get_dashboard_navigation` tool to fetch the navigation data BEFORE you attempt to answer the user's question. NEVER recite navigation steps from memory or prior context.
Once you know the steps the user needs to take, you MUST call the `render_navigation_guide` tool to send the interactive visual steps to their screen. DO NOT output the steps as JSON in your text response.
If the user specifically asks for the FULL documentation, use `get_full_dashboard_documentation` and then pass the results to `render_navigation_guide`.
Do not guess where things are. Always rely on the tool output."""

@observe(name="navigation_agent_node")
async def navigation_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([get_dashboard_navigation, get_full_dashboard_documentation, render_navigation_guide])
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{NAVIGATION_PROMPT}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
