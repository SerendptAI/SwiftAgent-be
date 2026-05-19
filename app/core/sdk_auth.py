"""
SDK Auth Middleware.

Handles:
1. API Key verification (for SDK initialization).
2. SDK Session JWT verification (for SDK chat and data access).
"""

from fastapi import Depends, Header, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import db
from app.core.security import decode_access_token
from app.services import api_key_service

security = HTTPBearer()

async def verify_api_key(
    x_api_key: str = Header(..., alias="X-API-Key", description="Company API Key")
) -> dict:
    """Verify an API key header and return the company document."""
    key_doc = await api_key_service.verify_api_key(x_api_key)
    if not key_doc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    company_id = key_doc["company_id"]
    company = await db.companies.find_one({"id": company_id})
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company associated with this API key no longer exists",
        )

    return company


async def get_sdk_session(
    credentials: HTTPAuthorizationCredentials = Security(security),
) -> dict:
    """
    Verify SDK session JWT and return claims: {sub (email), company_id, type}.
    """
    token = credentials.credentials
    payload = decode_access_token(token)

    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired SDK session token",
        )

    if payload.get("type") != "sdk":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token is not a valid SDK session token",
        )

    user_email = payload.get("sub")
    company_id = payload.get("company_id")

    if not user_email or not company_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed SDK session token (missing email or company_id)",
        )

    return {
        "email": user_email,
        "company_id": company_id,
        "jti": payload.get("jti"),
    }
