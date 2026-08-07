from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langchain_core.language_models.chat_models import BaseChatModel

from langchain_anthropic import ChatAnthropic
from app.core.config import settings

def get_llm(provider: str, fast_routing: bool = False, streaming: bool = True, force_anthropic_native: bool = False) -> BaseChatModel:
    """
    Returns an instantiated LangChain ChatModel based on the selected provider.
    
    Args:
        provider: "anthropic" (via Cencori AI Gateway), "gemini", or "openrouter"
        fast_routing: If True, uses a smaller, faster model (e.g., Haiku or Flash) for orchestration.
        streaming: Whether to enable token streaming.
        force_anthropic_native: If True, bypasses Cencori's OpenAI proxy and uses native Anthropic SDK (avoids tool streaming bugs).
    """
    
    if force_anthropic_native and provider == "anthropic" and settings.ANTHROPIC_API_KEY:
        actual_model = settings.ANTHROPIC_MODEL
        if actual_model == "claude-haiku-4.5":
            actual_model = "claude-haiku-4-5-20251001"
        elif actual_model == "claude-sonnet-4-6":
            actual_model = "claude-sonnet-4-5-20250929"
        elif "haiku" in actual_model.lower():
            actual_model = "claude-haiku-4-5-20251001"
        elif "sonnet" in actual_model.lower():
            actual_model = "claude-sonnet-4-5-20250929"
        elif "opus" in actual_model.lower():
            actual_model = "claude-opus-4-6-20251101"

        return ChatAnthropic(
            model=actual_model,
            api_key=settings.ANTHROPIC_API_KEY,
            streaming=streaming,
            temperature=0.0,
            max_retries=1
        )
    
    if fast_routing:
        if provider == "gemini" and settings.GEMINI_API_KEY:
            return ChatGoogleGenerativeAI(
                model=settings.GEMINI_MODEL,
                api_key=settings.GEMINI_API_KEY,
                streaming=streaming,
                temperature=0.0,
                max_retries=1
            )
        elif provider == "anthropic" and (settings.CENCORI_API_KEY or settings.ANTHROPIC_API_KEY):
            return ChatOpenAI(
                model=settings.ANTHROPIC_MODEL,
                api_key=settings.CENCORI_API_KEY or settings.ANTHROPIC_API_KEY,
                base_url="https://api.cencori.com/v1",
                streaming=streaming,
                temperature=0.0,
                max_retries=1
            )
        elif provider == "openrouter" and settings.OPENROUTER_API_KEY:
            return ChatOpenAI(
                model=settings.OPENROUTER_MODEL,
                api_key=settings.OPENROUTER_API_KEY,
                base_url="https://openrouter.ai/api/v1",
                streaming=streaming,
                temperature=0.0,
                default_headers={"HTTP-Referer": settings.FRONTEND_URL, "X-Title": "SwiftAgent"},
                max_retries=1
            )
    # Primary Agents Model Selection
    if provider == "anthropic" and (settings.CENCORI_API_KEY or settings.ANTHROPIC_API_KEY):
        return ChatOpenAI(
            model=settings.ANTHROPIC_MODEL,
            api_key=settings.CENCORI_API_KEY or settings.ANTHROPIC_API_KEY,
            base_url="https://api.cencori.com/v1",
            streaming=streaming,
            temperature=0.0,
            max_retries=1
        )
    elif provider == "gemini" and settings.GEMINI_API_KEY:
        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            api_key=settings.GEMINI_API_KEY,
            streaming=streaming,
            temperature=0.0,
            max_retries=1
        )
    elif provider == "openrouter" and settings.OPENROUTER_API_KEY:
        return ChatOpenAI(
            model=settings.OPENROUTER_MODEL,
            api_key=settings.OPENROUTER_API_KEY,
            base_url="https://openrouter.ai/api/v1",
            streaming=streaming,
            temperature=0.0,
            default_headers={"HTTP-Referer": settings.FRONTEND_URL, "X-Title": "SwiftAgent"},
            max_retries=1
        )
    
    # Fallback cascade if requested provider is missing API keys
    if settings.CENCORI_API_KEY or settings.ANTHROPIC_API_KEY:
        return ChatOpenAI(
            model=settings.ANTHROPIC_MODEL,
            api_key=settings.CENCORI_API_KEY or settings.ANTHROPIC_API_KEY,
            base_url="https://api.cencori.com/v1",
            streaming=streaming,
            temperature=0.0,
            max_retries=1
        )
    elif settings.GEMINI_API_KEY:
        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_MODEL,
            api_key=settings.GEMINI_API_KEY,
            streaming=streaming,
            temperature=0.0,
            max_retries=1
        )
        
    raise ValueError(f"No configured API keys found for provider {provider} or fallbacks.")
