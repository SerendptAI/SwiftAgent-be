import asyncio
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.services.email_utils import add_html_with_inline_images

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "email_templates" / "welcome.html"


async def send_welcome_email(to_email: str, name: Optional[str] = None) -> None:
    """Send the welcome HTML email to `to_email` using Zoho SMTP.

    This runs the blocking SMTP send in a thread to avoid blocking the event loop.
    """
    try:
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        html = html.replace("{{base_url}}", settings.API_BASE_URL)
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

    subject = "Welcome to Swift Agent"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.active_sender_email
    msg["To"] = to_email
    msg.set_content("Welcome to Swift Agent! Please view this email in an HTML-capable client.")

    add_html_with_inline_images(msg, html)

    def _send() -> None:
        try:
            with smtplib.SMTP_SSL(settings.active_smtp_server, settings.active_smtp_port) as smtp:
                smtp.login(settings.active_smtp_username, settings.active_smtp_password)
                smtp.send_message(msg)
            logger.info("Sent welcome email to %s", to_email)
        except Exception as e:
            logger.exception("Failed to send welcome email to %s: %s", to_email, e)

    await asyncio.to_thread(_send)
