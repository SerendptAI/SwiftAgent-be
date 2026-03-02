from pathlib import Path
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.api.routers import auth, knowledge, diagnosis, conversations, companies, dashboard, widget
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

app.include_router(auth.router, prefix="/api/v1/auth")
app.include_router(knowledge.router, prefix="/api/v1/knowledge")
app.include_router(diagnosis.router, prefix="/api/v1/diagnosis")
app.include_router(conversations.router, prefix="/api/v1/conversations")
app.include_router(companies.router, prefix="/api/v1/companies")
app.include_router(dashboard.router, prefix="/api/v1/dashboard")
app.include_router(widget.router, prefix="/api/v1/widget")

app.mount("/static", StaticFiles(directory=Path(__file__).parent / "app" / "static"), name="static")

@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy"}
