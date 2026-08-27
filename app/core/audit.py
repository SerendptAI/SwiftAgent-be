"""
Audit system for SwiftAgent-be.

Provides:
- AuditEvent model
- Audit middleware for automatic request capture
- @audit_action decorator for semantic business events
- Audit log query helpers
"""

from datetime import datetime, timedelta, timezone
from typing import Optional, Callable, Any
from functools import wraps
from fastapi import Request, Response
from pydantic import BaseModel, Field
from app.core.config import settings
import logging
import json

logger = logging.getLogger(__name__)


# ── Models ────────────────────────────────────────────────────────────────────

class AuditEvent(BaseModel):
    """Represents a single auditable event in the system."""
    
    event_id: str = Field(default_factory=lambda: __import__('uuid').uuid4().hex)
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    actor_role: Optional[str] = None
    company_id: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    action: Optional[str] = None
    status: str = "success"
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    request_id: Optional[str] = None
    path: Optional[str] = None
    method: Optional[str] = None
    before: Optional[dict] = None
    after: Optional[dict] = None
    changes: Optional[list[str]] = None
    metadata: Optional[dict] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Audit Logger ──────────────────────────────────────────────────────────────

class AuditLogger:
    """Handles writing audit events to MongoDB."""
    
    COLLECTION_NAME = "audit_events"
    
    @staticmethod
    async def log_event(
        actor_id: Optional[str] = None,
        actor_email: Optional[str] = None,
        actor_role: Optional[str] = None,
        company_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        action: Optional[str] = None,
        status: str = "success",
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
        path: Optional[str] = None,
        method: Optional[str] = None,
        before: Optional[dict] = None,
        after: Optional[dict] = None,
        changes: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
    ) -> Optional[str]:
        """
        Write an audit event to the database.
        Returns the event_id on success, None on failure.
        Never raises — audit logging should never break the main request.
        """
        try:
            from app.core.database import db
            
            event = AuditEvent(
                actor_id=actor_id,
                actor_email=actor_email,
                actor_role=actor_role,
                company_id=company_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                status=status,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                path=path,
                method=method,
                before=before,
                after=after,
                changes=changes,
                metadata=metadata,
            )
            
            result = await db[AuditLogger.COLLECTION_NAME].insert_one(
                event.model_dump(exclude_none=True)
            )
            return event.event_id
        
        except Exception as e:
            # Audit logging must NEVER break the main request
            logger.error(f"Failed to write audit event: {e}")
            return None
    
    @staticmethod
    async def log_request(
        request: Request,
        user: Optional[dict] = None,
        status_code: int = 200,
    ):
        """Log a generic HTTP request (used by middleware)."""
        return await AuditLogger.log_event(
            actor_id=user.get("user_id") if user else None,
            actor_email=user.get("email") if user else None,
            actor_role=user.get("role") if user else None,
            company_id=user.get("company_id") if user else None,
            resource_type="http_request",
            action="request",
            status="success" if status_code < 400 else "error",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.state.get("request_id"),
            path=str(request.url.path),
            method=request.method,
            metadata={"status_code": status_code},
        )


# ── Audit Decorator ───────────────────────────────────────────────────────────

