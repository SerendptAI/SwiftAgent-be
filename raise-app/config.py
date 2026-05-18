import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from raise-app directory
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    """Raise App Configuration."""

    # Auth
    RAISE_APP_PASSWORD: str = os.getenv("RAISE_APP_PASSWORD")
    SECRET_KEY: str = os.getenv("SECRET_KEY")

    # SMTP / ZeptoMail
    SMTP_SERVER: str = os.getenv("SMTP_SERVER", "smtp.zeptomail.com")
    SMTP_PORT: int = int(os.getenv("SMTP_PORT", "465"))
    # ZeptoMail requires 'emailapikey' as username. Fall back to SMTP_EMAIL for backwards compat.
    SMTP_USERNAME: str = os.getenv("SMTP_USERNAME") or os.getenv("SMTP_EMAIL", "emailapikey")
    SENDER_EMAIL: str = os.getenv("SENDER_EMAIL") or os.getenv("SMTP_EMAIL", "raise@swiftagents.org")
    SMTP_PASSWORD: str = os.getenv("SMTP_PASSWORD") or ""

    # App
    APP_HOST: str = os.getenv("APP_HOST") or "0.0.0.0"
    APP_PORT: int = int(os.getenv("APP_PORT") or "8500")
    APP_ENV: str = os.getenv("APP_ENV") or "production"
    TEST_MODE_EMAIL: str = os.getenv("TEST_MODE_EMAIL", "abdulabiola21@gmail.com")

    # Data
    VC_FILE_PATH: str = os.getenv("VC_FILE_PATH", "data/VC.xlsx")

    # MongoDB
    MONGO_URI: str = os.getenv("MONGO_URI", "")
    MONGO_DB_NAME: str = os.getenv("MONGO_DB_NAME", "SwiftAgentsDB")


config = Config()
