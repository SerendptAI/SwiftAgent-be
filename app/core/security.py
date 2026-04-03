import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from jose import jwt
from app.core.config import settings

logger = logging.getLogger(__name__)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire, "jti": str(uuid4())})
    encoded_jwt = jwt.encode(
        to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM
    )
    return encoded_jwt


def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    secret = settings.REFRESH_SECRET_KEY or settings.SECRET_KEY
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            days=settings.REFRESH_TOKEN_EXPIRE_DAYS
        )

    to_encode.update({"exp": expire, "type": "refresh", "jti": str(uuid4())})
    encoded_jwt = jwt.encode(to_encode, secret, algorithm=settings.ALGORITHM)
    return encoded_jwt


def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Token expired: signature has expired")
        return None
    except jwt.JWTError as e:
        logger.warning("Token validation failed: %s", e)
        return None


def decode_refresh_token(token: str) -> Optional[dict]:
    secret = settings.REFRESH_SECRET_KEY or settings.SECRET_KEY
    try:
        payload = jwt.decode(token, secret, algorithms=[settings.ALGORITHM])
        if payload.get("type") != "refresh":
            logger.warning("Token type mismatch: expected refresh token")
            return None
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("Refresh token expired")
        return None
    except jwt.JWTError as e:
        logger.warning("Refresh token validation failed: %s", e)
        return None
