from fastapi import HTTPException, Security, status, Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.core.security import decode_access_token
from app.core.database import get_database

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
            raise HTTPException(status_code=404, detail="User not found")

        return user

    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
