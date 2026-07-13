import logging
from app.services.graph.llm_factory import get_llm
from langchain_core.messages import SystemMessage, HumanMessage

logger = logging.getLogger(__name__)

async def generate_chat_title(message: str, provider: str = "anthropic") -> str:
    """Generate a short 3-5 word title for the chat session based on the first message."""
    if len(message) > 500:
        message = message[:500] + "..."
        
    try:
        llm = get_llm(provider, fast_routing=True, streaming=False)
        prompt = "You are a helpful assistant. Please generate a very short 2-5 word title for this conversation based on the user's first message. Respond ONLY with the title, no quotes or extra text."
        
        response = await llm.ainvoke([
            SystemMessage(content=prompt),
            HumanMessage(content=message)
        ])
        
        title = response.content.strip().strip('"').strip("'")
        if len(title) > 50:
            title = title[:47] + "..."
        return title
    except Exception as e:
        logger.warning(f"Failed to generate chat title: {e}")
        return "New Chat"
