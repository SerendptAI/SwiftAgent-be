import asyncio
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# locate welcome.html at repository root
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "welcome.html"

# 1x1 transparent PNG data URI fallback for missing images
TRANSPARENT_PNG = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMA" 
    "ASsJTYQAAAAASUVORK5CYII="
)


async def send_welcome_email(to_email: str, name: Optional[str] = None) -> None:
    """Send the welcome HTML email to `to_email` using Zoho SMTP.

    This runs the blocking SMTP send in a thread to avoid blocking the event loop.
    """
    try:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
    except Exception as e:
        logger.exception("Failed to read welcome.html: %s", e)
        return

    # Replace template placeholders with provided values or sensible defaults
    html = html.replace("{{name}}", name or "")

    # dashboard URL fallback to API base URL
    dashboard_url = getattr(settings, "API_BASE_URL", "") or ""
    # ensure scheme present
    if dashboard_url and not dashboard_url.startswith("http"):
        dashboard_url = f"https://{dashboard_url}"

    html = html.replace("{{dashboardUrl}}", dashboard_url or "#")

    # logo and hero image fallbacks
    html = html.replace("{{logoUrl}}", getattr(settings, "LOGO_URL", TRANSPARENT_PNG) or TRANSPARENT_PNG)
    html = html.replace("{{heroImageUrl}}", getattr(settings, "HERO_IMAGE_URL", TRANSPARENT_PNG) or TRANSPARENT_PNG)

    subject = "Welcome to Swift Agent"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.ZOHO_EMAIL
    msg["To"] = to_email
    msg.set_content("Welcome to Swift Agent! Please view this email in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    def _send() -> None:
        try:
            with smtplib.SMTP_SSL(settings.ZOHO_SMTP_SERVER, settings.ZOHO_SMTP_PORT) as smtp:
                smtp.login(settings.ZOHO_EMAIL, settings.ZOHO_APP_PASSWORD)
                smtp.send_message(msg)
            logger.info("Sent welcome email to %s", to_email)
        except Exception as e:
            logger.exception("Failed to send welcome email to %s: %s", to_email, e)

    await asyncio.to_thread(_send)
