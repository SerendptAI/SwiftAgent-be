"""
Language Detection and Multi-Language Agent System
===================================================
Detects user language per message and routes to language-specific
knowledge bases and agent responses.
"""

import logging
from typing import Dict, List, Optional

from app.core.config import settings
from app.models.language_models import (
    LanguageDetection,
    CompanyLanguageConfig,
    get_language_name,
    is_supported,
    is_reliable_for_detection,
)

logger = logging.getLogger(__name__)


# ============================================================================
# LANGUAGE DETECTION
# ============================================================================

_DETECTION_PROMPT = """Detect the language of this text. Return ONLY a JSON object:
{{"language_code": "en", "confidence": 0.98}}

Rules:
- Use ISO 639-1 codes (en, fr, es, de, it, pt, nl, ru, zh, ja, ko, ar, hi, tr, pl, sv, da, no, fi, cs, el, he, th, vi, id, ms, uk, ro, hu, bg, hr, sk, sl, lt, lv, et, sr, ca)
- Be confident only for clear, unambiguous text
- For very short text (1-2 words) or mixed language, set confidence lower (0.3-0.6)
- For empty or whitespace-only text, return {{"language_code": "en", "confidence": 0.0}}

Text to analyze:
"{text}" """

_TRANSLATION_PROMPT = """Translate the following text to {target_language}.
Return ONLY the translated text, nothing else.

Text:
"{text}" """

_KB_SEARCH_TRANSLATE_PROMPT = """The user asked a question in {source_language}. The knowledge base is in {target_language}.
Generate a search query in {target_language} that would find the most relevant articles.

User question: "{query}"

Return ONLY the search query in {target_language}."""


async def detect_language(text: str) -> Optional[LanguageDetection]:
    """Detect the language of a text using Gemini Flash."""
    if not text or not text.strip():
        return LanguageDetection(
            language_code="en",
            language_name="English",
            confidence=0.0,
            is_reliable=False,
        )

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from app.services.graph.llm_factory import get_llm

        llm = get_llm("gemini", fast_routing=True, streaming=False)
        messages = [
            SystemMessage(content="You are a language detection engine. Return ONLY JSON, no other text."),
            HumanMessage(content=_DETECTION_PROMPT.format(text=text[:200])),
        ]
        response = await llm.ainvoke(messages)
        text_resp = response.content if isinstance(response.content, str) else str(response.content)
        text_resp = text_resp.strip()

        # Strip markdown
        if text_resp.startswith("```"):
            text_resp = text_resp.split("\n", 1)[1] if "\n" in text_resp else text_resp[3:]
            if text_resp.endswith("```"):
                text_resp = text_resp[:-3]
            text_resp = text_resp.strip()
            
        import json
        result = json.loads(text_resp)
        code = result.get("language_code", "en").lower()
        confidence = result.get("confidence", 0.5)

        # Fallback if unsupported
        if not is_supported(code):
            logger.warning(f"Unsupported language detected: {code}, falling back to 'en'")
            code = "en"

        return LanguageDetection(
            language_code=code,
            language_name=get_language_name(code),
            confidence=confidence,
            is_reliable=confidence >= 0.6 and is_reliable_for_detection(code),
        )

    except Exception as e:
        logger.warning(f"Language detection failed: {e}")
        return LanguageDetection(
            language_code="en",
            language_name="English",
            confidence=0.0,
            is_reliable=False,
        )


# ============================================================================
# LANGUAGE CONFIGURATION
# ============================================================================


async def get_company_language_config(company_data: Dict) -> CompanyLanguageConfig:
    """Extract language config from company data with sensible defaults."""
    config = CompanyLanguageConfig()

    if company_data:
        config.primary_language = company_data.get("primary_language", "en")
        supported = company_data.get("supported_languages")
        if supported is not None:
            config.supported_languages = supported
        config.auto_detect = company_data.get("auto_detect_language", True)
        config.language_specific_kb = company_data.get("language_specific_kb", True)

    return config


# ============================================================================
# RESPONSE LANGUAGE INSTRUCTIONS
# ============================================================================


def build_language_instruction(
    user_language: str,
    company_config: CompanyLanguageConfig,
) -> str:
    """Build language instruction for the system prompt."""
    if user_language == company_config.primary_language:
        return ""

    user_lang_name = get_language_name(user_language)

    return (
        f"\nLANGUAGE INSTRUCTION: The user is writing in {user_lang_name}. "
        f"You MUST explicitly match the customer's incoming language ({user_lang_name}) for maximum customer service satisfaction. "
        f"Respond in {user_lang_name}.\n"
    )


# ============================================================================
# KNOWLEDGE BASE ROUTING
# ============================================================================


async def get_kb_search_query(
    query: str,
    user_language: str,
    kb_language: str,
) -> str:
    """
    Generate a search query in the KB language for cross-lingual search.
    Returns the original query if languages match.
    """
    if user_language == kb_language:
        return query

    user_lang_name = get_language_name(user_language)
    kb_lang_name = get_language_name(kb_language)

    try:
        from langchain_core.messages import HumanMessage, SystemMessage
        from app.services.graph.llm_factory import get_llm

        llm = get_llm("gemini", fast_routing=True, streaming=False)
        messages = [
            SystemMessage(content="You are a translation engine. Return ONLY the translated text."),
            HumanMessage(content=_KB_SEARCH_TRANSLATE_PROMPT.format(
                source_language=user_lang_name,
                target_language=kb_lang_name,
                query=query[:500],
            )),
        ]

        response = await llm.ainvoke(messages)
        result = response.content if isinstance(response.content, str) else str(response.content)
        return result.strip()
    except Exception as e:
        logger.warning(f"KB query translation failed: {e}, using original query")
        return query




# ============================================================================
# AGENT STATE EXTENSION
# ============================================================================


def build_language_state_update(
    user_language: str,
    company_config: CompanyLanguageConfig,
    language_instruction: str,
) -> Dict:
    """Build the state update dict for language context."""
    return {
        "user_language": user_language,
        "kb_language": user_language if user_language in company_config.supported_languages else company_config.primary_language,
        "language_instruction": language_instruction,
    }
