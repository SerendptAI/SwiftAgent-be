"""
Raise App — Bulk Email Dashboard for VC Outreach
=================================================
A modern FastAPI + Jinja2 server-rendered dashboard for managing
personalised bulk email campaigns to venture capital contacts.
"""

import logging
import secrets
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Depends, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from config import config
from vc_loader import VCLoader
from email_service import EmailService
from db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    await Database.connect()
    logger.info("Raise App started — connected to MongoDB")
    yield
    await Database.close()
    logger.info("Raise App shutdown")


app = FastAPI(title="Raise — VC Email Dashboard", lifespan=lifespan)

# Session middleware for auth
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)

# Static files & templates
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# AUTH HELPERS 
def get_current_user(request: Request):
    """Check session for authentication."""
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return True


# AUTH ROUTES 
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": None}
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form(...)):
    if password == config.RAISE_APP_PASSWORD:
        request.session["authenticated"] = True
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"error": "Invalid password"}
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# DASHBOARD
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)

    loader = VCLoader()
    sheets = loader.get_sheet_names()
    total_contacts = 0
    contacts_with_email = 0
    sheet_stats = []

    for sheet in sheets:
        contacts = loader.get_contacts(sheet)
        total = len(contacts)
        with_email = sum(1 for c in contacts if c.get("Email"))
        total_contacts += total
        contacts_with_email += with_email
        sheet_stats.append(
            {"name": sheet, "total": total, "with_email": with_email}
        )

    # Get email stats from DB
    stats = await Database.get_email_stats()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "active": "dashboard",
            "total_contacts": total_contacts,
            "contacts_with_email": contacts_with_email,
            "total_sheets": len(sheets),
            "sheet_stats": sheet_stats,
            "emails_sent": stats.get("total_sent", 0),
            "emails_failed": stats.get("total_failed", 0),
            "recent_sends": stats.get("recent_sends", []),
        },
    )


# CONTACTS / VC VIEW
@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request, sheet: str = None, page: int = 1, per_page: int = 25):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)

    loader = VCLoader()
    sheets = loader.get_sheet_names()

    if not sheet:
        sheet = sheets[0] if sheets else None

    contacts = loader.get_contacts(sheet) if sheet else []

    # Enrich with email send status from DB
    enriched = await Database.enrich_contacts_with_status(contacts)

    # Pagination
    total = len(enriched)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_contacts = enriched[start : start + per_page]

    return templates.TemplateResponse(
        request=request,
        name="contacts.html",
        context={
            "active": "contacts",
            "sheets": sheets,
            "current_sheet": sheet,
            "contacts": page_contacts,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "per_page": per_page,
        },
    )


# COMPOSE EMAIL
@app.get("/compose", response_class=HTMLResponse)
async def compose_page(request: Request, sheet: str = None):
    if not request.session.get("authenticated"):
        return RedirectResponse(url="/login", status_code=303)

    loader = VCLoader()
    sheets = loader.get_sheet_names()

    recipients = []
    if sheet:
        contacts = loader.get_contacts(sheet)
        recipients = [c for c in contacts if c.get("Email")]

    return templates.TemplateResponse(
        request=request,
        name="compose.html",
        context={
            "active": "compose",
            "sheets": sheets,
            "current_sheet": sheet,
            "recipients": recipients,
            "from_email": config.SMTP_EMAIL,
        },
    )


# API: GET RECIPIENTS FOR SHEET
@app.get("/api/recipients")
async def api_get_recipients(request: Request, sheet: str):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401)

    loader = VCLoader()
    contacts = loader.get_contacts(sheet)
    recipients = [c for c in contacts if c.get("Email")]
    return {"recipients": recipients, "total": len(recipients)}


# API: PREVIEW EMAIL
@app.post("/api/preview")
async def api_preview_email(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401)

    data = await request.json()
    subject = data.get("subject", "")
    body = data.get("body", "")
    recipient = data.get("recipient", {})

    # Apply personalisation
    personalised_subject = EmailService.personalise(subject, recipient)
    personalised_body = EmailService.personalise(body, recipient)

    return {
        "subject": personalised_subject,
        "body": personalised_body,
    }


# API: SEND EMAILS
@app.post("/api/send")
async def api_send_emails(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401)

    form = await request.form()
    subject = form.get("subject", "")
    body = form.get("body", "")
    sheet = form.get("sheet", "")
    selected_emails_raw = form.get("selected_emails", "")

    # Handle attachments
    attachments = []
    for key in form:
        if key.startswith("attachments"):
            file = form[key]
            if hasattr(file, "read"):
                content = await file.read()
                attachments.append(
                    {
                        "filename": file.filename,
                        "content": content,
                        "content_type": file.content_type or "application/octet-stream",
                    }
                )

    # Determine recipients
    loader = VCLoader()
    if selected_emails_raw:
        selected_list = [e.strip() for e in selected_emails_raw.split(",") if e.strip()]
        contacts = loader.get_contacts(sheet)
        recipients = [c for c in contacts if c.get("Email") in selected_list]
    else:
        contacts = loader.get_contacts(sheet)
        recipients = [c for c in contacts if c.get("Email")]

    if not recipients:
        return JSONResponse(
            {"success": False, "error": "No recipients with email addresses found"},
            status_code=400,
        )

    # Send emails in background
    results = await EmailService.send_bulk(
        subject=subject,
        body=body,
        recipients=recipients,
        attachments=attachments,
    )

    return JSONResponse(
        {
            "success": True,
            "total": len(recipients),
            "sent": results["sent"],
            "failed": results["failed"],
            "errors": results["errors"][:10],  # limit error details
        }
    )


# API: SEND SINGLE TEST EMAIL
@app.post("/api/send-test")
async def api_send_test(request: Request):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401)

    data = await request.json()
    subject = data.get("subject", "")
    body = data.get("body", "")
    test_email = data.get("test_email", "")
    recipient = data.get("recipient", {})

    if not test_email:
        return JSONResponse({"success": False, "error": "No test email provided"}, status_code=400)

    personalised_subject = EmailService.personalise(subject, recipient)
    personalised_body = EmailService.personalise(body, recipient)

    try:
        await EmailService.send_single(
            to_email=test_email,
            subject=personalised_subject,
            body=personalised_body,
            attachments=[],
        )
        return {"success": True, "message": f"Test email sent to {test_email}"}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# API: EMAIL HISTORY
@app.get("/api/history")
async def api_email_history(request: Request, page: int = 1, per_page: int = 50):
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401)

    history = await Database.get_email_history(page=page, per_page=per_page)
    return history


# HEALTH
@app.get("/health")
async def health():
    return {"status": "ok", "service": "raise-app"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=config.APP_HOST,
        port=config.APP_PORT,
        reload=config.APP_ENV != "production",
    )
