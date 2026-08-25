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
    user_language: str
    kb_language: str
    language_instruction: str
    
    # Handoff context
    handoff_context: Optional[dict]
    handoff_initiated: bool
    
    # Internal Routing State
    intent: Optional[str]
    escalate_to_human: bool
