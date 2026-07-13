from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import get_dashboard_navigation, get_full_dashboard_documentation
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe

NAVIGATION_PROMPT = """You are the Dashboard Navigation Expert Agent.
Your job is to guide users through the dashboard UI.
Use `get_dashboard_navigation` to get a report of the dashboard layout.
If the user specifically asks for the FULL documentation, use `get_full_dashboard_documentation` and return the `navigation_steps` JSON block EXACTLY as provided.
Do not guess where things are. Always rely on the tool output."""

@observe(as_type="generation")
async def navigation_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([get_dashboard_navigation, get_full_dashboard_documentation])
    
    messages = [SystemMessage(content=NAVIGATION_PROMPT)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
