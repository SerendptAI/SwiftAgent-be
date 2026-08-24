"""
RBAC (Role-Based Access Control) core for SwiftAgent-be.

Provides:
- Role definitions and permission mappings
- Permission checking dependencies for FastAPI
- Company-scoped access enforcement
"""

from typing import Optional
from fastapi import HTTPException, Depends, status
from app.core.auth import get_current_user

# ── Role Definitions ──────────────────────────────────────────────────────────

ROLE_PERMISSIONS: dict[str, list[str]] = {
    "owner": ["*"],
    "admin": [
        "tickets:*", "conversations:*", "knowledge:*",
        "analytics:*", "settings:*", "users:*",
        "billing:read", "prompts:*", "forms:*",
        "stroll:*", "notifications:*", "integrations:*",
    ],
    "manager": [
        "tickets:*", "conversations:*", "knowledge:*",
        "analytics:*", "forms:*", "billing:read",
        "prompts:read", "stroll:read", "notifications:*",
    ],
    "agent": [
        "tickets:read", "tickets:write",
        "conversations:read", "knowledge:read",
        "analytics:read", "forms:read",
    ],
    "viewer": [
        "tickets:read", "conversations:read",
        "analytics:read", "knowledge:read",
        "forms:read",
    ],
}

VALID_ROLES = list(ROLE_PERMISSIONS.keys())


def get_role_permissions(role: str) -> list[str]:
    """Get the list of permissions for a role."""
    return ROLE_PERMISSIONS.get(role, [])


def has_permission(user_permissions: list[str], required: str) -> bool:
    """Check if user has a specific permission (supports wildcards)."""
    if not user_permissions:
        return False
    
    # Exact match
    if required in user_permissions:
        return True
    
    # Full wildcard
    if "*" in user_permissions:
        return True
    
    # Resource wildcard (e.g., "tickets:*" matches "tickets:read")
    if ":" in required:
        resource, _ = required.split(":", 1)
        if f"{resource}:*" in user_permissions:
            return True
    
    return False


class RBACError(HTTPException):
    """Raised when a user lacks required permissions."""
    def __init__(self, required: str):
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: '{required}' required",
        )


def require_permission(required_permission: str):
    """
    FastAPI dependency factory that enforces a permission check.
    
    Usage:
        @router.delete("/{doc_id}")
        async def delete_doc(
            doc_id: str,
            user=Depends(require_permission("knowledge:delete"))
        ):
            ...
    """
    async def permission_checker(
        user: dict = Depends(get_current_user),
    ) -> dict:
        user_permissions = user.get("permissions", [])
        
        if has_permission(user_permissions, required_permission):
            return user
        
        raise RBACError(required_permission)
    
    return permission_checker


def require_company_access():
    """
    FastAPI dependency factory that ensures the user belongs to the company
    being accessed via the company_id path parameter.
    
    Usage:
        @router.get("/{company_id}/tickets")
        async def list_tickets(
            company_id: str,
            user=Depends(require_company_access())
        ):
            ...
    """
    async def company_checker(
        company_id: str,
        user: dict = Depends(get_current_user),
    ) -> dict:
        user_company = user.get("company_id")
        user_permissions = user.get("permissions", [])
        
        # Owner/admin with wildcard can access any company
        if "*" in user_permissions:
            return user
        
        if user_company != company_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access to this company's data is denied",
            )
        
        return user
    
    return company_checker


def require_any_permission(*permissions: str):
    """
    FastAPI dependency factory that requires ANY of the listed permissions.
    
    Usage:
        @router.get("/dashboard")
        async def dashboard(
            user=Depends(require_any_permission("analytics:read", "admin:read"))
        ):
            ...
    """
    async def permission_checker(
        user: dict = Depends(get_current_user),
    ) -> dict:
        user_permissions = user.get("permissions", [])
        
        for perm in permissions:
            if has_permission(user_permissions, perm):
                return user
        
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied: requires any of {list(permissions)}",
        )
    
    return permission_checker
