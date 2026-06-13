import asyncio
import logging
import smtplib
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi.responses import HTMLResponse
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import get_database
from app.models.auth_models import RegistrationInterestRequest
from app.services.email_utils import add_html_with_inline_images
from app.services.welcome_email_service import send_welcome_email

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "email_templates"
NOTIFICATION_TEMPLATE = TEMPLATES_DIR / "approve-company.html"
CONFIRM_TEMPLATE = TEMPLATES_DIR / "admin_registration_confirm.html"
APPROVED_TEMPLATE = TEMPLATES_DIR / "admin_registration_approved.html"


def _load_template(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    return html.replace("{{base_url}}", settings.API_BASE_URL)


def _get_admin_email() -> str:
    """Return a comma-separated list of registration notification recipients.

    Override via the ADMIN_NOTIFICATION_EMAIL env var (comma-separated for
    multiple recipients); otherwise fall back to the default list below.
    """
    default = "thelma@swiftagents.org,romeobourne211@gmail.com"
    raw = getattr(settings, "ADMIN_NOTIFICATION_EMAIL", None) or default
    recipients = [addr.strip() for addr in raw.split(",") if addr.strip()]
    return ", ".join(recipients)


from fastapi import HTTPException

async def submit_registration(data: RegistrationInterestRequest) -> dict:
    """Save registration to DB and send notification email to admin."""
    db = await get_database()
    now = datetime.now(tz=timezone.utc)
    token = str(uuid.uuid4())
    
    email = data.company_email.lower()
    
    # Gate 1: check if they already have a pending/approved registration
    existing_reg = await db.pending_registrations.find_one({"company_email": email})
    if existing_reg:
        if existing_reg.get("status") == "approved":
            raise HTTPException(
                status_code=409,
                detail="Your registration has already been approved. Please log in to continue."
            )
        raise HTTPException(
            status_code=409,
            detail="You have already registered. Your application is awaiting approval."
        )
        
    # Gate 2: check if they already have a user account with a company
    existing_user = await db.users.find_one({"email": email})
    if existing_user:
        user_id = existing_user.get("user_id")
        # Check if they own a company
        owned_company = await db.companies.find_one({"user_id": user_id})
        if owned_company:
            raise HTTPException(
                status_code=409,
                detail="You already have a registered company. Please log in to access your account."
            )
        # Check if they are a member of a company
        member_company = await db.companies.find_one({"members.user_id": user_id})
        if member_company:
            raise HTTPException(
                status_code=409,
                detail="You are already a member of a company. Please log in to access your account."
            )
            
    doc = {
        "company_name": data.company_name,
        "company_email": email,
        "company_description": data.company_description,
        "customer_size": data.customer_size,
        "status": "pending",
        "token": token,
        "created_at": now,
        "updated_at": now
    }
    
    try:
        await db.pending_registrations.insert_one(doc)
    except DuplicateKeyError:
        pass # Should be caught by the pre-checks above, but safe to ignore if it happens
        
    # Dispatch email
    asyncio.create_task(_send_notification_email(doc, token))
    return {"status": "success", "message": "Registration submitted for approval"}


async def _send_notification_email(doc: dict, token: str):
    try:
        api_base = getattr(settings, "API_BASE_URL", "http://localhost:8000")
        approval_url = f"{api_base}/api/v1/auth/registrations/approve/{token}"
        
        html = _load_template(NOTIFICATION_TEMPLATE)
        html = html.replace("{{company_name}}", doc.get("company_name", ""))
        html = html.replace("{{company_email}}", doc.get("company_email", ""))
        html = html.replace("{{customer_size}}", doc.get("customer_size", ""))
        company_description = doc.get("company_description", "").replace("\n", "<br>")
        html = html.replace("{{company_description}}", company_description)
        html = html.replace("{{approval_url}}", approval_url)
        
        admin_email = _get_admin_email()
        subject = f"New SwiftAgent Registration: {doc.get('company_name')}"
        
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = settings.active_sender_email
        msg["To"] = admin_email
        msg.set_content(f"New registration request from {doc.get('company_name')}. View in HTML client.")
        add_html_with_inline_images(msg, html)

        def _send() -> None:
            with smtplib.SMTP_SSL(settings.active_smtp_server, settings.active_smtp_port) as smtp:
                smtp.login(settings.active_smtp_username, settings.active_smtp_password)
                smtp.send_message(msg)
                
        await asyncio.to_thread(_send)
        logger.info("Sent registration notification to %s for %s", admin_email, doc.get("company_email"))
    except Exception as e:
        logger.exception("Failed to send admin registration notification for %s: %s", doc.get("company_email"), e)


async def render_approval_confirmation(token: str) -> HTMLResponse:
    """Returns HTML for the admin to confirm they want to approve."""
    db = await get_database()
    reg = await db.pending_registrations.find_one({"token": token})
    
    if not reg:
        return HTMLResponse("<h1>Invalid or Expired Link</h1>", status_code=404)
        
    if reg.get("status") == "approved":
        return HTMLResponse("<h1>Already Approved</h1><p>This registration has already been approved.</p>", status_code=200)

    html = _load_template(CONFIRM_TEMPLATE)
    html = html.replace("{{company_name}}", reg.get('company_name', ''))
    html = html.replace("{{company_email}}", reg.get('company_email', ''))
    html = html.replace("{{token}}", token)
    
    return HTMLResponse(content=html, status_code=200)


async def execute_approval(token: str) -> HTMLResponse:
    """Executes the approval and triggers welcome email."""
    db = await get_database()
    reg = await db.pending_registrations.find_one({"token": token})
    
    if not reg:
        return HTMLResponse("<h1>Invalid or Expired Link</h1>", status_code=404)
        
    if reg.get("status") == "approved":
        return HTMLResponse("<h1>Already Approved</h1>", status_code=200)

    now = datetime.now(tz=timezone.utc)
    await db.pending_registrations.update_one(
        {"token": token},
        {"$set": {"status": "approved", "updated_at": now}}
    )
    
    # Trigger welcome email to user
    try:
        asyncio.create_task(send_welcome_email(reg["company_email"], reg["company_name"]))
    except Exception as e:
        logger.error(f"Failed to queue welcome email for {reg['company_email']}: {e}")

    html = _load_template(APPROVED_TEMPLATE)
    html = html.replace("{{company_email}}", reg.get('company_email', ''))
    
    return HTMLResponse(content=html, status_code=200)
