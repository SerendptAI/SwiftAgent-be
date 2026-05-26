"""
Auth router — /api/v1/auth

Supports two auth methods that share one user account per email address:

  1. Google OAuth (existing)            — /login, /callback
  2. Passwordless OTP                   — /otp/send, /otp/verify

Account linking:
  All users are keyed by a stable UUID ``user_id``.  Email is the dedup key.
  If a Google OAuth user and a passwordless user share the same address they
  resolve to the same document and can use either login method.
"""

import asyncio
import json
import base64
import logging
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status, UploadFile, File
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_database
from app.core.security import create_access_token, create_refresh_token, decode_refresh_token
from app.core.rate_limiter import rate_limit_auth
from app.models.auth_models import (
    LoginResponse,
    OTPSendRequest,
    OTPVerifyRequest,
    ReferralRequest,
    RefreshTokenRequest,
    UserProfileUpdate,
    UserNameUpdate,
    UserSecurityUpdate,
    RegistrationInterestRequest,
    RegistrationInterestResponse,
)
from app.services.credential_auth_service import (
    build_new_passwordless_user,
    generate_otp,
    otp_is_valid,
    send_otp_email,
    within_otp_grace_period,
)
from app.services.welcome_email_service import send_welcome_email
from app.services import registration_service, cloudinary_service
from fastapi.responses import HTMLResponse
router = APIRouter(tags=["Auth"])
logger = logging.getLogger(__name__)

if settings.is_development:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"

SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
REDIRECT_URI = f"{settings.API_BASE_URL}/api/v1/auth/callback"


def _build_client_config():
    return {
        "web": {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [REDIRECT_URI],
        }
    }


def _smtp_guard():
    """Raise 503 if SMTP is not configured."""
    if not (settings.ZOHO_EMAIL and settings.ZOHO_APP_PASSWORD and settings.ZOHO_SMTP_SERVER):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email service is not configured on the server.",
        )


def _token_pair(user_id: str) -> dict:
    return {
        "access_token": create_access_token(data={"sub": user_id}),
        "refresh_token": create_refresh_token(data={"sub": user_id}),
        "token_type": "bearer",
    }


async def _assert_email_approved(db, email: str):
    """
    Ensure a completely new email is allowed to sign up.
    Allowed if:
    1. Exists in pending_registrations with status 'approved'
    2. Has been invited to a company
    """
    # check registrations
    reg = await db.pending_registrations.find_one({"company_email": email})
    if reg and reg.get("status") == "approved":
        return

    # check if invited
    company_invite = await db.companies.find_one({
        "$or": [
            {"pending_invites.email": email},
            {"members.email": email}
        ]
    })
    if company_invite:
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN, 
        detail="Your email has not been approved for registration. Please fill out the registration form first."
    )


