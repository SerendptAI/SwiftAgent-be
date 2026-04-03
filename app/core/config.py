from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    MONGO_URI: str
    MONGO_DB_NAME: str = "swift_agent"
    QDRANT_URL: str
    QDRANT_API_KEY: str
    QDRANT_COLLECTION_NAME: str = "knowledge_docs_gemini"
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    API_BASE_URL: str
    CONFIDENCE_THRESHOLD: float = 0.7
    REFERRAL_CODE: str = ""
    # gemini AI
    GEMINI_API_KEY: str = ""
    # anthropic AI
    ANTHROPIC_API_KEY: str = ""
    # cloudinary (file uploads)
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""
    # fish.audio (voice STT/TTS)
    FISH_AUDIO_API_KEY: str = ""
    FISH_AUDIO_VOICE_ID: str = ""
    # etherscan API V2 — single key for all EVM chains
    ETHERSCAN_API_KEY: str = ""
    # stroll (dashboard crawler)
    STROLL_MAX_PAGES: int = 50
    STROLL_PAGE_TIMEOUT_MS: int = 10000
    STROLL_SCREENSHOT_QUALITY: int = 70

    # Zoho SMTP (welcome emails)
    ZOHO_APP_PASSWORD: str = ""
    ZOHO_EMAIL: str = ""
    ZOHO_SMTP_PORT: int = 465
    ZOHO_SMTP_SERVER: str = "smtp.zoho.com"

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()
