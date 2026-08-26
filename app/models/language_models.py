from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


# ============================================================================
# LANGUAGE MODELS
# ============================================================================


class LanguageDetection(BaseModel):
    """Result of language detection on a message."""
    language_code: str = Field(
        ...,
        description="ISO 639-1 language code (e.g., 'en', 'fr', 'es', 'de', 'zh')",
    )
    language_name: str = Field(
        ...,
        description="Human-readable language name (e.g., 'English', 'French')",
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Detection confidence",
    )
    is_reliable: bool = Field(
        ...,
        description="Whether the detection is reliable enough to act on",
    )


class CompanyLanguageConfig(BaseModel):
    """Language configuration for a company."""
    primary_language: str = Field(
        default="en",
        description="Primary/fallback language code",
    )
    supported_languages: List[str] = Field(
        default_factory=lambda: ["en"],
        description="List of supported language codes",
    )
    auto_detect: bool = Field(
        default=True,
        description="Auto-detect user language for each message",
    )
    language_specific_kb: bool = Field(
        default=True,
        description="Use language-specific knowledge bases",
    )


# ============================================================================
# SUPPORTED LANGUAGES
# ============================================================================


SUPPORTED_LANGUAGES = {
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "ru": "Russian",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "ar": "Arabic",
    "hi": "Hindi",
    "tr": "Turkish",
    "pl": "Polish",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "cs": "Czech",
    "el": "Greek",
    "he": "Hebrew",
    "th": "Thai",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ms": "Malay",
    "uk": "Ukrainian",
    "ro": "Romanian",
    "hu": "Hungarian",
    "bg": "Bulgarian",
    "hr": "Croatian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "et": "Estonian",
    "sr": "Serbian",
    "ca": "Catalan",
}

# Languages where Gemini Flash is reliable for detection and generation
GEMINI_RELIABLE_LANGUAGES = {
    "en", "fr", "es", "de", "it", "pt", "nl", "ru", "zh", "ja", "ko",
    "ar", "hi", "tr", "pl", "sv", "da", "no", "fi", "th", "vi", "id",
}


def get_language_name(code: str) -> str:
    """Get human-readable name for a language code."""
    return SUPPORTED_LANGUAGES.get(code.lower(), "Unknown")


def is_supported(code: str) -> bool:
    """Check if a language code is supported."""
    return code.lower() in SUPPORTED_LANGUAGES


def is_reliable_for_detection(code: str) -> bool:
    """Check if a language is reliable for auto-detection."""
    return code.lower() in GEMINI_RELIABLE_LANGUAGES
