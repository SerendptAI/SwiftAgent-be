from typing import Annotated, TypedDict, Optional
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """
    State dictionary for the LangGraph orchestration.
    """
    # Core conversation state
    messages: Annotated[list[BaseMessage], add_messages]
    
    # Request Context
    session_id: str
    company_id: str
    company_data: dict
    user_id: Optional[str]
    sdk_user_email: Optional[str]
    page_url: Optional[str]
    attachments: list[dict]
    agent_provider: str  # 'anthropic', 'gemini', 'openrouter'
    
    # Multi-language support
    user_language: str  # ISO 639-1 code (e.g., 'en', 'fr', 'es')
    kb_language: str    # language for KB search (may differ from user_language)
    language_instruction: str  # injected into system prompt
    
    # Internal Routing State
    intent: Optional[str]
    escalate_to_human: bool