def audit_action(
    resource_type: str,
    action: str,
    get_resource_id: Optional[Callable] = None,
    capture_changes: bool = False,
    get_company_id: Optional[Callable] = None,
):
    """
    Decorator that logs semantic business events.
    
    Usage:
        @audit_action("ticket", "write", capture_changes=True)
        async def update_ticket(ticket_id: str, updates: dict, user=Depends(...)):
            ...
    
    Args:
        resource_type: Category of resource (e.g., "ticket", "user", "knowledge")
        action: The action performed (e.g., "read", "write", "delete", "export")
        get_resource_id: Optional callable to extract resource ID from args/kwargs
        capture_changes: If True, captures before/after state for mutations
        get_company_id: Optional callable to extract company_id from args/kwargs
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract user from kwargs
            user = kwargs.get("user") or kwargs.get("current_user") or kwargs.get("current_admin")
            company_id = kwargs.get("company_id")
            
            # Get resource ID if extractor provided
            resource_id = None
            if get_resource_id:
                try:
                    resource_id = get_resource_id(*args, **kwargs)
                except Exception:
                    pass
            
            # Get company ID from callable if provided
            if get_company_id:
                try:
                    company_id = get_company_id(*args, **kwargs)
                except Exception:
                    pass
            
            # Capture "before" state if needed
            before = None
            if capture_changes and resource_id:
                before = await _fetch_before(resource_type, resource_id)
            
            # Execute the actual function
            result = await func(*args, **kwargs)
            
            # Capture "after" state
            after = None
            changes = None
            if capture_changes and resource_id:
                after = await _fetch_after(resource_type, resource_id)
                changes = _diff_states(before, after)
            
            # Determine status
            status = "success"
            if isinstance(result, Response):
                status = "success" if result.status_code < 400 else "error"
            
            # Log the event
            await AuditLogger.log_event(
                actor_id=user.get("user_id") if user else "system",
                actor_email=user.get("email") if user else None,
                actor_role=user.get("role") if user else None,
                company_id=company_id,
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                status=status,
                before=before,
                after=after,
                changes=changes,
            )
            
            return result
        
        return wrapper
    return decorator


async def _fetch_before(resource_type: str, resource_id: str) -> Optional[dict]:
    """Fetch the current state of a resource before mutation."""
    try:
        from app.core.database import db
        collection_map = {
            "ticket": "email_tickets",
            "conversation": "widget_conversations",
            "knowledge": "knowledge_docs",
            "user": "users",
            "company": "companies",
            "form": "forms",
        }
        collection = collection_map.get(resource_type)
        if collection:
            doc = await db[collection].find_one({"id": resource_id})
            if doc:
                doc.pop("_id", None)
            return doc
    except Exception as e:
        logger.warning(f"Failed to fetch before state for {resource_type}/{resource_id}: {e}")
    return None


async def _fetch_after(resource_type: str, resource_id: str) -> Optional[dict]:
    """Fetch the current state of a resource after mutation."""
    return await _fetch_before(resource_type, resource_id)


def _diff_states(before: Optional[dict], after: Optional[dict]) -> list[str]:
    """Compute human-readable differences between two states."""
    changes = []
    
    if not before or not after:
        return changes
    
    # Compare common keys
    all_keys = set(before.keys()) | set(after.keys())
    for key in sorted(all_keys):
        if key.startswith("_"):
            continue
        old_val = before.get(key)
        new_val = after.get(key)
        if old_val != new_val:
            # Truncate long values
            old_str = str(old_val)[:100] if old_val is not None else "None"
            new_str = str(new_val)[:100] if new_val is not None else "None"
            changes.append(f"{key}: {old_str} → {new_str}")
    
    return changes


# ── Audit Middleware ───────────────────────────────────────────────────────────

class AuditMiddleware:
    """
    Starlette middleware that automatically captures all authenticated requests.
    Must be outermost middleware to capture all requests.
    """
    
    def __init__(self, app):
        self.app = app
    
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        
        # Only audit API routes
        path = scope.get("path", "")
        if not path.startswith("/api/v1/"):
            await self.app(scope, receive, send)
            return
        
        # Skip health checks and docs
        skip_paths = ("/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico")
        if path in skip_paths:
            await self.app(scope, receive, send)
            return
        
        # Capture request details
        request = Request(scope, receive)
        
        # Extract user from request state (set by auth middleware)
        user = getattr(request.state, "user", None)
        
        # Capture response status
        response_status = 200
        
        async def wrapped_send(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 200)
            await send(message)
        
        await self.app(scope, receive, wrapped_send)
        
        # Log the request (fire-and-forget)
        try:
            await AuditLogger.log_request(request, user, response_status)
        except Exception:
            pass  # Never break the request


# ── Query Helpers ─────────────────────────────────────────────────────────────

async def query_audit_events(
    company_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = 100,
    skip: int = 0,
) -> list[dict]:
    """Query audit events with filters."""
    from app.core.database import db
    
    query = {}
    
    if company_id:
        query["company_id"] = company_id
    if actor_id:
        query["actor_id"] = actor_id
    if resource_type:
        query["resource_type"] = resource_type
    if resource_id:
        query["resource_id"] = resource_id
    if action:
        query["action"] = action
    if status:
        query["status"] = status
    
    # Date range
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    if date_filter:
        query["timestamp"] = date_filter
    
    cursor = db[AuditLogger.COLLECTION_NAME].find(query).sort(
        "timestamp", -1
    ).skip(skip).limit(limit)
    
    events = []
    async for doc in cursor:
        doc.pop("_id", None)
        events.append(doc)
    
    return events


async def ensure_audit_indexes():
    """Create indexes for the audit_events collection."""
    from app.core.database import db
    
    collection = db[AuditLogger.COLLECTION_NAME]
    
    # Core indexes
    await collection.create_index("timestamp")
    await collection.create_index("company_id")
    await collection.create_index("actor_id")
    await collection.create_index("resource_type")
    await collection.create_index("action")
    await collection.create_index("status")
    
    # Compound indexes for common queries
    await collection.create_index([("company_id", 1), ("timestamp", -1)])
    await collection.create_index([("actor_id", 1), ("timestamp", -1)])
    await collection.create_index([("resource_type", 1), ("resource_id", 1)])
    
    # TTL index: auto-delete after configured retention period (default: 365 days)
    ttl_days = getattr(settings, "AUDIT_LOG_RETENTION_DAYS", 365)
    await collection.create_index(
        "timestamp",
        expireAfterSeconds=ttl_days * 86400,
        name="ttl_audit_events",
    )
