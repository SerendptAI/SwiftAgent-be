"""
User management API router — invite, accept, suspend, role changes.
"""

from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr

from app.core.auth import get_current_user
from app.core.rbac import require_permission, require_company_access, VALID_ROLES
from app.core.config import settings
from app.core.audit import AuditLogger, audit_action
from app.services import company_user_service

router = APIRouter(tags=["User Management"])


# ── Request/Response Models ──────────────────────────────────────────────────

class InviteCreateRequest(BaseModel):
    email: EmailStr
    role: str


class InviteAcceptRequest(BaseModel):
    token: str


class RoleUpdateRequest(BaseModel):
    role: str


class UserResponse(BaseModel):
    id: str
    company_id: str
    user_id: str
    email: str
    role: str
    permissions: list[str]
    is_active: bool
    joined_at: Optional[datetime] = None
    created_at: datetime


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/{company_id}/users/invite")
async def invite_user(
    company_id: str,
    request: InviteCreateRequest,
    current_user: dict = Depends(require_permission("users:write")),
):
    """
    Invite a new user to the company.
    Only admins and owners can invite users.
    """
    if request.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {VALID_ROLES}",
        )
    
    # Prevent inviting users with higher or equal role
    current_user_role = current_user.get("role", "")
    role_hierarchy = ["viewer", "agent", "manager", "admin", "owner"]
    
    if role_hierarchy.index(request.role) >= role_hierarchy.index(current_user_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot invite a user with equal or higher role than yourself",
        )
    
    try:
        invite = await company_user_service.create_invite(
            company_id=company_id,
            email=request.email,
            role=request.role,
            invited_by=current_user["user_id"],
        )
        
        # TODO: Send invite email with token link
        
        return {
            "status": "success",
            "message": f"Invite sent to {request.email}",
            "invite": invite,
        }
    
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/users/accept-invite")
async def accept_invite(
    request: InviteAcceptRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Accept an invite and join a company.
    The invite email must match the authenticated user's email.
    """
    try:
        company_user = await company_user_service.accept_invite(
            token=request.token,
            user_id=current_user["user_id"],
            user_email=current_user.get("email", ""),
        )
        
        return {
            "status": "success",
            "message": "Successfully joined the company",
            "company_user": company_user,
        }
    
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/users/accept-invite")
async def accept_invite_get(token: str):
    """
    Redirect to frontend with invite token.
    Frontend will extract the token and call POST /users/accept-invite.
    """
    from fastapi.responses import RedirectResponse
    frontend_url = getattr(settings, "FRONTEND_URL", "https://swiftagents.org").rstrip("/")
    return RedirectResponse(url=f"{frontend_url}/accept-invite?token={token}")


@router.get("/{company_id}/users")
async def list_users(
    company_id: str,
    current_user: dict = Depends(require_permission("users:read")),
):
    """List all users in a company."""
    users = await company_user_service.get_company_users(company_id)
    return {"users": users, "total": len(users)}


@router.get("/{company_id}/users/{user_id}")
async def get_user(
    company_id: str,
    user_id: str,
    current_user: dict = Depends(require_permission("users:read")),
):
    """Get a specific user's details."""
    user = await company_user_service.get_company_user(company_id, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.patch("/{company_id}/users/{user_id}/role")
async def update_role(
    company_id: str,
    user_id: str,
    request: RoleUpdateRequest,
    current_user: dict = Depends(require_permission("users:write")),
):
    """Update a user's role within the company."""
    if request.role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role. Must be one of: {VALID_ROLES}",
        )
    
    # Prevent promoting users to equal or higher role than yourself
    current_user_role = current_user.get("role", "")
    role_hierarchy = ["viewer", "agent", "manager", "admin", "owner"]
    
    if role_hierarchy.index(request.role) >= role_hierarchy.index(current_user_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot assign a role equal or higher than your own",
        )
    
    try:
        updated = await company_user_service.update_user_role(
            company_id=company_id,
            user_id=user_id,
            new_role=request.role,
            updated_by=current_user["user_id"],
        )
        return {"status": "success", "user": updated}
    
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/{company_id}/users/{user_id}/suspend")
async def suspend_user(
    company_id: str,
    user_id: str,
    current_user: dict = Depends(require_permission("users:write")),
):
    """Suspend a user (soft delete, can be reactivated)."""
    try:
        result = await company_user_service.suspend_user(
            company_id=company_id,
            user_id=user_id,
            suspended_by=current_user["user_id"],
        )
        return {"status": "success", "message": "User suspended", "user": result}
    
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.post("/{company_id}/users/{user_id}/reactivate")
async def reactivate_user(
    company_id: str,
    user_id: str,
    current_user: dict = Depends(require_permission("users:write")),
):
    """Reactivate a suspended user."""
    try:
        result = await company_user_service.reactivate_user(
            company_id=company_id,
            user_id=user_id,
            reactivated_by=current_user["user_id"],
        )
        return {"status": "success", "message": "User reactivated", "user": result}
    
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.delete("/{company_id}/users/{user_id}")
async def remove_user(
    company_id: str,
    user_id: str,
    current_user: dict = Depends(require_permission("users:delete")),
):
    """Permanently remove a user from the company."""
    try:
        await company_user_service.remove_user(
            company_id=company_id,
            user_id=user_id,
            removed_by=current_user["user_id"],
        )
        return {"status": "success", "message": "User removed"}
    
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@router.get("/users/me/companies")
async def get_my_companies(
    current_user: dict = Depends(get_current_user),
):
    """Get all companies the current user belongs to."""
    companies = await company_user_service.get_user_companies(current_user["user_id"])
    return {"companies": companies}
