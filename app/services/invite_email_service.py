import asyncio
import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from app.core.config import settings
from app.services.email_utils import add_html_with_inline_images

logger = logging.getLogger(__name__)

_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "email_templates" / "team_member_invite.html"

async def send_invite_email(to_email: str, company_name: str, accept_link: str) -> None:
    """Send an invitation HTML email to `to_email` using Zoho SMTP."""

    try:
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        html = (
            html.replace("{{company_name}}", company_name)
                .replace("{{invite_url}}", accept_link)
        )
    except Exception:
        logger.exception("Failed to load invite template, falling back to simple text.")
        html = f"<p>You are invited to join {company_name}. Click <a href='{accept_link}'>here</a> to accept.</p>"

    subject = f"You are invited to manage {company_name}"
    msg = EmailMessage()
    msg["Subject"] = subject

    if settings.ZOHO_EMAIL:
        msg["From"] = settings.ZOHO_EMAIL

    msg["To"] = to_email
    msg.set_content(f"You have been invited to manage {company_name}. Please accept here: {accept_link}")

    add_html_with_inline_images(msg, html)

    def _send() -> None:
        if not (settings.ZOHO_EMAIL and settings.ZOHO_APP_PASSWORD and settings.ZOHO_SMTP_SERVER):
            logger.warning("SMTP not configured. Skipping invite email to %s. Link: %s", to_email, accept_link)
            return

        try:
            with smtplib.SMTP_SSL(settings.ZOHO_SMTP_SERVER, settings.ZOHO_SMTP_PORT) as smtp:
                smtp.login(settings.ZOHO_EMAIL, settings.ZOHO_APP_PASSWORD)
                smtp.send_message(msg)
            logger.info("Sent invite email to %s", to_email)
        except Exception as e:
            logger.exception("Failed to send invite email to %s: %s", to_email, e)

    await asyncio.to_thread(_send)
