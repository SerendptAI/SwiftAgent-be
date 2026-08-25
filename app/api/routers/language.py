from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Response
from app.core.auth import verify_analytics_secret_key
from app.core.database import db
from app.services.language_service import detect_language, get_company_language_config
from app.models.language_models import SUPPORTED_LANGUAGES

router = APIRouter(tags=["Language"])


@router.get("/language/detect")
async def detect_language_endpoint(
    text: str,
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Detect the language of a text string.
    Returns ISO 639-1 code, language name, and confidence.
    """
    response.headers["Cache-Control"] = "private, max-age=300"

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Text is required")

    detection = await detect_language(text)
    if not detection:
        raise HTTPException(status_code=500, detail="Language detection failed")

    return detection


@router.get("/language/supported")
async def list_supported_languages(
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    List all supported languages with their ISO codes and names.
    """
    response.headers["Cache-Control"] = "public, max-age=3600"
    return {
        "supported_languages": SUPPORTED_LANGUAGES,
        "count": len(SUPPORTED_LANGUAGES),
    }


@router.get("/language/company/{company_id}/config")
async def get_company_language_config_endpoint(
    company_id: str,
    response: Response,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Get the language configuration for a company.
    """
    response.headers["Cache-Control"] = "private, max-age=300"

    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    config = await get_company_language_config(company)
    return config


@router.put("/language/company/{company_id}/config")
async def update_company_language_config(
    company_id: str,
    response: Response,
    primary_language: str,
    supported_languages: list[str],
    auto_detect: bool = True,
    language_specific_kb: bool = True,
    auth: dict = Depends(verify_analytics_secret_key),
):
    """
    Update the language configuration for a company.
    """
    response.headers["Cache-Control"] = "private, max-age=0"

    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    await db.companies.update_one(
        {"id": company_id},
        {"$set": {
            "primary_language": primary_language,
            "supported_languages": supported_languages,
            "auto_detect_language": auto_detect,
            "language_specific_kb": language_specific_kb,
            "updated_at": datetime.utcnow(),
        }},
    )

    return {
        "success": True,
        "company_id": company_id,
        "primary_language": primary_language,
        "supported_languages": supported_languages,
        "auto_detect": auto_detect,
        "language_specific_kb": language_specific_kb,
    }
