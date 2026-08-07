from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import get_api_documentation, query_company_api
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import API_HANDOFF_TOOLS

API_PROMPT = """You are the API & Integrations Expert.
Your job is to query the company's external API integrations to look up real-time data for the user.
1. Use `get_api_documentation` to understand available endpoints.
2. Use `query_company_api` to execute read-only GET requests based on those endpoints.
Return the data clearly formatted to the user.
If you cannot find the requested data, do not guess. Instead, use the available transfer tools to hand off the task to another appropriate agent.
CRITICAL RULE: When you need to call a tool (including handoff/transfer tools), you MUST NOT output ANY conversational text or "thinking" before the tool call! ONLY return the tool call itself."""

@observe(name="api_agent_node")
async def api_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"], streaming=False)
    llm_with_tools = llm.bind_tools([get_api_documentation, query_company_api] + API_HANDOFF_TOOLS)
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{API_PROMPT}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
