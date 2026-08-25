import logging
import time
import uuid
from contextlib import asynccontextmanager

from app.core.logging_setup import install_default_log_record_fields

install_default_log_record_fields()
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.routers import (
    auth,
    knowledge,
    diagnosis,
    conversations,
    companies,
    dashboard,
    voice,
    billing,
    chat,
    stroll,
    stroll_public,
    email,
    mobile,
    sdk,
    forms,
    forms_public,
    integrations,
    notifications,
    analytics,
    feedback,
    audit_log,
)
from app.core.config import settings
from app.core.database import create_indexes
from app.services.stroll_service import init_browser, close_browser
from app.services.stroll_scheduler import init_scheduler, close_scheduler
from app.services import wrap_scheduler
from app.services import ticket_scheduler
from app.core.audit_middleware import AuditLogMiddleware
from app.services import audit_service
from app.core.langfuse import init_langfuse, shutdown_langfuse

# structured logging setup
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await create_indexes()
        await audit_service.ensure_audit_indexes()
    except Exception as e:
        logging.getLogger(__name__).warning("DB index creation failed: %s", e)

    # Langfuse LLM observability (no-ops gracefully if keys are unset)
    init_langfuse()

    try:
        await init_browser()
    except Exception:
        logging.getLogger(__name__).warning(
            "Playwright browser init failed — stroll feature unavailable"
        )

    # Start stroll schedules
    try:
        await init_scheduler()
        wrap_scheduler.init_scheduler()
        ticket_scheduler.init_scheduler()
    except Exception as e:
        logging.getLogger(__name__).error(f"Scheduler init failed: {e}")

    yield
    # shutdown: close Playwright browser and scheduler
    try:
        await close_scheduler()
        wrap_scheduler.close_scheduler()
        ticket_scheduler.close_scheduler()
    except Exception:
        pass

    try:
        await close_browser()
    except Exception:
        pass

    shutdown_langfuse()


