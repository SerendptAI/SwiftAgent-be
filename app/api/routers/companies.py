from fastapi import APIRouter, Depends, HTTPException
from typing import List
from app.core.auth import get_current_user
from app.models.company_models import (
    CompanyInfoCreate,
    CompanyIdentityUpdate,
    CompanyTypeUpdate,
    AnswerBoundariesUpdate,
    VoiceSettingsUpdate,
    CompanyResponse,
    CompanySummary,
)
from app.services import company_service, cloudinary_service
from fastapi import UploadFile, File, Form

router = APIRouter(tags=["Companies"])

@router.post("/", response_model=CompanyResponse)
async def create_company(
    data: CompanyInfoCreate,
    current_user: dict = Depends(get_current_user),
):
    """Create a new company (onboarding step 1)."""
    user_id = current_user["user_id"]
    company = await company_service.create_company(user_id, data.model_dump())
    return company

@router.get("/", response_model=List[CompanySummary])
async def list_companies(
    current_user: dict = Depends(get_current_user),
):
    """List all companies for the current user."""
    user_id = current_user["user_id"]
    return await company_service.list_companies(user_id)

@router.get("/{company_id}", response_model=CompanyResponse)
async def get_company(
    company_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Get full company details."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company

@router.patch("/{company_id}/identity", response_model=CompanyResponse)
async def update_identity(
    company_id: str,
    data: CompanyIdentityUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update company identity (onboarding step 2)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await company_service.update_identity(company_id, user_id, data.model_dump(exclude_none=True))

@router.patch("/{company_id}/type", response_model=CompanyResponse)
async def update_company_type(
    company_id: str,
    data: CompanyTypeUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Set company type — saas_finance or crypto (onboarding step 3)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await company_service.update_company_type(company_id, user_id, data.company_type)

@router.patch("/{company_id}/boundaries", response_model=CompanyResponse)
async def update_boundaries(
    company_id: str,
    data: AnswerBoundariesUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update answer boundaries (onboarding step 4)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await company_service.update_boundaries(company_id, user_id, data.model_dump())

@router.patch("/{company_id}/voice", response_model=CompanyResponse)
async def update_voice(
    company_id: str,
    data: VoiceSettingsUpdate,
    current_user: dict = Depends(get_current_user),
):
    """Update voice settings and finalize setup (onboarding step 5)."""
    user_id = current_user["user_id"]
    company = await company_service.get_company(company_id, user_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return await company_service.update_voice(company_id, user_id, data.model_dump())

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
        
    logo_url = await cloudinary_service.upload_image(file, folder=f"logos/{company_id}")
    return await company_service.update_logo(company_id, user_id, logo_url)

