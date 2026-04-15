import asyncio
import json
import base64
import logging
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from google_auth_oauthlib.flow import Flow

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_database
from app.core.security import create_access_token, create_refresh_token, decode_refresh_token
from app.models.auth_models import (
    CredentialLoginRequest,
    CredentialLoginResponse,
    ForgotPasswordInitRequest,
    ForgotPasswordResetRequest,
    ForgotPasswordVerifyRequest,
    OTPResendRequest,
    OTPVerifyRequest,
    ReferralRequest,
    RefreshTokenRequest,
    SignupRequest,
    SignupResponse,
    UserProfileUpdate,
)
from app.services.credential_auth_service import (
    build_new_credential_user,
    generate_otp,
    hash_password,
    otp_is_valid,
    send_otp_email,
    verify_password,
    within_otp_grace_period,
)
from app.services.welcome_email_service import send_welcome_email

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
        "password": None,
        "otp_code": None,
        "otp_expires": None,
        "last_otp_login_at": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.users.insert_one(new_user)
    return new_user


# --- referral ---

@router.post("/verify-referral")
async def verify_referral(request: ReferralRequest):
    """Verify if the provided referral code is valid."""
    if not settings.REFERRAL_CODE:
        return {"status": "success", "message": "Invites are open"}
    if request.code != settings.REFERRAL_CODE:
        raise HTTPException(status_code=400, detail="Invalid referral code")
    return {"status": "success", "message": "Valid referral code"}


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
        raise HTTPException(status_code=400, detail="Failed to retrieve user info from Google")

    is_new = not bool(await db.users.find_one({"email": email}))
    user = await _upsert_google_user(db, google_id, email, name, user_info.get("picture"))

    if is_new:
        try:
            asyncio.create_task(send_welcome_email(email, name))
        except Exception:
            pass

    tokens = _token_pair(user["user_id"])

    if redirect_url:
        fragment = urllib.parse.urlencode(tokens)
        return RedirectResponse(url=f"{redirect_url}#{fragment}")

    safe_user = {k: v for k, v in user.items() if k not in ("_id", "password", "otp_code")}
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
    current_user.pop("password", None)
    current_user.pop("otp_code", None)

    company = await db.companies.find_one(
        {"user_id": current_user["user_id"], "setup_complete": True},
        {"_id": 0, "id": 1},
    )
    current_user["onboarding_completed"] = company is not None
    current_user["company_id"] = company["id"] if company else None
    return current_user


