import json
import base64
import logging
import os
import urllib.parse
from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from google_auth_oauthlib.flow import Flow
from app.core.config import settings
from app.core.database import get_database
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from app.core.auth import get_current_user
from app.models.auth_models import (
    RefreshTokenRequest,
    UserProfileUpdate,
    ReferralRequest,
)
import asyncio
from app.services.welcome_email_service import send_welcome_email

router = APIRouter(tags=["Auth"])
logger = logging.getLogger(__name__)

# only allow insecure transport in development
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


@router.post("/verify-referral")
async def verify_referral(request: ReferralRequest):
    """Verify if the provided referral code is valid."""
    if not settings.REFERRAL_CODE:
        # If no code is configured, accept any code or disable invite-only
        return {"status": "success", "message": "Invites are open"}

    if request.code != settings.REFERRAL_CODE:
        raise HTTPException(status_code=400, detail="Invalid referral code")

    return {"status": "success", "message": "Valid referral code"}


@router.get("/login")
async def login(redirect_url: Optional[str] = None):
    """Initiates the Google OAuth flow."""
    flow = Flow.from_client_config(_build_client_config(), scopes=SCOPES, redirect_uri=REDIRECT_URI)

    # encode redirect_url into oauth state so it survives the round-trip
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
    """Handles the callback from Google."""
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    # decode redirect_url from state
    redirect_url = None
    state = request.query_params.get("state")
    if state:
        try:
            state_data = json.loads(base64.urlsafe_b64decode(state).decode())
            redirect_url = state_data.get("redirect_url") or None
        except Exception:
            pass

    flow = Flow.from_client_config(_build_client_config(), scopes=SCOPES, redirect_uri=REDIRECT_URI)

    # exchange code for token
    flow.fetch_token(code=code)

    # get user info
    session = flow.authorized_session()
    user_info = session.get("https://www.googleapis.com/oauth2/v2/userinfo").json()

    user_id = user_info.get("id")
    email = user_info.get("email")
    name = user_info.get("name")

    if not user_id:
        raise HTTPException(status_code=400, detail="Failed to get user info")

    # upsert user in db
    user_data = {
        "user_id": user_id,
        "email": email,
        "name": name,
        "picture": user_info.get("picture"),
        "updated_at": datetime.now(tz=timezone.utc),
    }

    result = await db.users.update_one({"user_id": user_id}, {"$set": user_data}, upsert=True)

    # If the update performed an upsert, result.upserted_id will be set
    # — treat this as a new user signup and send the welcome email asynchronously.
    if getattr(result, "upserted_id", None):
        try:
            asyncio.create_task(send_welcome_email(email, name))
        except Exception:
            # don't fail the auth flow if email send scheduling fails
            pass

    # create jwt
    access_token = create_access_token(data={"sub": user_id})
    refresh_token = create_refresh_token(data={"sub": user_id})

    # redirect with tokens in URL fragment (not query params) to avoid
    # exposure in server logs, browser history, and referrer headers
    if redirect_url:
        fragment = urllib.parse.urlencode(
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "bearer",
            }
        )
        target = f"{redirect_url}#{fragment}"
        return RedirectResponse(url=target)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": user_data,
    }


@router.post("/refresh")
async def refresh_token(request: RefreshTokenRequest):
    """Refreshes the access token using a valid refresh token."""
    payload = decode_refresh_token(request.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    new_access_token = create_access_token(data={"sub": user_id})
    return {"access_token": new_access_token, "token_type": "bearer"}


@router.get("/me")
async def read_users_me(current_user: dict = Depends(get_current_user), db=Depends(get_database)):
    """Get current user details."""
    if "_id" in current_user:
        current_user["_id"] = str(current_user["_id"])

    # check if the user has completed company onboarding
    company = await db.companies.find_one(
        {"user_id": current_user["user_id"], "setup_complete": True},
        {"_id": 0, "id": 1},
    )
    current_user["onboarding_completed"] = company is not None
    current_user["company_id"] = company["id"] if company else None

    return current_user


@router.patch("/me")
async def update_user_profile(
    data: UserProfileUpdate,
    current_user: dict = Depends(get_current_user),
    db=Depends(get_database),
):
    """Update current user's profile details."""
    user_id = current_user["user_id"]

    update_data = data.model_dump(exclude_none=True)
    if not update_data:
        return {"status": "success"}

    update_data["updated_at"] = datetime.now(tz=timezone.utc)

    await db.users.update_one({"user_id": user_id}, {"$set": update_data})
    return {"status": "success"}
