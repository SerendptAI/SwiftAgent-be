import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.routers import auth, knowledge, diagnosis, conversations, companies, dashboard, voice, billing, chat, stroll, stroll_public
from app.services.stroll_service import init_browser, close_browser
from app.services.stroll_scheduler import init_scheduler, close_scheduler

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await init_browser()
    except Exception:
        logging.getLogger(__name__).warning("Playwright browser init failed — stroll feature unavailable")
        
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
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://swiftagents.org",
        "https://www.swiftagents.org",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class WidgetCorsBypassMiddleware:
    """
    Middleware to bypass CORS for public widget endpoints.
    Starlette's CORSMiddleware rejects requests from unallowed origins.
    Since the voice widget is embedded on various websites, we strip the Origin
    header for widget-facing routes so they aren't blocked by the allowlist.
    """
    BYPASS_PREFIXES = ("/api/v1/voice", "/api/v1/chat", "/api/v1/public/stroll", "/api/v1/stroll")
    BYPASS_SUFFIXES = ("/visitors/log",)

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if any(path.startswith(p) for p in self.BYPASS_PREFIXES) or \
               any(path.endswith(s) for s in self.BYPASS_SUFFIXES):
                if "headers" in scope:
                    scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"origin"]
        await self.app(scope, receive, send)

app.add_middleware(WidgetCorsBypassMiddleware)

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



@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
