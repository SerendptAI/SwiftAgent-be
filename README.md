# SwiftAgent-be

Voice-first AI support platform backend for crypto applications. Built with FastAPI, MongoDB, Qdrant, and multi-provider AI (Anthropic Claude + Google Gemini).

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Application                   │
├──────────┬──────────┬──────────┬──────────┬─────────────┤
│  Auth    │  Chat    │  Voice   │ Knowledge│  Dashboard  │
│  (OAuth) │  (SSE)   │  (WS)    │  (RAG)   │  (REST)     │
├──────────┴──────────┴──────────┴──────────┴─────────────┤
│                     Service Layer                        │
│  ┌────────────┐ ┌────────────┐ ┌──────────────────────┐ │
│  │ AI Agents  │ │ Blockchain │ │ Stroll (Playwright)  │ │
│  │ Claude     │ │ EVM/BTC    │ │ BFS Crawler + Vision │ │
│  │ Gemini     │ │ Diagnosis  │ │ APScheduler          │ │
│  └────────────┘ └────────────┘ └──────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│  MongoDB (motor)     │  Qdrant (vector)   │  Cloudinary │
└─────────────────────────────────────────────────────────┘
```

## Features

- **OAuth Authentication** — Google OAuth 2.0 with JWT access/refresh tokens
- **AI Chat** — Streaming SSE endpoint with Anthropic Claude or Google Gemini
- **Voice Calls** — Real-time WebSocket call sessions with Fish Audio STT/TTS
- **Knowledge Base** — RAG pipeline with Gemini embeddings and Qdrant vector search
- **Blockchain Diagnosis** — On-chain transaction analysis across EVM chains and Bitcoin
- **Dashboard Stroll** — Automated BFS crawling of customer dashboards with Playwright + Claude vision
- **File Uploads** — PDF/DOCX/TXT document ingestion via Cloudinary

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- MongoDB instance (local or Atlas)
- Qdrant instance
- API keys for Google OAuth, Anthropic, Gemini, Cloudinary

### Setup

```bash
# 1. Clone and install dependencies
uv sync

# 2. Install Playwright browsers (for stroll feature)
uv run playwright install chromium

# 3. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 4. Run the server
uv run uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

### Development

```bash
# Run with dev dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=app --cov-report=term-missing

# Lint
uv run ruff check .

# Type check
uv run mypy app/
```

## Project Structure

```
├── main.py                    # FastAPI app, middleware, lifespan
├── app/
│   ├── core/
│   │   ├── config.py          # Pydantic settings with validation
│   │   ├── security.py        # JWT creation/validation
│   │   ├── auth.py            # FastAPI auth dependency
│   │   ├── database.py        # MongoDB + Qdrant clients + indexes
│   │   ├── cache.py           # TTL cache layer
│   │   └── rate_limiter.py    # Token bucket rate limiter
│   ├── api/routers/
│   │   ├── auth.py            # OAuth, JWT, user profile
│   │   ├── chat.py            # SSE streaming chat
│   │   ├── voice.py           # WebSocket voice calls
│   │   ├── knowledge.py       # Knowledge base CRUD + upload
│   │   ├── conversations.py   # Conversation history
│   │   ├── companies.py       # Company onboarding/settings
│   │   ├── dashboard.py       # Dashboard stats + visitors
│   │   ├── diagnosis.py       # Transaction diagnosis
│   │   ├── billing.py         # Subscription management
│   │   ├── stroll.py          # Stroll management (auth)
│   │   └── stroll_public.py   # Widget stroll reports (public)
│   ├── services/
│   │   ├── anthropic_agent_service.py  # Claude agent with tools
│   │   ├── gemini_agent_service.py     # Gemini agent with tools
│   │   ├── knowledge_service.py        # Embedding + vector search
│   │   ├── company_service.py          # Company CRUD + caching
│   │   ├── dashboard_service.py        # Stats aggregation
│   │   ├── chain_service.py            # Transaction diagnosis
│   │   ├── cloudinary_service.py       # File uploads
│   │   ├── fish_audio_service.py       # STT/TTS
│   │   ├── text_extraction_service.py  # PDF/DOCX parsing
│   │   ├── stroll_service.py           # BFS dashboard crawler
│   │   ├── stroll_scheduler.py         # APScheduler cron jobs
│   │   ├── stroll_index_service.py     # Navigation reports
│   │   └── blockchain/                 # EVM, Bitcoin, prices
│   └── models/                         # Pydantic request/response models
└── tests/                              # Test suite
```

## API Endpoints

All endpoints are prefixed with `/api/v1`.

| Router | Auth | Description |
|--------|------|-------------|
| `POST /auth/login` | Public | Initiate Google OAuth |
| `GET /auth/callback` | Public | OAuth callback, returns JWT |
| `POST /auth/refresh` | Public | Refresh access token |
| `GET /auth/me` | Bearer | Current user profile |
| `PATCH /auth/me` | Bearer | Update user profile |
| `POST /{company_id}/chat` | Public | Streaming chat (SSE) |
| `WS /{company_id}/call` | Public | Voice call (WebSocket) |
| `POST /knowledge/` | Bearer | Ingest knowledge document |
| `GET /knowledge/` | Bearer | List documents (paginated) |
| `POST /knowledge/upload` | Bearer | Upload file (PDF/DOCX/TXT) |
| `POST /knowledge/query` | Bearer | Semantic search |
| `POST /conversations/` | Bearer | Save conversation |
| `GET /conversations/` | Bearer | List conversations (paginated) |
| `POST /companies/` | Bearer | Create company |
| `GET /companies/{id}` | Bearer | Company details |
| `PATCH /companies/{id}/*` | Bearer | Update company settings |
| `GET /companies/{id}/public` | Public | Public company info |
| `GET /dashboard/{id}/stats` | Bearer | Dashboard statistics |
| `GET /dashboard/{id}/visitors` | Bearer | Visitor list (paginated) |
| `GET /dashboard/{id}/chats` | Bearer | Chat sessions (paginated) |
| `POST /diagnosis/` | Bearer | Diagnose transaction |
| `GET /billing/{id}` | Bearer | Billing details |
| `POST /billing/{id}/subscribe` | Bearer | Subscribe to plan |
| `POST /stroll/{id}/run` | Bearer | Trigger dashboard crawl |
| `GET /stroll/{id}/versions` | Bearer | List stroll versions |
| `POST /public/stroll/{id}/report` | Public | Submit widget stroll report |

## Configuration

All configuration is via environment variables. See `.env.example` for the full list.

Key settings:
- `ENVIRONMENT` — `development` or `production` (controls OAuth security settings)
- `LOG_LEVEL` — `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- `RATE_LIMIT_PER_MINUTE` — General API rate limit (default: 60)
- `RATE_LIMIT_AUTH_PER_MINUTE` — Auth endpoint rate limit (default: 10)
- `MAX_UPLOAD_SIZE_BYTES` — Max file upload size (default: 50MB)

## Deployment

```bash
# Build Docker image
docker build -t swift-agent-be .

# Run
docker run -p 8000:8000 --env-file .env swift-agent-be
```

## License

Private — All rights reserved.
