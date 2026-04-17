import logging
import time
import uuid
from contextlib import asynccontextmanager

from app.core.logging_setup import install_default_log_record_fields

install_default_log_record_fields()
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.util import get_remote_address

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
    health,
)
from app.core.config import settings
from app.core.database import create_indexes, redis_client
from app.services.stroll_service import init_browser, close_browser
from app.services.stroll_scheduler import init_scheduler, close_scheduler

# Rate limiter
limiter = Limiter(key_func=get_remote_address)

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

    try:
        await init_browser()
    except Exception:
        logging.getLogger(__name__).warning(
            "Playwright browser init failed — stroll feature unavailable"
        )

    try:
        await init_scheduler()
    except Exception as e:
        logging.getLogger(__name__).error(f"Scheduler init failed: {e}")

    yield
    # shutdown: close Playwright browser and scheduler
    try:
        await close_scheduler()
    except Exception:
        pass

    try:
        await close_browser()
    except Exception:
        pass


app = FastAPI(
    title="Swift Agent API",
    description="Voice-first AI support platform for crypto applications.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — origins from config
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_hosts_list,
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
            # patch the logging filter via context
            import logging

            old_factory = logging.getLogRecordFactory()

            def record_factory(*args, **kwargs):
                record = old_factory(*args, **kwargs)
                record.request_id = request_id
                return record

            logging.setLogRecordFactory(record_factory)

        await self.app(scope, receive, send)


class WidgetCorsBypassMiddleware:
    """
    Middleware to bypass CORS for public widget endpoints.
    Starlette's CORSMiddleware rejects requests from unallowed origins.
    Since the voice widget is embedded on various websites, we strip the Origin
    header for widget-facing routes so they aren't blocked by the allowlist.
    """

    BYPASS_PREFIXES = (
        "/api/v1/voice",
        "/api/v1/chat",
        "/api/v1/public/stroll",
        "/api/v1/stroll",
        "/api/v1/email/inbound",
        "/api/v1/email/resolve",
    )
    BYPASS_SUFFIXES = ("/visitors/log",)

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if any(path.startswith(p) for p in self.BYPASS_PREFIXES) or any(
                path.endswith(s) for s in self.BYPASS_SUFFIXES
            ):
                if "headers" in scope:
                    scope["headers"] = [
                        (k, v) for k, v in scope["headers"] if k.lower() != b"origin"
                    ]
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


# middleware order matters — request_id first, then logging, then cors bypass
app.add_middleware(RequestIdMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(WidgetCorsBypassMiddleware)

# routers
app.include_router(auth.router, prefix="/api/v1/auth")
app.include_router(knowledge.router, prefix="/api/v1/knowledge")
app.include_router(diagnosis.router, prefix="/api/v1/diagnosis")
app.include_router(conversations.router, prefix="/api/v1/conversations")
app.include_router(companies.router, prefix="/api/v1/companies")
app.include_router(dashboard.router, prefix="/api/v1/dashboard")
app.include_router(billing.router, prefix="/api/v1/billing")

app.include_router(voice.router, prefix="/api/v1/voice")
app.include_router(chat.router, prefix="/api/v1/chat")
app.include_router(stroll.router, prefix="/api/v1/stroll")
app.include_router(stroll_public.router, prefix="/api/v1/public/stroll")
app.include_router(email.router, prefix="/api/v1/email")
app.include_router(health.router, prefix="/api/v1")

# Rate limiting state
app.state.limiter = limiter


# global exception handler
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
    return JSONResponse(status_code=status_code, content={"status": "healthy", "checks": checks})
