from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.routers import auth, knowledge, diagnosis, conversations, companies, dashboard, voice, billing
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # startup logic if needed
    yield
    # shutdown logic if needed

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

class WSCorsBypassMiddleware:
    """
    Middleware to bypass CORS for WebSocket endpoints.
    Starlette's CORSMiddleware rejects WS connections from unallowed origins.
    Since the voice widget is embedded on various websites, we need to allow all origins for it.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and scope.get("path", "").startswith("/api/v1/voice"):
            if "headers" in scope:
                scope["headers"] = [(k, v) for k, v in scope["headers"] if k.lower() != b"origin"]
        await self.app(scope, receive, send)

app.add_middleware(WSCorsBypassMiddleware)

app.include_router(auth.router, prefix="/api/v1/auth")
app.include_router(knowledge.router, prefix="/api/v1/knowledge")
app.include_router(diagnosis.router, prefix="/api/v1/diagnosis")
app.include_router(conversations.router, prefix="/api/v1/conversations")
app.include_router(companies.router, prefix="/api/v1/companies")
app.include_router(dashboard.router, prefix="/api/v1/dashboard")
app.include_router(billing.router, prefix="/api/v1/billing")

app.include_router(voice.router, prefix="/api/v1/voice")



@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