app = FastAPI(
    title="Swift Agent API",
    description="Voice-first AI support platform for crypto applications.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware is added below alongside other middlewares to enforce ordering.
# See the middleware registration block near the bottom of this file.


@app.middleware("http")
async def add_security_headers(request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    if request.scope["type"] == "http":
        path = request.url.path
        
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        
        # Exempt /docs and /redoc from strict CSP since they need inline scripts/styles
        if path in ("/docs", "/redoc", "/openapi.json"):
            # Relaxed CSP for documentation endpoints
            response.headers["Content-Security-Policy"] = (
                "default-src 'self' https:; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:; "
                "style-src 'self' 'unsafe-inline' https:; "
                "img-src 'self' data: https:; "
                "font-src 'self' https:; "
                "connect-src 'self' https:"
            )
        else:
            # Strict CSP for API endpoints
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:;"
            )
    return response


class RequestIdMiddleware:
    """Attach a unique request ID to every request for log correlation."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            request_id = str(uuid.uuid4())
            scope["state"] = scope.get("state", {})
            scope["state"]["request_id"] = request_id
            
            from app.core.logging_setup import request_id_context_var
            token = request_id_context_var.set(request_id)
            
            try:
                await self.app(scope, receive, send)
            finally:
                request_id_context_var.reset(token)
        else:
            await self.app(scope, receive, send)


import re

class WidgetCorsBypassMiddleware:
    """
    Middleware to dynamically handle CORS for public widget endpoints
    based on the company's configured allowed origins.
    """

    _UUID_RE = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        re.IGNORECASE,
    )

    BYPASS_PREFIXES = (
        "/api/v1/sdk",
        "/api/v1/voice",
        "/api/v1/chat",
        "/api/v1/public/stroll",
        "/api/v1/public/forms",
        # NOTE: /api/v1/stroll is intentionally excluded — all routes are JWT-protected
        # admin endpoints. Widget origins should not receive CORS access to them.
        "/api/v1/email/inbound",
        "/api/v1/email/resolve",
    )
    BYPASS_SUFFIXES = ("/visitors/log", "/public")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        is_http = scope["type"] == "http"
        is_ws = scope["type"] == "websocket"

        if is_ws:
            # Strip the Origin header from the scope for ALL WebSocket connections
            # so that downstream Starlette CORSMiddleware doesn't block the upgrade
            # with a bare 403 Forbidden when origin isn't whitelisted.
            # WebSocket authentication is enforced at each endpoint via JWT / API key.
            if "headers" in scope:
                scope["headers"] = [
                    (k, v) for k, v in scope["headers"]
                    if k.lower() != b"origin"
                ]
            await self.app(scope, receive, send)
            return

        if is_http:
            path = scope.get("path", "")
            if any(path.startswith(p) for p in self.BYPASS_PREFIXES) or any(
                path.endswith(s) for s in self.BYPASS_SUFFIXES
            ):
                # These are public widget endpoints embedded on arbitrary customer
                # websites — allow any origin. Auth is enforced at the endpoint level
                # via API key, not CORS. Note: * is incompatible with credentials=true,
                # which is correct here since widget auth uses API keys, not cookies.
                if scope["method"] == "OPTIONS":
                    from starlette.responses import Response
                    response = Response(
                        status_code=200,
                        headers={
                            "Access-Control-Allow-Origin": "*",
                            "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS",
                            "Access-Control-Allow-Headers": "*",
                            "Access-Control-Max-Age": "86400",
                        },
                    )
                    await response(scope, receive, send)
                    return
                else:
                    async def custom_send(message):
                        if message.get("type") in ("http.response.start", "websocket.accept"):
                            res_headers = [
                                (k, v) for k, v in message.get("headers", [])
                                if k.lower() not in (
                                    b"access-control-allow-origin",
                                    b"access-control-allow-credentials",
                                    b"access-control-allow-methods",
                                    b"access-control-allow-headers",
                                )
                            ]
                            res_headers.append((b"access-control-allow-origin", b"*"))
                            message["headers"] = res_headers
                        await send(message)

                    await self.app(scope, receive, custom_send)
                    return

        await self.app(scope, receive, send)


class LoggingMiddleware:
    """Log request method, path, status code, and duration."""

    def __init__(self, app):
        self.app = app
        self.logger = logging.getLogger("app.access")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        path = scope.get("path", "")
        method = scope.get("method", "")

        # capture response status code
        response_status = 500

        async def send_wrapper(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 500)
            await send(message)

        await self.app(scope, receive, send_wrapper)

        duration_ms = (time.perf_counter() - start) * 1000
        self.logger.info("%s %s %d %.1fms", method, path, response_status, duration_ms)


# Middleware execution order (Starlette reverses registration order):
# WidgetCorsBypassMiddleware → LoggingMiddleware → RequestIdMiddleware → CORSMiddleware → app
# WidgetCorsBypassMiddleware is registered last so it executes FIRST (outermost),
# allowing it to overwrite CORS headers AFTER CORSMiddleware has already run.
app.add_middleware(RequestIdMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(CORSMiddleware,
    allow_origins=settings.allowed_hosts_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(WidgetCorsBypassMiddleware)
app.add_middleware(AuditLogMiddleware)

# routers
app.mount("/chat-avatars", StaticFiles(directory="app/chat-avatars"), name="chat-avatars")
app.mount("/email-fonts", StaticFiles(directory="app/email_templates/fonts"), name="email-fonts")
app.mount("/images", StaticFiles(directory="app/email_templates/images"), name="images")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router, prefix="/api/v1/auth")
app.include_router(knowledge.router, prefix="/api/v1/knowledge")
app.include_router(audit_log.router, prefix="/api/v1/audit")
app.include_router(diagnosis.router, prefix="/api/v1/diagnosis")
app.include_router(conversations.router, prefix="/api/v1/conversations")
app.include_router(companies.router, prefix="/api/v1/companies")
app.include_router(dashboard.router, prefix="/api/v1/dashboard")
app.include_router(analytics.router, prefix="/api/v1/analytics")
app.include_router(feedback.router)
app.include_router(billing.router, prefix="/api/v1/billing")

app.include_router(voice.router, prefix="/api/v1/voice")
app.include_router(chat.router, prefix="/api/v1/chat")
app.include_router(stroll.router, prefix="/api/v1/stroll")
app.include_router(stroll_public.router, prefix="/api/v1/public/stroll")
app.include_router(email.router, prefix="/api/v1/email")
app.include_router(mobile.router, prefix="/api/v1/mobile")
app.include_router(sdk.router, prefix="/api/v1/sdk", tags=["SDK"])
app.include_router(forms.router, prefix="/api/v1/forms", tags=["Forms"])
app.include_router(forms_public.router, prefix="/api/v1/public/forms", tags=["Forms"])
app.include_router(
    integrations.router,
    prefix="/api/v1/companies/{company_id}/integrations",
    tags=["API Integrations"],
)
app.include_router(notifications.router, prefix="/api/v1/notifications")

# global exception handlers
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    # Mask noisy locs
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Validation error",
            "errors": [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in errors]
        },
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger = logging.getLogger("app.errors")
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception(
        "Unhandled exception on %s %s [request_id=%s]",
        request.method,
        request.url.path,
        request_id,
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "An internal server error occurred. Please try again later.",
            "request_id": request_id,
        },
    )


@app.get("/health", tags=["Health"])
async def health_check():
    from app.core.database import mongo_client, qdrant_client

    checks = {"api": "ok"}

    try:
        await mongo_client.admin.command("ping")
        checks["mongodb"] = "ok"
    except Exception as e:
        checks["mongodb"] = f"error: {e}"

    try:
        await qdrant_client.get_collections()
        checks["qdrant"] = "ok"
    except Exception as e:
        checks["qdrant"] = f"error: {e}"

    status_code = 200 if all(v == "ok" for v in checks.values()) else 503
    return JSONResponse(
        status_code=status_code, content={"status": "healthy", "checks": checks}
    )
