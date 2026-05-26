# QA Hub — Backend

FastAPI backend for QA Hub. Serves the REST API consumed by `qa-hub-frontend`.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Docker + Docker Compose (for Postgres + Redis)

## Setup

```bash
# Install uv (if not installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Start Postgres + Redis
docker-compose up -d postgres redis

# Copy env and fill in values
cp .env.example .env

# Run database migrations
uv run alembic upgrade head

# Start dev server
uv run uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs  
OpenAPI JSON: http://localhost:8000/openapi.json

## Run tests

```bash
uv run pytest
```

## Lint + type-check

```bash
uv run ruff check .
uv run mypy .
```

## Project structure

```
app/
├── main.py                 # FastAPI app factory + middleware
├── config.py               # pydantic-settings
├── deps.py                 # Shared FastAPI dependencies
├── api/v1/
│   ├── bdd.py              # Gherkin Generator: projects, scenarios, generate (SSE), settings, Ollama controls
│   ├── e2e.py              # E2E suites and runs
│   ├── jira.py             # Jira KPI
│   └── ...
├── core/                   # DB engine, auth, security helpers
├── models/                 # SQLAlchemy ORM models
├── schemas/
│   ├── bdd.py              # BDD request/response models (SettingsOut, ScenarioOut, ...)
│   └── ...
├── services/
│   ├── bdd.py              # BDD CRUD + settings encryption/decryption
│   ├── bdd_ai.py           # AI provider abstraction (Ollama, Claude)
│   ├── bdd_parsers.py      # Source parsers (Confluence, PDF, DOCX, text)
│   └── ...
└── tasks/                  # Celery/Arq background workers
alembic/                    # DB migration scripts
tests/                      # pytest test suite
docker-compose.yml
```

## Environment variables

See `.env.example` for the full list. Key variables:

```env
DATABASE_URL=postgresql+asyncpg://postgres:password@localhost:5432/qa_hub
SECRET_KEY=<genera con: openssl rand -hex 32>   # usato per cifrare API key BDD in DB
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
CORS_ORIGINS=["http://localhost:3000"]
```

BDD settings (AI provider, Confluence token, Claude API key) are stored encrypted in the database and managed via `PUT /api/v1/bdd/settings`. They are **not** read from environment variables.

## Design reference

See `../design_handoff_qa_webportal/README.md` for the full spec and design system.
