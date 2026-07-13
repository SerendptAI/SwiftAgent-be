from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import settings


def get_llm(provider: str, fast_routing: bool = False, streaming: bool = True) -> BaseChatModel:
    """
    Returns an instantiated LangChain ChatModel based on the selected provider.
    
    Args:
        provider: "anthropic", "gemini", or "openrouter"
        fast_routing: If True, uses a smaller, faster model (e.g., Haiku or Flash) for orchestration.
        streaming: Whether to enable token streaming.
    """
    
    if fast_routing:
        if provider == "gemini" and settings.GEMINI_API_KEY:
            return ChatGoogleGenerativeAI(
                model="gemini-2.5-flash",
                api_key=settings.GEMINI_API_KEY,
                streaming=streaming,
                temperature=0.0
            )
        elif provider == "anthropic" and settings.ANTHROPIC_API_KEY:
            return ChatAnthropic(
                model="claude-3-5-haiku-20241022",
                api_key=settings.ANTHROPIC_API_KEY,
                streaming=streaming,
                temperature=0.0
            )
        elif provider == "openrouter" and settings.OPENROUTER_API_KEY:
            return ChatOpenAI(
                model=settings.OPENROUTER_MODEL,
                api_key=settings.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
                streaming=streaming,
                temperature=0.0,
                default_headers={"HTTP-Referer": settings.FRONTEND_URL, "X-Title": "SwiftAgent"},
            )
    # Primary Agents Model Selection
    if provider == "anthropic" and settings.ANTHROPIC_API_KEY:
        return ChatAnthropic(
            model=settings.ANTHROPIC_MODEL,
            api_key=settings.ANTHROPIC_API_KEY,
            streaming=streaming,
            temperature=0.0
        )
    elif provider == "gemini" and settings.GEMINI_API_KEY:
        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            api_key=settings.GEMINI_API_KEY,
            streaming=streaming,
            temperature=0.0
        )
    elif provider == "openrouter" and settings.OPENROUTER_API_KEY:
        return ChatOpenAI(
            model=settings.OPENROUTER_MODEL,
            api_key=settings.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            streaming=streaming,
            temperature=0.0,
            default_headers={"HTTP-Referer": settings.FRONTEND_URL, "X-Title": "SwiftAgent"},
        )
    
    # Fallback cascade if requested provider is missing API keys
    if settings.ANTHROPIC_API_KEY:
        return ChatAnthropic(
            model=settings.ANTHROPIC_MODEL,
            api_key=settings.ANTHROPIC_API_KEY,
            streaming=streaming,
            temperature=0.0
        )
    elif settings.GEMINI_API_KEY:
        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            api_key=settings.GEMINI_API_KEY,
            streaming=streaming,
            temperature=0.0
        )
        
    raise ValueError(f"No configured API keys found for provider {provider} or fallbacks.")
