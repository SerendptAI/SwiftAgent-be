"""Credential-based authentication helpers: password hashing, OTP, email dispatch."""

import asyncio
import logging
import random
import smtplib
import string
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional
from uuid import uuid4

from passlib.context import CryptContext

from app.core.config import settings

logger = logging.getLogger(__name__)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "email_templates" / "otp_email.html"

_PURPOSE_LABELS = {
    "signup": "email verification",
    "login": "login verification",
    "password_reset": "password reset",
}


# --- password ---

def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


# --- OTP generation ---

def generate_otp(length: int = 6) -> str:
    """Return a random numeric OTP of the given length."""
    return "".join(random.choices(string.digits, k=length))


# --- OTP email ---

def _is_smtp_ready() -> bool:
    return bool(settings.ZOHO_EMAIL and settings.ZOHO_APP_PASSWORD and settings.ZOHO_SMTP_SERVER)


async def send_otp_email(to_email: str, otp_code: str, ttl_minutes: int, purpose: str = "signup") -> bool:
    """Send an OTP email via Zoho SMTP. Returns True on success."""
    if not _is_smtp_ready():
        logger.error("SMTP not configured — cannot send OTP to %s", to_email)
        return False

    purpose_label = _PURPOSE_LABELS.get(purpose, purpose.replace("_", " "))

    try:
        html = _TEMPLATE_PATH.read_text(encoding="utf-8")
        html = (
            html
            .replace("{{otp_code}}", otp_code)
            .replace("{{ttl_minutes}}", str(ttl_minutes))
            .replace("{{purpose_label}}", purpose_label)
            .replace("{{app_name}}", "Swift Agent")
        )
    except Exception:
        logger.exception("Failed to read OTP email template — using inline fallback")
        html = (
            f"<html><body>"
            f"<h3>Swift Agent verification code</h3>"
            f"<p>Use the code below to complete your {purpose_label}:</p>"
            f"<div style='font-size:28px;font-weight:bold;letter-spacing:6px;margin:16px 0'>{otp_code}</div>"
            f"<p>This code expires in <strong>{ttl_minutes} minutes</strong>.</p>"
            f"<p>If you did not request this, you can safely ignore this message.</p>"
            f"</body></html>"
        )

    msg = EmailMessage()
    msg["Subject"] = f"Swift Agent — your {purpose_label} code"
    msg["From"] = settings.ZOHO_EMAIL
    msg["To"] = to_email
    msg.set_content(f"Your Swift Agent {purpose_label} code is: {otp_code}. Expires in {ttl_minutes} minutes.")
    msg.add_alternative(html, subtype="html")

    def _send() -> bool:
        try:
            with smtplib.SMTP_SSL(settings.ZOHO_SMTP_SERVER, settings.ZOHO_SMTP_PORT) as smtp:
                smtp.login(settings.ZOHO_EMAIL, settings.ZOHO_APP_PASSWORD)
                smtp.send_message(msg)
            logger.info("OTP email (%s) sent to %s", purpose, to_email)
            return True
        except smtplib.SMTPAuthenticationError:
            logger.error("SMTP auth failed sending OTP to %s", to_email)
            return False
        except Exception:
            logger.exception("SMTP error sending OTP to %s", to_email)
            return False

    return await asyncio.to_thread(_send)


# --- user document builder ---

def build_new_credential_user(full_name: str, email: str, hashed_password: str, otp_code: str, ttl_minutes: int) -> dict:
    """Return a ready-to-insert user document for an email/password signup."""
    now = datetime.now(tz=timezone.utc)
    return {
        "user_id": str(uuid4()),
        "email": email,
        "name": full_name,
        "picture": None,
        "password": hashed_password,
        "is_verified": False,
        "otp_code": otp_code,
        "otp_expires": now + timedelta(minutes=ttl_minutes),
        "last_otp_login_at": None,
        "created_at": now,
        "updated_at": now,
    }


# --- OTP validation ---

def otp_is_valid(user: dict, otp_code: str) -> tuple[bool, str]:
    """Return (True, "") if OTP matches and is not expired, else (False, reason)."""
    stored = user.get("otp_code")
    if not stored or stored != otp_code:
        return False, "Invalid OTP code."

    expires: Optional[datetime] = user.get("otp_expires")
    if expires is None:
        return False, "OTP expiry missing."

    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    if datetime.now(tz=timezone.utc) > expires:
        return False, "OTP has expired. Please request a new one."

    return True, ""


def within_otp_grace_period(user: dict) -> bool:
    """Return True if the user completed OTP login within the configured grace window."""
    last: Optional[datetime] = user.get("last_otp_login_at")
    if not last:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(tz=timezone.utc) - last) < timedelta(days=settings.OTP_GRACE_PERIOD_DAYS)
