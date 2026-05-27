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
│   ├── e2e.py              # E2E suites and runs (sync, CRUD, bulk delete)
│   ├── jira.py             # Jira KPI: /overview /trend /sanp/overview /sanp/trend /data/overview /data/trend
│   ├── docs.py             # Docs & Knowledge Base CRUD + HTML proxy
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
│   ├── jira.py             # JiraClient (board/queue/jql), compute_overview, compute_trend
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

### Jira integration

Requires `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env`.

- **Testing board** (`BOARD_TESTING = 597`): fetched via Agile board API
- **SANP/Data queues** (`QUEUE_SANP = 1919`, `QUEUE_DATA = 1416`, `SD_PIDM = 85`): two-step fetch — issue keys from JSM Queue API, full details from `POST /rest/api/3/search/jql`
- `compute_overview` returns status/component/type/assignee breakdowns and 5 alert lists (no_estimate, backlog_old, blocked_old, open_old, in_progress_old)
- `open_old`: issue in open/pending statuses with no update for > 5 days
- `in_progress_old`: issue in progress/review statuses with no update for > 10 days

### Docs HTML proxy

`GET /api/v1/docs/proxy?url=<url>` fetches the page server-side, converts GitHub blob URLs to raw.githubusercontent.com, injects `<base href>`, strips `X-Frame-Options`, and returns an `HTMLResponse`. Used to embed external HTML pages as iframes without CSP/CORS issues.

## Design reference

See `../design_handoff_qa_webportal/README.md` for the full spec and design system.
