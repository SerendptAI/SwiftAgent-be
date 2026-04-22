import asyncio
import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
import mimetypes
from pathlib import Path
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# locate welcome.html at repository root
TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "welcome.html"
IMAGES_DIR = Path(__file__).resolve().parents[1] / "email_templates" / "images"

def _get_image_data(filename: str) -> tuple[bytes, str, str]:
    path = IMAGES_DIR / filename
    with path.open("rb") as f:
        data = f.read()
    ctype, _ = mimetypes.guess_type(str(path))
    maintype, subtype = (ctype or "image/jpeg").split("/")
    return data, maintype, subtype


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

    # dashboard URL fallback to FRONTEND_URL then API base URL
    frontend = getattr(settings, "FRONTEND_URL", None)
    api_base = getattr(settings, "API_BASE_URL", "")
    dashboard_url = frontend or api_base or ""
    # ensure scheme present
    if dashboard_url and not dashboard_url.startswith("http"):
        dashboard_url = f"https://{dashboard_url}"

    html = html.replace("{{dashboardUrl}}", dashboard_url or "#")

    # generate CIDs for images
    logo_cid = make_msgid(domain="swiftagent.com")
    hero_cid = make_msgid(domain="swiftagent.com")

    # logo and hero image fallbacks (use CID instead of transparency or base64)
    html = html.replace("{{logoUrl}}", f"cid:{logo_cid[1:-1]}")
    html = html.replace("{{heroImageUrl}}", f"cid:{hero_cid[1:-1]}")

    subject = "Welcome to Swift Agent"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.ZOHO_EMAIL
    msg["To"] = to_email
    msg.set_content("Welcome to Swift Agent! Please view this email in an HTML-capable client.")
    msg.add_alternative(html, subtype="html")

    # Attach inline images to the HTML payload
    try:
        logo_data, logo_main, logo_sub = _get_image_data("logo.png")
        msg.get_payload()[1].add_related(logo_data, maintype=logo_main, subtype=logo_sub, cid=logo_cid)
    except Exception as e:
        logger.warning("Could not attach logo.png: %s", e)

    try:
        hero_data, hero_main, hero_sub = _get_image_data("welcome.jpg")
        msg.get_payload()[1].add_related(hero_data, maintype=hero_main, subtype=hero_sub, cid=hero_cid)
    except Exception as e:
        logger.warning("Could not attach welcome.jpg: %s", e)

    def _send() -> None:
        try:
            with smtplib.SMTP_SSL(settings.ZOHO_SMTP_SERVER, settings.ZOHO_SMTP_PORT) as smtp:
                smtp.login(settings.ZOHO_EMAIL, settings.ZOHO_APP_PASSWORD)
                smtp.send_message(msg)
            logger.info("Sent welcome email to %s", to_email)
        except Exception as e:
            logger.exception("Failed to send welcome email to %s: %s", to_email, e)

    await asyncio.to_thread(_send)
