import asyncio
import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger(__name__)

async def send_invite_email(to_email: str, company_name: str, accept_link: str) -> None:
    """Send an invitation HTML email to `to_email` using Zoho SMTP."""
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Dashboard Invitation</title>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f5; margin: 0; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1); }}
            .header {{ background-color: #1a56db; color: #ffffff; padding: 20px; text-align: center; }}
            .content {{ padding: 30px 20px; color: #374151; line-height: 1.6; text-align: center; }}
            .button {{ display: inline-block; padding: 12px 24px; margin-top: 20px; background-color: #1a56db; color: #ffffff; text-decoration: none; border-radius: 4px; font-weight: bold; }}
            .footer {{ background-color: #f9fafb; color: #6b7280; padding: 15px; text-align: center; font-size: 14px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>Dashboard Invitation</h2>
            </div>
            <div class="content">
                <p>Hello,</p>
                <p>You have been invited to manage the dashboard for <strong>{company_name}</strong> on Swift Agent.</p>
                <p>Click the button below to accept your invitation and access the dashboard:</p>
                <a href="{accept_link}" class="button">Accept Invitation</a>
                <p style="margin-top: 30px; font-size: 13px; color: #9ca3af;">This link will expire in 10 days.</p>
            </div>
            <div class="footer">
                <p>&copy; 2026 Swift Agent. All rights reserved.</p>
            </div>
        </div>
    </body>
    </html>
    """

    subject = f"You are invited to manage {company_name}"
    msg = EmailMessage()
    msg["Subject"] = subject
    
    # Avoid setting From header if settings don't exist in dev envs
    if settings.ZOHO_EMAIL:
        msg["From"] = settings.ZOHO_EMAIL
    
    msg["To"] = to_email
    msg.set_content(f"You have been invited to manage {company_name}. Please accept here: {accept_link}")
    msg.add_alternative(html, subtype="html")

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
