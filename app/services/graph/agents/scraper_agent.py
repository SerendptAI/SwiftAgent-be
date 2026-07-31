from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import read_website_page
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import ROUTING_TOOLS

SCRAPER_PROMPT = """You are the Web Scraper Expert.
Your job is to read and extract information from public URLs provided by the user.

COMPANY WEBSITE: {website}

WORKFLOW:
1. If the user asks about pricing, features, eligibility, use cases, or general information and doesn't provide a specific URL, use the `read_website_page` tool to fetch the company's official website: {website}
2. Use the `read_website_page` tool to fetch the text content and links of the URL.
3. If the information you need is NOT on the current page, look at the `links` returned by the tool and call `read_website_page` AGAIN on the most relevant link (e.g., a link containing "pricing", "plans", "features", "about", or "use-cases") to find the information.
4. RETRY RULE: If you called `read_website_page` and the returned content does not contain the information the user needs (e.g. it's a cached page missing the relevant section), you MUST retry the call with `force_refresh=True` to fetch a fresh copy. Do NOT give up after one attempt.
5. Summarize or answer the user's specific questions based ONLY on the content of the page.
6. If after retrying with force_refresh and following relevant links you still cannot find the information, use the available transfer tools to hand off the task to another appropriate agent (like the knowledge agent). Do NOT tell the user you don't know without first exhausting these options.

FALLBACK RULE: If the `read_website_page` tool returns an error or no content at all, tell the user clearly: "I'm having trouble loading the page right now. Let me check our knowledge base instead." and then transfer to the knowledge agent. NEVER go silent.

CRITICAL RULE: When you need to call a tool (including handoff/transfer tools), you MUST NOT output ANY conversational text or "thinking" before the tool call! ONLY return the tool call itself."""

@observe(name="scraper_agent_node")
async def scraper_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([read_website_page] + ROUTING_TOOLS)
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    website = state.get("company_data", {}).get("website", "")
    scraper_prompt = SCRAPER_PROMPT.format(website=website or "Not configured")
    full_prompt = f"{persona}\n\n{scraper_prompt}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
