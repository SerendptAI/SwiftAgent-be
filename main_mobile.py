"""
Mobile-only backend — lightweight FastAPI entrypoint.

Contains ONLY the endpoints needed by the mobile companion app:
  • Auth      — OTP login/signup, token refresh, profile
  • Mobile    — device registration, OTP challenge relay
  • Email     — ticket CRUD, reply, inbound webhook, resolve
  • Dashboard — resolved items (chats + tickets), read messages, mark seen
  • Companies — company context, list companies

Excludes: knowledge, diagnosis, conversations, voice, chat widget,
          stroll, stroll_public, billing, Playwright, scheduler.
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager

from app.core.logging_setup import install_default_log_record_fields

install_default_log_record_fields()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routers import (
    auth,
    mobile,
    email,
    dashboard,
    companies,
)
from app.core.config import settings
from app.core.database import create_indexes

# structured logging setup
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s %(levelname)s [%(name)s] [%(request_id)s] %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await create_indexes()
    except Exception as e:
        logging.getLogger(__name__).warning("DB index creation failed: %s", e)
    yield


app = FastAPI(
    title="Swift Agent Mobile API",
    description="Mobile-only backend for the Swift Agent companion app.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # mobile apps send requests from native, allow all
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

        response_status = 500

        async def send_wrapper(message):
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = message.get("status", 500)
            await send(message)

        await self.app(scope, receive, send_wrapper)

        duration_ms = (time.perf_counter() - start) * 1000
        self.logger.info("%s %s %d %.1fms", method, path, response_status, duration_ms)


# middleware order: request_id first, then logging
app.add_middleware(RequestIdMiddleware)
app.add_middleware(LoggingMiddleware)

def filter_router(original_router, allowed_paths: set):
    from fastapi import APIRouter
    new_router = APIRouter(tags=original_router.tags)
    for route in original_router.routes:
        if getattr(route, "path", None) in allowed_paths:
            new_router.routes.append(route)
    return new_router

# ── filtered routers ────────────────────────────────────────────────
filtered_auth = filter_router(auth.router, {"/otp/send", "/otp/verify", "/refresh", "/me"})
app.include_router(filtered_auth, prefix="/api/v1/auth")

app.include_router(mobile.router, prefix="/api/v1/mobile")

filtered_email = filter_router(email.router, {
    "/{company_id}/tickets",
    "/{company_id}/tickets/{ticket_id}",
    "/{company_id}/tickets/{ticket_id}/reply",
    "/{company_id}/tickets/{ticket_id}/seen",
})
app.include_router(filtered_email, prefix="/api/v1/email")

filtered_dashboard = filter_router(dashboard.router, {
    "/{company_id}/chats",
    "/{company_id}/chats/{chat_id}",
    "/{company_id}/chats/{chat_id}/seen",
})
app.include_router(filtered_dashboard, prefix="/api/v1/dashboard")


# ── global exception handler ────────────────────────────────────────
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


# ── health check ────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health_check():
    from app.core.database import mongo_client

    checks = {"api": "ok"}

    try:
        await mongo_client.admin.command("ping")
        checks["mongodb"] = "ok"
    except Exception as e:
        checks["mongodb"] = f"error: {e}"

    status_code = 200 if all(v == "ok" for v in checks.values()) else 503
    return JSONResponse(
        status_code=status_code, content={"status": "healthy", "checks": checks}
    )
