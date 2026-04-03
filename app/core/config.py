from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional


class Settings(BaseSettings):
    # environment
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"

    # database
    MONGO_URI: str
    MONGO_DB_NAME: str = "swift_agent"
    MONGO_MAX_POOL_SIZE: int = 100
    MONGO_MIN_POOL_SIZE: int = 10

    # qdrant
    QDRANT_URL: str
    QDRANT_API_KEY: str
    QDRANT_COLLECTION_NAME: str = "knowledge_docs_gemini"

    # google oauth
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str

    # jwt
    SECRET_KEY: str
    REFRESH_SECRET_KEY: str = ""
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # app
    API_BASE_URL: str
    CONFIDENCE_THRESHOLD: float = 0.7
    REFERRAL_CODE: str = ""
    ALLOWED_HOSTS: str = "http://localhost:3000,https://swiftagents.org,https://www.swiftagents.org"

    # rate limiting
    RATE_LIMIT_PER_MINUTE: int = 60
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10

    # file uploads
    MAX_UPLOAD_SIZE_BYTES: int = 52_428_800  # 50MB

    # ai models
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-20250514"

    # cloudinary
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # fish.audio
    FISH_AUDIO_API_KEY: str = ""
    FISH_AUDIO_VOICE_ID: str = ""

    # blockchain
    ETHERSCAN_API_KEY: str = ""

    # knowledge base
    EMBEDDING_MODEL: str = "gemini-embedding-001"
    EMBEDDING_DIMENSION: int = 3072

    # stroll
    STROLL_MAX_PAGES: int = 50
    STROLL_PAGE_TIMEOUT_MS: int = 10000
    STROLL_SCREENSHOT_QUALITY: int = 70

    # Zoho SMTP (welcome emails)
    ZOHO_APP_PASSWORD: str = ""
    ZOHO_EMAIL: str = ""
    ZOHO_SMTP_PORT: int = 465
    ZOHO_SMTP_SERVER: str = "smtp.zoho.com"

    # pagination defaults
    DEFAULT_PAGE_LIMIT: int = 20
    MAX_PAGE_LIMIT: int = 100
    DEFAULT_CONVERSATION_LIMIT: int = 50

    # context window
    MAX_CONTEXT_MESSAGES: int = 10

    @field_validator("MONGO_URI", "QDRANT_URL", "SECRET_KEY", mode="before")
    @classmethod
    def validate_required_fields(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("This field is required and cannot be empty")
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}")
        return v.upper()

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT.lower() in ("development", "dev", "local")

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()]

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
