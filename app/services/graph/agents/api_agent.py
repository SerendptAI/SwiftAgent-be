from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import get_api_documentation, query_company_api
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import API_HANDOFF_TOOLS

API_PROMPT = """You are the API & Integrations Expert.
1. Use `get_api_documentation` to understand endpoints.
2. Use `query_company_api` to execute read-only GET requests.
Return data clearly formatted. No text before tool calls."""

@observe(name="api_agent_node")
async def api_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"], streaming=True, force_anthropic_native=True)
    llm_with_tools = llm.bind_tools([get_api_documentation, query_company_api] + API_HANDOFF_TOOLS)
    
    from app.services.prompt_service import render_prompt_for_company
    rendered = await render_prompt_for_company("api_agent", state.get("company_data", {}), state.get("company_id"))
    prompt_text = rendered.rendered_text if rendered.rendered_text else API_PROMPT
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{prompt_text}"
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
