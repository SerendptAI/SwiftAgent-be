from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from typing import Optional


class Settings(BaseSettings):
    # environment
    ENVIRONMENT: str = "production"
    LOG_LEVEL: str = "INFO"

    # database
    MONGO_URI: str
    MONGO_DB_NAME: str = "SwiftAgentsDB"
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
    FRONTEND_URL: str = "https://swiftagents.org"
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
    ANTHROPIC_MODEL: str = "claude-haiku-4-5-20251001"
    OPENROUTER_API_KEY: str = ""
    OPENROUTER_MODEL: str = "google/gemma-4-31b-it:free"

    # cloudinary
    CLOUDINARY_CLOUD_NAME: str = ""
    CLOUDINARY_API_KEY: str = ""
    CLOUDINARY_API_SECRET: str = ""

    # fish.audio
    FISH_AUDIO_API_KEY: str = ""
    FISH_AUDIO_VOICE_ID: str = ""

    # billing
    POLAR_API_URL: str = "https://api.polar.sh/v1"
    POLAR_ACCESS_TOKEN: str = ""
    POLAR_WEBHOOK_SECRET: str = ""
    
    # Polar product IDs — African pricing
    POLAR_PRODUCT_BASIC_AF: str = "76c6aab0-e9df-4528-b418-e16aa70abba5"
    POLAR_PRODUCT_PRO_AF: str = "9e6e7deb-a5e4-40b0-ad8e-1106a3d3a76c"
    POLAR_PRODUCT_ENTERPRISE_AF: str = "bb76d48f-e41e-4a16-9a33-1ee1c0f4786f"
    
    # Polar product IDs — International pricing
    POLAR_PRODUCT_BASIC_INTL: str = "52c04072-a8a6-4ad7-a999-374079acbfd1"
    POLAR_PRODUCT_PRO_INTL: str = "8ff00348-f76a-4b5a-afd0-207e39ba910e"
    POLAR_PRODUCT_ENTERPRISE_INTL: str = "5d3eca44-241f-43cd-a4c7-bc2a00f0ffd4"
    
    # Discount for International Basic (50% off)
    POLAR_DISCOUNT_BASIC_INTL: str = "845c8f40-0c9b-4743-8abb-c5bc944ff829"

    # blockchain
    ETHERSCAN_API_KEY: str = ""

    # knowledge base
    EMBEDDING_MODEL: str = "gemini-embedding-001"
    EMBEDDING_DIMENSION: int = 3072

    # stroll
    STROLL_MAX_PAGES: int = 50
    STROLL_PAGE_TIMEOUT_MS: int = 10000
    PLAYWRIGHT_WS_ENDPOINT: Optional[str] = "ws://browserless:3000"

    # Expo Push Notifications (optional — push works without it but recommended)
    EXPO_ACCESS_TOKEN: str = ""

    # Web Push Notifications (VAPID)
    VAPID_PRIVATE_KEY: str = ""
    VAPID_PUBLIC_KEY: str = ""
    VAPID_CLAIMS_EMAIL: str = "mailto:team@swiftagents.org"

    # OTP challenge (stroll 2FA relay)
    OTP_CHALLENGE_TIMEOUT_SECONDS: int = 120
    OTP_CHALLENGE_EXPIRY_SECONDS: int = 300
    STROLL_SCREENSHOT_QUALITY: int = 70

    # Zoho SMTP (welcome emails)
    ZOHO_APP_PASSWORD: str = ""
    ZOHO_EMAIL: str = ""
    ZOHO_SMTP_PORT: int = 465
    ZOHO_SMTP_SERVER: str = "smtp.zoho.com"

    # SendGrid (company email ticketing)
    SENDGRID_API_KEY: str = ""
    EMAIL_DOMAIN: str = "swfty.email"

    # otp / credential auth
    OTP_TTL_SIGNUP_MINUTES: int = 15
    OTP_TTL_LOGIN_MINUTES: int = 10
    OTP_GRACE_PERIOD_DAYS: int = 30  # skip OTP if last verified within N days

    # pagination defaults
    DEFAULT_PAGE_LIMIT: int = 20
    MAX_PAGE_LIMIT: int = 100
    DEFAULT_CONVERSATION_LIMIT: int = 50

    # context window
    MAX_CONTEXT_MESSAGES: int = 10

    # working memory cache
    WORKING_MEMORY_TTL_SECONDS: int = 3600  # 1 hour

    # memory retention
    EPISODIC_RETENTION_DAYS: int = 90
    SEMANTIC_RETENTION_DAYS: int = 365

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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
