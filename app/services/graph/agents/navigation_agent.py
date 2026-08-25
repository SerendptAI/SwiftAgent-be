from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import get_dashboard_navigation, get_full_dashboard_documentation, render_navigation_guide
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import NAVIGATION_HANDOFF_TOOLS

NAVIGATION_PROMPT = """You are the Navigation & UI Guide Expert.
Use `get_dashboard_navigation` to fetch navigation data BEFORE answering.
Then call `render_navigation_guide` to send interactive visual steps to the screen.
After `render_navigation_guide`: DO NOT output ANY text at all. No text before tool calls."""

@observe(name="navigation_agent_node")
async def navigation_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"], streaming=True, force_anthropic_native=True)
    llm_with_tools = llm.bind_tools([get_dashboard_navigation, get_full_dashboard_documentation, render_navigation_guide] + NAVIGATION_HANDOFF_TOOLS)
    
    from app.services.prompt_service import render_prompt_for_company
    rendered = await render_prompt_for_company("navigation_agent", state.get("company_data", {}), state.get("company_id"))
    prompt_text = rendered.rendered_text if rendered.rendered_text else NAVIGATION_PROMPT
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{prompt_text}"
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
