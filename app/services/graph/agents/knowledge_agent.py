from app.services.graph.state import AgentState
from app.services.graph.llm_factory import get_llm
from app.services.graph.tools import search_knowledge_base, scrape_documentation_link
from app.services.graph.prompt_utils import build_company_persona_prompt
from langchain_core.messages import SystemMessage
from app.core.langfuse import observe

KNOWLEDGE_PROMPT = """You are the Knowledge Base Expert.
Your job is to answer the user's question using the company's knowledge base.
Always use the `search_knowledge_base` tool to find answers.

CRITICAL RULES:
- The `search_knowledge_base` tool returns the GROUND TRUTH for this company.
- If the tool returns results, base your answer strictly on those results.
- If the tool returns an error or empty results, tell the user:
  "I couldn't retrieve that information from our knowledge base right now.
   This may be a temporary issue — please try again in a moment, or contact
   our support team for immediate help."
  Do NOT claim access is restricted, do NOT redirect to sales, do NOT apologize
  for policy limits. The retrieval system is internal — the user does not need
  to know about it.

If the user provides a link and asks you to learn from it, use `scrape_documentation_link`.
Never guess or hallucinate information."""

@observe(name="knowledge_agent_node")
async def knowledge_agent_node(state: AgentState, config):
    llm = get_llm(state["agent_provider"])
    llm_with_tools = llm.bind_tools([search_knowledge_base, scrape_documentation_link])
    
    persona = build_company_persona_prompt(state.get("company_data", {}))
    full_prompt = f"{persona}\n\n{KNOWLEDGE_PROMPT}"
    
    messages = [SystemMessage(content=full_prompt)] + state["messages"]
    response = await llm_with_tools.ainvoke(messages, config)
    return {"messages": [response]}
