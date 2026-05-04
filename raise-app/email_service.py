"""
Email Service — Handles personalisation and SMTP delivery via Zoho.
"""

import asyncio
import logging
import smtplib
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import Dict, List, Optional

from config import config
from db import Database

logger = logging.getLogger(__name__)

# Personalisation placeholders:
# {{first_name}}, {{last_name}}, {{full_name}}, {{company}}, {{city}}, {{state}}
PLACEHOLDER_MAP = {
    "{{first_name}}": "First Name",
    "{{last_name}}": "Last Name",
    "{{full_name}}": "Full Name",
    "{{company}}": "Company",
    "{{city}}": "City",
    "{{state}}": "State",
    "{{country}}": "Country",
    "{{email}}": "Email",
    "{{phone}}": "Phone",
    "{{website}}": "Website",
}


class EmailService:
    """Send personalised emails via Zoho SMTP."""

    @staticmethod
    def personalise(template: str, recipient: Dict) -> str:
        """Replace placeholders with recipient data."""
        result = template
        for placeholder, key in PLACEHOLDER_MAP.items():
            value = recipient.get(key, "")
            result = result.replace(placeholder, value or "")
        return result

    @staticmethod
    async def send_single(
        to_email: str,
        subject: str,
        body: str,
        attachments: List[Dict] = None,
        recipient_info: Dict = None,
    ) -> bool:
        """Send a single email via Zoho SMTP."""
        
        # SAFEGUARD: Intercept emails if TEST_MODE_EMAIL is set
        original_to_email = to_email
        if getattr(config, "TEST_MODE_EMAIL", ""):
            to_email = config.TEST_MODE_EMAIL
            subject = f"[TEST for: {original_to_email}] {subject}"
            logger.info("TEST MODE ENABLED: Redirecting email intended for %s to %s", original_to_email, to_email)

        def _send():
            msg = MIMEMultipart("mixed")
            msg["Subject"] = subject
            msg["From"] = config.SMTP_EMAIL
            msg["To"] = to_email

            # Determine if body is HTML or plain text
            if "<" in body and ">" in body:
                msg.attach(MIMEText(body, "html", "utf-8"))
            else:
                msg.attach(MIMEText(body, "plain", "utf-8"))

            # Attachments
            if attachments:
                for att in attachments:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(att["content"])
                    encoders.encode_base64(part)
                    part.add_header(
                        "Content-Disposition",
                        f'attachment; filename="{att["filename"]}"',
                    )
                    msg.attach(part)

            with smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT) as smtp:
                smtp.login(config.SMTP_EMAIL, config.SMTP_PASSWORD)
                smtp.send_message(msg)

        try:
            await asyncio.to_thread(_send)
            logger.info("Email sent to %s", to_email)

            # Track in DB
            await Database.log_email_send(
                to_email=to_email,
                subject=subject,
                status="sent",
                recipient_info=recipient_info,
            )
            return True
        except Exception as e:
            logger.exception("Failed to send email to %s: %s", to_email, e)
            await Database.log_email_send(
                to_email=to_email,
                subject=subject,
                status="failed",
                error=str(e),
                recipient_info=recipient_info,
            )
            raise

    @staticmethod
    async def send_bulk(
        subject: str,
        body: str,
        recipients: List[Dict],
        attachments: List[Dict] = None,
    ) -> Dict:
        """Send personalised emails to multiple recipients."""
        results = {"sent": 0, "failed": 0, "errors": []}

        for recipient in recipients:
            email = recipient.get("Email", "").strip()
            if not email:
                continue

            personalised_subject = EmailService.personalise(subject, recipient)
            personalised_body = EmailService.personalise(body, recipient)

            try:
                await EmailService.send_single(
                    to_email=email,
                    subject=personalised_subject,
                    body=personalised_body,
                    attachments=attachments,
                    recipient_info=recipient,
                )
                results["sent"] += 1
            except Exception as e:
                results["failed"] += 1
                results["errors"].append({"email": email, "error": str(e)})

            # Small delay between sends to avoid rate limiting
            await asyncio.sleep(0.5)

        # Log the batch
        await Database.log_batch_send(
            subject=subject,
            total=len(recipients),
            sent=results["sent"],
            failed=results["failed"],
        )

        return results