async def _upsert_google_user(db, google_id: str, email: str, name: str, picture: Optional[str]) -> dict:
    """Look up by email first to support account linking, then upsert."""
    now = datetime.now(tz=timezone.utc)
    existing = await db.users.find_one({"email": email})

    if existing:
        patch: dict = {"updated_at": now}
        if not existing.get("google_id"):
            patch["google_id"] = google_id
        await db.users.update_one({"email": email}, {"$set": patch})
        return {**existing, **patch}

    new_user = {
        "user_id": str(uuid4()),
        "email": email,
        "name": name,
        "picture": picture,
        "google_id": google_id,
        "is_verified": True,
        "otp_code": None,
        "otp_expires": None,
        "last_otp_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.users.insert_one(new_user)
    return new_user


# --- registration ---

@router.post("/register-interest", response_model=RegistrationInterestResponse)
async def register_interest(data: RegistrationInterestRequest):
    """Submit a form to express interest in creating a company."""
    return await registration_service.submit_registration(data)

@router.get("/registrations/approve/{token}", response_class=HTMLResponse)
async def confirm_registration_approval(token: str):
    """Admin endpoint to see the confirmation screen for approving a registration."""
    return await registration_service.render_approval_confirmation(token)

@router.post("/registrations/approve/{token}", response_class=HTMLResponse)
async def execute_registration_approval(token: str):
    """Execute the approval and send a welcome email."""
    return await registration_service.execute_approval(token)

@router.get("/registration-details")
async def get_registration_details(
    current_user: dict = Depends(get_current_user), db=Depends(get_database)
):
    """Fetch user's approved registration details to prefill onboarding."""
    email = current_user.get("email")
    if not email:
        raise HTTPException(status_code=400, detail="User email not found")
        
    reg = await db.pending_registrations.find_one({"company_email": email, "status": "approved"})
    if not reg:
        return {}
        
    return {
        "company_name": reg.get("company_name", ""),
        "company_description": reg.get("company_description", ""),
        "customer_size": reg.get("customer_size", ""),
    }



# --- Google OAuth ---

@router.get("/login")
async def login(redirect_url: Optional[str] = None):
    """Initiate the Google OAuth flow."""
    flow = Flow.from_client_config(_build_client_config(), scopes=SCOPES, redirect_uri=REDIRECT_URI)
    state_data = json.dumps({"redirect_url": redirect_url or ""})
    state = base64.urlsafe_b64encode(state_data.encode()).decode()
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    return RedirectResponse(authorization_url)


@router.get("/callback")
async def callback(request: Request, db=Depends(get_database)):
    """Handle the Google OAuth callback and issue JWT tokens."""
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    redirect_url = None
    state = request.query_params.get("state")
    if state:
        try:
            state_data = json.loads(base64.urlsafe_b64decode(state).decode())
            redirect_url = state_data.get("redirect_url") or None
        except Exception:
            pass

    flow = Flow.from_client_config(_build_client_config(), scopes=SCOPES, redirect_uri=REDIRECT_URI)
    flow.fetch_token(code=code)

    session = flow.authorized_session()
    user_info = session.get("https://www.googleapis.com/oauth2/v2/userinfo").json()

    google_id = user_info.get("id")
    email = user_info.get("email", "").lower()
    name = user_info.get("name", "")

    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Google sign-in failed. Could not retrieve your account information. Please try again.")

    is_new = not bool(await db.users.find_one({"email": email}))
    if is_new:
        await _assert_email_approved(db, email)
        
    user = await _upsert_google_user(db, google_id, email, name, user_info.get("picture"))

    if is_new:
        try:
            asyncio.create_task(send_welcome_email(email, name))
        except Exception as e:
            logger.warning(f"Failed to send welcome email to {email}: {e}")

    tokens = _token_pair(user["user_id"])

    if redirect_url:
        fragment = urllib.parse.urlencode(tokens)
        return RedirectResponse(url=f"{redirect_url}#{fragment}")

    safe_user = {k: v for k, v in user.items() if k not in ("_id", "otp_code")}
    return {**tokens, "user": safe_user}


# --- token management ---

@router.post("/refresh")
async def refresh_token(request: RefreshTokenRequest):
    """Exchange a valid refresh token for a new access token."""
    payload = decode_refresh_token(request.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token payload")
    return {"access_token": create_access_token(data={"sub": user_id}), "token_type": "bearer"}


# --- current user ---

@router.get("/me")
async def get_me(current_user: dict = Depends(get_current_user), db=Depends(get_database)):
    """Return the authenticated user's profile."""
    if "_id" in current_user:
        current_user["_id"] = str(current_user["_id"])
    # Strip all internal/sensitive fields before returning
    for field in ("otp_code", "otp_expires", "google_id"):
        current_user.pop(field, None)

    company = await db.companies.find_one(
        {
            "$or": [{"user_id": current_user["user_id"]}, {"members.user_id": current_user["user_id"]}],
            "setup_complete": True
        },
        {"_id": 0, "id": 1, "user_id": 1},
    )
    current_user["onboarding_completed"] = company is not None
    current_user["company_id"] = company["id"] if company else None
    current_user["is_admin"] = (company["user_id"] == current_user["user_id"]) if company else False
    return current_user


@router.patch("/me")
async def update_me(
    data: UserProfileUpdate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Update the authenticated user's profile fields (name, picture, personal_email, personal_phone)."""
    update_data = data.model_dump(exclude_none=True)
    if not update_data:
        return {"status": "success", "user": current_user}
    update_data["updated_at"] = datetime.now(tz=timezone.utc)
    updated = await db.users.find_one_and_update(
        {"user_id": current_user["user_id"]},
        {"$set": update_data},
        return_document=True,
    )
    if updated:
        updated.pop("_id", None)
        updated.pop("otp_code", None)
        updated.pop("otp_expires", None)
        updated.pop("google_id", None)
    return {"status": "success", "user": updated}


@router.patch("/me/name")
async def update_name(
    data: UserNameUpdate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Update the authenticated user's display name."""
    now = datetime.now(tz=timezone.utc)
    updated = await db.users.find_one_and_update(
        {"user_id": current_user["user_id"]},
        {"$set": {"name": data.name, "updated_at": now}},
        return_document=True,
    )
    picture = updated.get("picture") if updated else None
    return {"status": "success", "name": data.name, "picture": picture}


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

@router.patch("/me/pfp")
async def update_pfp(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Update the authenticated user's profile picture (pfp)."""
    user_id = current_user["user_id"]
    
    content_type = file.content_type
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: JPEG, PNG, WebP, GIF"
        )
        
    file.file.seek(0, 2)
    file_size = file.file.tell()
    file.file.seek(0)
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Max 5MB")
        
    try:
        picture_url = await cloudinary_service.upload_image(
            file, folder=f"pfps/{user_id}"
        )
    except Exception as e:
        logger.error(f"Profile picture upload failed for user {user_id}: {e}")
        raise HTTPException(status_code=502, detail="Profile picture upload failed. Please try a different image or try again later.")
        
    now = datetime.now(tz=timezone.utc)
    updated = await db.users.find_one_and_update(
        {"user_id": user_id},
        {"$set": {"picture": picture_url, "updated_at": now}},
        return_document=True,
    )
    name = updated.get("name") if updated else None
    return {"status": "success", "picture": picture_url, "name": name}


@router.patch("/me/security")
async def update_user_security(
    data: UserSecurityUpdate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Update user-level backup email and access code (admin only)."""
    user_id = current_user["user_id"]
    company = await db.companies.find_one(
        {"user_id": user_id, "setup_complete": True},
        {"_id": 0, "id": 1, "user_id": 1}
    )
    if not company:
        raise HTTPException(status_code=403, detail="Only company admins can set backup security credentials")
        
    update_data = data.model_dump(exclude_none=True)
    if not update_data:
        return {"status": "success"}
    
    update_data["updated_at"] = datetime.now(tz=timezone.utc)
    await db.users.update_one({"user_id": user_id}, {"$set": update_data})
    return {"status": "success"}


# --- unified passwordless flow ---

@router.post("/otp/send", response_model=LoginResponse)
async def send_otp(request: Request, body: OTPSendRequest, db=Depends(get_database)):
    """
    Unified entrypoint for passwordless login and signup.
    If the user exists and is within the grace period, returns tokens immediately.
    Otherwise sends an OTP. If the email is completely new, creates an unverified user record.
    """
    await rate_limit_auth(request)
    _smtp_guard()

    email = body.email.lower()
    now = datetime.now(tz=timezone.utc)
    user = await db.users.find_one({"$or": [{"email": email}, {"backup_email": email}]})
    is_new = getattr(body, "is_signup", False)

    if not user:
        # Check if they are allowed to register before proceeding
        await _assert_email_approved(db, email)
        
        # brand new user -> unverified document placeholder
        otp_code = generate_otp()
        ttl = settings.OTP_TTL_SIGNUP_MINUTES
        new_user = build_new_passwordless_user(body.full_name, email, otp_code, ttl)
        await db.users.insert_one(new_user)
        is_new = True
        user = new_user
    else:
        # existing user - always require OTP (grace period disabled for testing)
        otp_code = generate_otp()
        ttl = settings.OTP_TTL_LOGIN_MINUTES
        await db.users.update_one(
            {"user_id": user["user_id"]},
            {"$set": {"otp_code": otp_code, "otp_expires": now + timedelta(minutes=ttl), "updated_at": now}},
        )

    # Dispatch email
    purpose = "email verification" if is_new else "login verification"
    sent = await send_otp_email(email, otp_code, ttl, purpose_label=purpose)

    if not sent:
        # Rollback partial signups to not pollute the db with unverified garbage
        if is_new:
            await db.users.delete_one({"email": email, "is_verified": False})
        raise HTTPException(status_code=503, detail="We couldn't deliver the verification email. Please check the email address and try again.")

    return LoginResponse(
        message="A verification code has been sent to your email.",
        email=email,
        otp_required=True,
        is_new_user=is_new,
    )


@router.post("/otp/verify", response_model=LoginResponse)
async def verify_otp(request: Request, body: OTPVerifyRequest, db=Depends(get_database)):
    """Confirm the OTP and return a JWT token pair."""
    await rate_limit_auth(request)
    email = body.email.lower()
    user = await db.users.find_one({"$or": [{"email": email}, {"backup_email": email}]})

    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email.")

    ok, reason = otp_is_valid(user, body.otp_code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    was_new = not user.get("is_verified")
    
    now = datetime.now(tz=timezone.utc)
    await db.users.update_one(
        {"user_id": user["user_id"]},
        {
            "$set": {
                "is_verified": True, 
                "otp_code": None, 
                "otp_expires": None, 
                "last_otp_login_at": now, 
                "updated_at": now
            }
        },
    )

    if was_new and not user.get("google_id"):
        try:
            asyncio.create_task(send_welcome_email(email, user.get("name", "")))
        except Exception:
            pass

    tokens = _token_pair(user["user_id"])
    return LoginResponse(
        message="Verification successful. Welcome!",
        email=email,
        otp_required=False,
        is_new_user=was_new,
        **tokens,
    )
