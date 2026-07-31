from typing import Optional
from fastapi import HTTPException, Security, status, Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.core.security import decode_access_token
from app.core.database import get_database
from app.core.config import settings
from app.services import api_key_service

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Security(security),
    db = Depends(get_database)
):
    token = credentials.credentials
    try:
        payload = decode_access_token(token)
        if payload is None:
             raise ValueError("Invalid Token")

        user_id = payload.get("sub")
        if user_id is None:
             raise ValueError("Token missing user_id")

        # fetch user from db to ensure validity
        user = await db.users.find_one({"user_id": user_id})
        if not user:
            raise HTTPException(status_code=401, detail="Your account was not found. It may have been deleted or deactivated. Please sign in again.")

        return user

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def verify_analytics_secret_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    x_secret_key: Optional[str] = Header(default=None, alias="X-Secret-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> dict:
    """
    Server-to-server authentication dependency for analytics endpoints.
    Accepts X-API-Key, X-Secret-Key, or Bearer <key> in Authorization header.
    Verifies against settings.ANALYTICS_SECRET_KEY or company_api_keys collection.
    """
    raw_key = x_api_key or x_secret_key
    if not raw_key and authorization and authorization.startswith("Bearer "):
        raw_key = authorization.removeprefix("Bearer ").strip()

    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing server-to-server secret key. Provide X-API-Key, X-Secret-Key, or Bearer authorization header.",
        )

    # 1. Check if it matches configured global server secret key
    if settings.ANALYTICS_SECRET_KEY and raw_key == settings.ANALYTICS_SECRET_KEY:
        return {"auth_type": "server_secret", "company_id": None}

    # 2. Check if it matches a valid company API key in DB
    key_doc = await api_key_service.verify_api_key(raw_key)
    if key_doc:
        return {"auth_type": "company_api_key", "company_id": key_doc["company_id"]}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or revoked server-to-server secret key",
    )
