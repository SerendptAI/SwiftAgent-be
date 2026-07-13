from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import read_website_page
from langchain_core.messages import SystemMessage

SCRAPER_PROMPT = """You are the Web Scraper Expert Agent.
Your job is to read and extract information from public URLs provided by the user.
Use the `read_website_page` tool to fetch the text content and links of the URL.
Summarize or answer the user's specific questions based ONLY on the content of the page."""

async def scraper_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([read_website_page])
    
    messages = [SystemMessage(content=SCRAPER_PROMPT)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
