from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import search_knowledge_base, scrape_documentation_link
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe
from app.services.graph.orchestrator import KNOWLEDGE_HANDOFF_TOOLS

KNOWLEDGE_PROMPT = """You are the Knowledge Base Expert.
Your job is to answer the user's question using the company's knowledge base.
Always use the `search_knowledge_base` tool to find answers. 
If the user provides a link and asks you to learn from it, use `scrape_documentation_link`.
Never guess or hallucinate information. If the answer is not in the knowledge base, do not conclude or guess. Instead, use the available transfer tools to hand off the task to another appropriate agent (like the scraper or navigation agent).
If the user asks about pricing or plans and it's not in the knowledge base, transfer to the scraper agent so it can check the website.
SEAMLESS FALLBACK RULE: If search_knowledge_base returns no results or cant find the answer in the retrieved info, you MUST immediately call transfer_to_scraper to check the company's official website. Never say you don't know without checking the website first.
CRITICAL RULE: When you need to call a tool (including handoff/transfer tools), you MUST NOT output ANY conversational text or "thinking" before the tool call! ONLY return the tool call itself."""

@observe(name="knowledge_agent_node")
async def knowledge_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"], streaming=True, force_anthropic_native=True)
    llm_with_tools = llm.bind_tools([search_knowledge_base, scrape_documentation_link] + KNOWLEDGE_HANDOFF_TOOLS)
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{KNOWLEDGE_PROMPT}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
