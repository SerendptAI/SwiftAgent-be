from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import read_website_page
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe

SCRAPER_PROMPT = """You are the Web Scraper Expert.
Your job is to read and extract information from public URLs provided by the user.
Use the `read_website_page` tool to fetch the text content and links of the URL.
The tool caches pages for 7 days. If the user indicates that the data you returned is outdated or specifically asks you to re-read it, set `force_refresh=True` to fetch fresh data.
Summarize or answer the user's specific questions based ONLY on the content of the page."""

@observe(name="scraper_agent_node")
async def scraper_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([read_website_page])
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{SCRAPER_PROMPT}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
