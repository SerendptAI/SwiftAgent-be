import asyncio
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.services.email_utils import get_image_data, process_html_for_inline_images

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "email_templates" / "welcome.html"


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

    subject = "Welcome to Swift Agent"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.ZOHO_EMAIL
    msg["To"] = to_email
    msg.set_content("Welcome to Swift Agent! Please view this email in an HTML-capable client.")

    html, attachments = process_html_for_inline_images(html)
    msg.add_alternative(html, subtype="html")

    for filename, cid in attachments.items():
        try:
            data, maintype, subtype = get_image_data(filename)
            msg.get_payload()[1].add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")
        except Exception:
            logger.warning("Could not attach image %s to welcome email.", filename)

    def _send() -> None:
        try:
            with smtplib.SMTP_SSL(settings.ZOHO_SMTP_SERVER, settings.ZOHO_SMTP_PORT) as smtp:
                smtp.login(settings.ZOHO_EMAIL, settings.ZOHO_APP_PASSWORD)
                smtp.send_message(msg)
            logger.info("Sent welcome email to %s", to_email)
        except Exception as e:
            logger.exception("Failed to send welcome email to %s: %s", to_email, e)

    await asyncio.to_thread(_send)
