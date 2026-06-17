import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
import urllib.parse
from typing import List
from app.core.auth import get_current_user
from app.core.config import settings
from app.models.company_models import (
    CompanyInfoCreate,
    CompanyInfoUpdate,
    CompanySecurityUpdate,
    CompanyIdentityUpdate,
    CompanyTypeUpdate,
    CompanyResponse,
    CompanySummary,
    MemberInviteCreate,
    AcceptInviteRequest,
    CompanyMemberResponse,
)
from app.models.email_models import (
    EmailSlugCheck,
    EmailSlugCheckResponse,
    EmailSlugUpdate,
)
from app.services import company_service, cloudinary_service
from app.core.plan_enforcement import enforce_company_limit, enforce_member_limit
from fastapi import UploadFile, File, Form

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Companies"])


@router.post("/", response_model=CompanyResponse, status_code=201)
async def create_company(
    data: CompanyInfoCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a new company (onboarding step 1)."""
    user_id = current_user["user_id"]
    
    existing_companies = await company_service.list_companies(user_id)
    if existing_companies:
        for c in existing_companies:
            if c.get("user_id") != user_id:
                raise HTTPException(status_code=400, detail="Members are not allowed to create companies")

    # Plan enforcement: check companies_per_user
    await enforce_company_limit(user_id)

    company = await company_service.create_company(user_id, data.model_dump())
    return company


@router.get("/", response_model=List[CompanySummary])
async def list_companies(
    current_user: dict = Depends(get_current_user),
):
    """List all companies for the current user."""
    user_id = current_user["user_id"]
    return await company_service.list_companies(user_id)


@router.get("/email-slug/check", response_model=EmailSlugCheckResponse)
async def check_global_email_slug(
    slug: str = Query(..., min_length=3, max_length=30),
    current_user: dict = Depends(get_current_user),
):
    """Check if an email slug is available globally (e.g. before company creation)."""
    slug = slug.strip().lower()
    available = await company_service.check_slug_availability(slug)

    suggestion = None
    if not available:
        suggestion = await company_service.suggest_slug(slug)

    return {"available": available, "suggestion": suggestion}


@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company(
    company_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get full company details."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id, admin_only=False)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found. Please check the company ID or ensure you have access.")
    return company


@router.get("/{company_id}/public")
async def get_company_public(
    company_id: str,
):
    """Get public company details."""
    company = await company_service.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    name = company.get("name")
    website = company.get("website")
    logo_url = company.get("logo_url")
    suggested_ai_prompts = company.get("suggested_ai_prompts", [])
    enable_suggested_prompts = company.get("enable_suggested_prompts", True)
    return {
        "name": name, 
        "website": website, 
        "logo_url": logo_url,
        "suggested_ai_prompts": suggested_ai_prompts,
        "enable_suggested_prompts": enable_suggested_prompts
    }


@router.patch("/{company_id}/identity", response_model=CompanyResponse)
async def update_identity(
    company_id: str,
    data: CompanyIdentityUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update company identity (onboarding step 2)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id, admin_only=False)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await company_service.update_identity(
        company_id, user_id, data.model_dump(exclude_none=True)
    )


@router.patch("/{company_id}/info", response_model=CompanyResponse)
async def update_company_info(
    company_id: str,
    data: CompanyInfoUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update general company information (Settings page)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id, admin_only=False)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await company_service.update_company_info(
        company_id, user_id, data.model_dump(exclude_none=True)
    )


@router.patch("/{company_id}/security", response_model=CompanyResponse)
async def update_security(
    company_id: str,
    data: CompanySecurityUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update settings for security, like backup email and access code."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id, admin_only=True)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or unauthorized")
    return await company_service.update_security(
        company_id, user_id, data.model_dump(exclude_none=True)
    )


@router.patch("/{company_id}/type", response_model=CompanyResponse)
async def update_company_type(
    company_id: str,
    data: CompanyTypeUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Set company type — saas_finance or crypto (onboarding step 3 - final)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id, admin_only=True)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or unauthorized")
    return await company_service.update_company_type(
        company_id, user_id, data.company_type
    )





ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB


@router.patch("/{company_id}/logo", response_model=CompanyResponse)
async def update_logo(
    company_id: str,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Update company logo URL (uploads to Cloudinary)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Validate file type
    content_type = file.content_type
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail="Invalid file type. Allowed: JPEG, PNG, WebP, GIF"
        )

    # Validate file size
    file.file.seek(0, 2)  # Seek to end
    file_size = file.file.tell()
    file.file.seek(0)  # Reset to start
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Max 5MB")

    try:
        logo_url = await cloudinary_service.upload_image(
            file, folder=f"logos/{company_id}"
        )
    except Exception as e:
        logger.error(f"Logo upload failed for company {company_id}: {e}")
        raise HTTPException(status_code=502, detail="Logo upload failed. Please try a different image or try again later.")
    return await company_service.update_logo(company_id, user_id, logo_url)


@router.get("/{company_id}/email-slug/check", response_model=EmailSlugCheckResponse)
async def check_email_slug(
    company_id: str,
    slug: str = Query(..., min_length=3, max_length=30),
    current_user: dict = Depends(get_current_user),
):
    """Check if an email slug is available."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    slug = slug.strip().lower()
    available = await company_service.check_slug_availability(slug)

    suggestion = None
    if not available:
        # check if this company already owns this slug
        if company.get("email_slug") == slug:
            return {"available": True}
        suggestion = await company_service.suggest_slug(company.get("name", slug))

    return {"available": available, "suggestion": suggestion}


@router.patch("/{company_id}/email-slug", response_model=CompanyResponse)
async def update_email_slug(
    company_id: str,
    data: EmailSlugUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Set or update the company's email slug."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    slug = data.email_slug

    # check availability (allow if company already owns it)
    if company.get("email_slug") != slug:
        available = await company_service.check_slug_availability(slug)
        if not available:
            raise HTTPException(
                status_code=409,
                detail="Sorry, this email slug is already in use. Pick another.",
            )

    return await company_service.update_email_slug(company_id, user_id, slug)

@router.post("/{company_id}/invites", response_model=dict)
async def invite_member(
    company_id: str,
    data: MemberInviteCreate,
    current_user: dict = Depends(get_current_user),
):
    """Invite a new member to the company (Admin only)."""
    user_id = current_user["user_id"]

    # Plan enforcement: check members_per_company
    company = await company_service.get_company(company_id, user_id, admin_only=True)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found or unauthorized")
    await enforce_member_limit(company)

    try:
        invite = await company_service.create_invite(company_id, user_id, data.email)
        return {"status": "success", "message": "Invite sent", "invite": invite}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{company_id}/invites/resend", response_model=dict)
async def resend_member_invite(
    company_id: str,
    data: MemberInviteCreate,
    current_user: dict = Depends(get_current_user),
):
    """Resend a pending invite to a member, even if expired (Admin only)."""
    user_id = current_user["user_id"]

    try:
        invite = await company_service.resend_invite(company_id, user_id, data.email)
        return {"status": "success", "message": "Invite resent successfully", "invite": invite}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/invites/accept", response_model=dict)
async def accept_invite_post(
    data: AcceptInviteRequest,
):
    """Accept an invite using a token via frontend POST."""
    try:
        result = await company_service.accept_invite(data.token)
        return {"status": "success", "message": "Invite accepted. You can now log in.", "data": result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/invites/accept")
async def accept_invite_get(token: str):
    """Accept an invite via email link click (GET) and redirect to frontend."""
    frontend_url = getattr(settings, "FRONTEND_URL", "https://swiftagents.org").rstrip("/")
    try:
        await company_service.accept_invite(token)
        query = urllib.parse.urlencode({"invite_status": "success", "message": "Invite accepted. You can now log in."})
        return RedirectResponse(url=f"{frontend_url}?{query}")
    except ValueError as e:
        query = urllib.parse.urlencode({"invite_status": "error", "message": str(e)})
        return RedirectResponse(url=f"{frontend_url}?{query}")

@router.get("/{company_id}/members", response_model=List[CompanyMemberResponse])
async def list_members(
    company_id: str,
    current_user: dict = Depends(get_current_user),
):
    """List all active members and pending invites for the company."""
    user_id = current_user["user_id"]
    try:
        members = await company_service.get_unified_members(company_id, user_id)
        return members
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

@router.delete("/{company_id}/members/{email}")
async def remove_member_or_invite(
    company_id: str,
    email: str,
    current_user: dict = Depends(get_current_user),
):
    """Remove a member or revoke an invite."""
    user_id = current_user["user_id"]
    try:
        await company_service.remove_member_or_invite(company_id, user_id, email)
        return {"status": "success", "message": "User removed from company"}
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))

# ── API Key Management ──────────────────────────────────────────────────

from app.services import api_key_service
from app.models.sdk_models import ApiKeyCreateRequest, ApiKeyCreateResponse, ApiKeyListItem
from typing import List

@router.post("/{company_id}/api-keys", response_model=ApiKeyCreateResponse)
async def create_api_key(
    company_id: str,
    req: ApiKeyCreateRequest,
    current_user: dict = Depends(get_current_user),
):
    """Generate a new SDK API key for the company. Raw key is returned ONLY once."""
    company = await company_service.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if company.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Only the company admin can manage API keys.")

    return await api_key_service.create_api_key(company_id, label=req.label)


@router.get("/{company_id}/api-keys", response_model=List[ApiKeyListItem])
async def list_api_keys(
    company_id: str,
    current_user: dict = Depends(get_current_user),
):
    """List all SDK API keys for the company (returns prefixes only, never raw keys)."""
    company = await company_service.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if company.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Only the company admin can manage API keys.")

    return await api_key_service.list_api_keys(company_id)


@router.delete("/{company_id}/api-keys/{key_id}")
async def revoke_api_key(
    company_id: str,
    key_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Revoke an active API key."""
    company = await company_service.get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if company.get("user_id") != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Only the company admin can manage API keys.")

    success = await api_key_service.revoke_api_key(company_id, key_id)
    if not success:
        raise HTTPException(status_code=404, detail="Key not found or already revoked")
    return {"status": "revoked"}
