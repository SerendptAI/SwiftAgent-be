from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import read_website_page
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import SCRAPER_HANDOFF_TOOLS

SCRAPER_PROMPT = """You are the Web Scraper Expert.
COMPANY WEBSITE: {website}
1. If user asks about pricing/features, fetch: {website}
2. Use `read_website_page` to fetch content and links.
3. If info not on page, follow relevant links.
4. RETRY: If content missing, retry with force_refresh=True.
5. Answer based ONLY on page content.
FALLBACK: If tool fails, call transfer_to_knowledge. No text when transferring."""

@observe(name="scraper_agent_node")
async def scraper_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"], streaming=True, force_anthropic_native=True)
    llm_with_tools = llm.bind_tools([read_website_page] + SCRAPER_HANDOFF_TOOLS)
    
    from app.services.prompt_service import render_prompt_for_company
    rendered = await render_prompt_for_company("scraper_agent", state.get("company_data", {}), state.get("company_id"))
    
    if rendered.rendered_text:
        prompt_text = rendered.rendered_text
    else:
        website = state.get("company_data", {}).get("website", "")
        prompt_text = SCRAPER_PROMPT.format(website=website or "Not configured")
        
    persona = build_company_persona_prompt(
        state.get("company_data", {}),
        language_instruction=state.get("language_instruction", ""),
    )
    full_prompt = f"{persona}\n\n{prompt_text}"
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