@router.patch("/me")
async def update_me(
    data: UserProfileUpdate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Update the authenticated user's profile."""
    update_data = data.model_dump(exclude_none=True)
    if not update_data:
        return {"status": "success"}
    update_data["updated_at"] = datetime.now(tz=timezone.utc)
    await db.users.update_one({"user_id": current_user["user_id"]}, {"$set": update_data})
    return {"status": "success"}


# --- signup ---

@router.post("/signup", response_model=SignupResponse, status_code=status.HTTP_201_CREATED)
async def signup(body: SignupRequest, db=Depends(get_database)):
    """Register a new account with email and password, sending an OTP for verification."""
    _smtp_guard()

    if body.password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    email = body.email.lower()
    existing = await db.users.find_one({"email": email})

    if existing:
        if existing.get("is_verified") and existing.get("password"):
            raise HTTPException(
                status_code=409,
                detail="An account with this email already exists. Please sign in.",
            )

        # Google-only user adding a password — link credentials, re-send OTP to verify
        if existing.get("is_verified") and existing.get("google_id") and not existing.get("password"):
            otp_code = generate_otp()
            ttl = settings.OTP_TTL_SIGNUP_MINUTES
            await db.users.update_one(
                {"email": email},
                {
                    "$set": {
                        "password": hash_password(body.password),
                        "otp_code": otp_code,
                        "otp_expires": datetime.now(tz=timezone.utc) + timedelta(minutes=ttl),
                        "updated_at": datetime.now(tz=timezone.utc),
                    }
                },
            )
            sent = await send_otp_email(email, otp_code, ttl, purpose="signup")
            if not sent:
                raise HTTPException(status_code=503, detail="Failed to send verification email.")
            return SignupResponse(
                message="Verification code sent. Please verify your email to enable password login.",
                email=email,
            )

        if not existing.get("is_verified"):
            raise HTTPException(
                status_code=409,
                detail="An unverified account already exists. Use /auth/otp/resend to get a new code.",
            )

    otp_code = generate_otp()
    ttl = settings.OTP_TTL_SIGNUP_MINUTES
    new_user = build_new_credential_user(body.full_name, email, hash_password(body.password), otp_code, ttl)
    await db.users.insert_one(new_user)

    sent = await send_otp_email(email, otp_code, ttl, purpose="signup")
    if not sent:
        await db.users.delete_one({"email": email, "is_verified": False})
        raise HTTPException(status_code=503, detail="Failed to send verification email. Please try again.")

    return SignupResponse(
        message="Account created. Check your email for a verification code.",
        email=email,
    )


@router.post("/signup/verify", response_model=CredentialLoginResponse)
async def verify_signup(body: OTPVerifyRequest, db=Depends(get_database)):
    """Confirm signup OTP, mark account verified, and return a JWT token pair."""
    email = body.email.lower()
    user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email.")
    if user.get("is_verified") and not user.get("otp_code"):
        raise HTTPException(status_code=400, detail="Account is already verified. Please sign in.")

    ok, reason = otp_is_valid(user, body.otp_code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    now = datetime.now(tz=timezone.utc)
    await db.users.update_one(
        {"email": email},
        {"$set": {"is_verified": True, "otp_code": None, "otp_expires": None, "updated_at": now}},
    )

    # welcome email for brand-new credential users (fire-and-forget)
    if not user.get("google_id"):
        try:
            asyncio.create_task(send_welcome_email(email, user.get("name", "")))
        except Exception:
            pass

    tokens = _token_pair(user["user_id"])
    return CredentialLoginResponse(
        message="Email verified. Welcome to Swift Agent!",
        email=email,
        otp_required=False,
        **tokens,
    )


# --- signin ---

@router.post("/signin", response_model=CredentialLoginResponse)
async def signin(body: CredentialLoginRequest, db=Depends(get_database)):
    """Sign in with email and password. Returns tokens directly if within grace period, otherwise sends an OTP."""
    email = body.email.lower()
    user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    if not user.get("is_verified"):
        raise HTTPException(
            status_code=403,
            detail="Account not verified. Please complete signup verification first.",
        )
    if not user.get("password"):
        raise HTTPException(
            status_code=400,
            detail="This account uses Google Sign-In. Please use the Google login option.",
        )
    if not verify_password(body.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")

    # skip OTP if user recently completed OTP verification
    if within_otp_grace_period(user):
        tokens = _token_pair(user["user_id"])
        return CredentialLoginResponse(
            message="Welcome back!",
            email=email,
            otp_required=False,
            **tokens,
        )

    _smtp_guard()
    otp_code = generate_otp()
    ttl = settings.OTP_TTL_LOGIN_MINUTES
    now = datetime.now(tz=timezone.utc)
    await db.users.update_one(
        {"email": email},
        {"$set": {"otp_code": otp_code, "otp_expires": now + timedelta(minutes=ttl), "updated_at": now}},
    )

    sent = await send_otp_email(email, otp_code, ttl, purpose="login")
    if not sent:
        raise HTTPException(status_code=503, detail="Failed to send login verification email.")

    return CredentialLoginResponse(
        message="A verification code has been sent to your email.",
        email=email,
        otp_required=True,
    )


@router.post("/signin/verify", response_model=CredentialLoginResponse)
async def verify_signin(body: OTPVerifyRequest, db=Depends(get_database)):
    """Confirm the login OTP and return a JWT token pair."""
    email = body.email.lower()
    user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email.")

    ok, reason = otp_is_valid(user, body.otp_code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    now = datetime.now(tz=timezone.utc)
    await db.users.update_one(
        {"email": email},
        {"$set": {"otp_code": None, "otp_expires": None, "last_otp_login_at": now, "updated_at": now}},
    )

    tokens = _token_pair(user["user_id"])
    return CredentialLoginResponse(
        message="Sign-in successful.",
        email=email,
        otp_required=False,
        **tokens,
    )


# --- OTP resend ---

@router.post("/otp/resend")
async def resend_otp(body: OTPResendRequest, db=Depends(get_database)):
    """Resend an OTP for signup, login, or password_reset."""
    _smtp_guard()

    email = body.email.lower()
    user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email.")

    if body.purpose == "signup":
        if user.get("is_verified"):
            raise HTTPException(status_code=400, detail="Account is already verified.")
        ttl = settings.OTP_TTL_SIGNUP_MINUTES
    else:
        if not user.get("is_verified"):
            raise HTTPException(status_code=403, detail="Account is not verified yet.")
        ttl = settings.OTP_TTL_LOGIN_MINUTES

    otp_code = generate_otp()
    now = datetime.now(tz=timezone.utc)
    await db.users.update_one(
        {"email": email},
        {"$set": {"otp_code": otp_code, "otp_expires": now + timedelta(minutes=ttl), "updated_at": now}},
    )

    sent = await send_otp_email(email, otp_code, ttl, purpose=body.purpose)
    if not sent:
        raise HTTPException(status_code=503, detail="Failed to send verification email.")

    label = body.purpose.replace("_", " ")
    return {"message": f"A new {label} code has been sent to your email."}


# --- forgot password (send → verify → reset) ---

@router.post("/password/forgot")
async def forgot_password(body: ForgotPasswordInitRequest, db=Depends(get_database)):
    """Send a password-reset OTP. Always returns 200 to prevent email enumeration."""
    _smtp_guard()

    email = body.email.lower()
    user = await db.users.find_one({"email": email})

    # always return the same message to prevent user-enumeration
    neutral = {"message": "If that email is registered you will receive a reset code shortly."}

    if not user or not user.get("is_verified"):
        return neutral

    otp_code = generate_otp()
    ttl = settings.OTP_TTL_LOGIN_MINUTES
    now = datetime.now(tz=timezone.utc)
    await db.users.update_one(
        {"email": email},
        {"$set": {"otp_code": otp_code, "otp_expires": now + timedelta(minutes=ttl), "updated_at": now}},
    )
    await send_otp_email(email, otp_code, ttl, purpose="password_reset")
    return neutral


@router.post("/password/verify")
async def verify_password_otp(body: ForgotPasswordVerifyRequest, db=Depends(get_database)):
    """Verify the password-reset OTP without resetting yet."""
    email = body.email.lower()
    user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email.")

    ok, reason = otp_is_valid(user, body.otp_code)
    if not ok:
        raise HTTPException(status_code=400, detail=reason)

    return {"message": "Code verified. You may now reset your password."}


@router.post("/password/reset")
async def reset_password(body: ForgotPasswordResetRequest, db=Depends(get_database)):
    """Set a new password using the verified reset OTP."""
    if body.new_password != body.confirm_new_password:
        raise HTTPException(status_code=400, detail="Passwords do not match.")

    email = body.email.lower()
    user = await db.users.find_one({"email": email})

    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email.")

    # re-validate the OTP is still in the DB and not expired
    ok, reason = otp_is_valid(user, user.get("otp_code", ""))
    if not ok:
        raise HTTPException(status_code=400, detail=f"Reset session expired: {reason}")

    now = datetime.now(tz=timezone.utc)
    await db.users.update_one(
        {"email": email},
        {
            "$set": {
                "password": hash_password(body.new_password),
                "otp_code": None,
                "otp_expires": None,
                "updated_at": now,
            }
        },
    )
    return {"message": "Password reset successfully. You can now sign in with your new password."}
