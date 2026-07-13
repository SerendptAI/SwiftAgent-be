from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import search_knowledge_base, scrape_documentation_link
from langchain_core.messages import SystemMessage

KNOWLEDGE_PROMPT = """You are the Knowledge Base Expert Agent.
Your job is to answer the user's question using the company's knowledge base.
Always use the `search_knowledge_base` tool to find answers. 
If the user provides a link and asks you to learn from it, use `scrape_documentation_link`.
Never guess or hallucinate information. If the answer is not in the knowledge base, say so clearly."""

async def knowledge_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([search_knowledge_base, scrape_documentation_link])
    
    messages = [SystemMessage(content=KNOWLEDGE_PROMPT)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
