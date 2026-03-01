from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    MONGO_URI: str
    MONGO_DB_NAME: str = "swift_agent"
    QDRANT_URL: str
    QDRANT_API_KEY: str
    QDRANT_COLLECTION_NAME: str = "knowledge_docs"
    GOOGLE_CLIENT_ID: str
    GOOGLE_CLIENT_SECRET: str
    SECRET_KEY: str
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    API_BASE_URL: str
    CONFIDENCE_THRESHOLD: float = 0.7

    # Gemini AI
    GEMINI_API_KEY: str = ""

    # Blockchain explorer API keys (optional per-chain)
    ETHERSCAN_API_KEY: str = ""
    BSCSCAN_API_KEY: str = ""
    POLYGONSCAN_API_KEY: str = ""
    ARBISCAN_API_KEY: str = ""
    BASESCAN_API_KEY: str = ""
    AVALANCHE_API_KEY: str = ""

    model_config = SettingsConfigDict(env_file=".env")

settings = Settings()
