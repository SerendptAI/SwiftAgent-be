from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import search_knowledge_base, scrape_documentation_link
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import KNOWLEDGE_HANDOFF_TOOLS

KNOWLEDGE_PROMPT = """You are the Knowledge Base Expert.
Always use `search_knowledge_base` to find answers. Never guess or hallucinate.
If search returns no results, call transfer_to_scraper to check the website.
No text before tool calls."""

@observe(name="knowledge_agent_node")
async def knowledge_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"], streaming=True, force_anthropic_native=True)
    llm_with_tools = llm.bind_tools([search_knowledge_base, scrape_documentation_link] + KNOWLEDGE_HANDOFF_TOOLS)
    
    from app.services.prompt_service import render_prompt_for_company
    rendered = await render_prompt_for_company("knowledge_agent", state.get("company_data", {}), state.get("company_id"))
    prompt_text = rendered.rendered_text if rendered.rendered_text else KNOWLEDGE_PROMPT
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{prompt_text}"
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
