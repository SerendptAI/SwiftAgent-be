"""FastAPI middleware that records an audit entry for every state-changing
request on company-scoped resources.

Read-only requests are not logged (audit covers access to *data mutations*;
sensitive read paths can opt in via ``AUDITED_READ_PATHS``). Failures inside
the audit writer never break the request itself.
"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.services import audit_service

logger = logging.getLogger(__name__)

AUDITED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Paths where even reads are sensitive enough to log.
AUDITED_READ_PATHS = ("/api/v1/companies",)


def _company_id_from_path(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if "companies" in parts:
        index = parts.index("companies")
        if index + 1 < len(parts):
            return parts[index + 1]
    return None


def _action_from_request(method: str, path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    domain = parts[1] if len(parts) > 1 else "api"
    verb = {
        "POST": "created",
        "PUT": "updated",
        "PATCH": "updated",
        "DELETE": "deleted",
    }.get(method, method.lower())
    return f"{domain}.{verb}"


class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        should_audit = (
            request.method in AUDITED_METHODS
            or request.url.path.startswith(AUDITED_READ_PATHS)
        )
        if not should_audit:
            return await call_next(request)

        start = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - start) * 1000)

        user = getattr(request.state, "user", None) or {}
        try:
            await audit_service.record_event(
                company_id=_company_id_from_path(request.url.path),
                actor_id=user.get("user_id"),
                actor_type="user" if user else "system",
                action=_action_from_request(request.method, request.url.path),
                resource_type=request.url.path.rstrip("/").split("/")[-1] or "root",
                resource_id=None,
                outcome="success" if response.status_code < 400 else "failure",
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                metadata={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                },
            )
        except Exception:
            logger.exception("Failed to write audit log entry")
        return response
