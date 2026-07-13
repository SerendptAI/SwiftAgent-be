from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import get_api_documentation, query_company_api
from langchain_core.messages import SystemMessage

API_PROMPT = """You are the API Integration Expert Agent.
Your job is to query the company's external API integrations to look up real-time data for the user.
1. Use `get_api_documentation` to understand available endpoints.
2. Use `query_company_api` to execute read-only GET requests based on those endpoints.
Return the data clearly formatted to the user."""

async def api_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([get_api_documentation, query_company_api])
    
    messages = [SystemMessage(content=API_PROMPT)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
