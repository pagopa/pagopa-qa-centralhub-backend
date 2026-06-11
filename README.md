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
│   ├── gpd_position.py     # GPD Positions Data Hub: GET /snapshots, POST /sync
│   ├── docs.py             # Docs & Knowledge Base CRUD + HTML proxy
│   ├── users.py            # GET/PATCH /api/v1/users, POST /api/v1/users/sync-login
│   ├── roles.py            # GET /api/v1/roles (role/permission matrix), PATCH /api/v1/roles/{role}
│   └── ...
├── core/
│   ├── permissions.py      # ACTION_CATALOG, ACTION_KEYS, EDITABLE_ROLES, compute_role_matrix
│   └── ...                 # DB engine, security helpers
├── models/                 # SQLAlchemy ORM models (incl. Role, User.role FK)
├── schemas/
│   ├── bdd.py              # BDD request/response models (SettingsOut, ScenarioOut, ...)
│   ├── gpd_position.py     # GpdPositionSnapshotOut, GpdPositionSyncStatusOut, ...
│   ├── user.py             # UserOut, UserListResponse, UserUpdate, sync-login schemas
│   ├── role.py             # RoleOut, RoleMatrixResponse, RolePermissionUpdate
│   └── ...
├── services/
│   ├── bdd.py              # BDD CRUD + settings encryption/decryption
│   ├── bdd_ai.py           # AI provider abstraction (Ollama, Claude)
│   ├── bdd_parsers.py      # Source parsers (Confluence, PDF, DOCX, text)
│   ├── jira.py             # JiraClient (board/queue/jql), compute_overview, compute_trend
│   ├── gpd_position.py     # parse_report_line, sync_from_source, list_snapshots, get_sync_status
│   ├── github.py           # GitHubClient (Contents API + Actions API: list_workflow_runs, get_job_log)
│   ├── users.py            # sync_login, list_users, update_user
│   ├── roles.py            # get_role_matrix, update_role_permissions
│   └── ...
└── tasks/                  # Celery/Arq background workers (sync_e2e, sync_gpd_position, ...)
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

### GPD Positions Data Hub

Snapshot giornalieri delle posizioni debitorie GPD, sincronizzati dai run del workflow `gpd_report.yml` del repo `pagopa/pagopa-qa` (richiede `GITHUB_TOKEN` con accesso alle Actions di quel repo).

- `GET /api/v1/gpd-position/snapshots`: lista snapshot ordinati per `report_date` + stato ultimo sync
- `POST /api/v1/gpd-position/sync`: trigger sync manuale (503 se manca `GITHUB_TOKEN`, 502 su errori GitHub API)
- `app/services/gpd_position.py`: `sync_from_source` itera i run di successo dal più recente, scarica e concatena i log di **tutti** i job di ciascun run, estrae la riga `report data {dict}` con `parse_report_line` (regex + `ast.literal_eval`) e mappa i campi su `GpdPositionSnapshot`. Backfill di 90 giorni al primo sync, poi incrementale (si ferma al primo `run_id` già noto)
- Task Celery `sync_gpd_position_snapshots` schedulato ogni 24h (vedi `app/tasks/schedule.py`)

Vedi `../README.md` per il dettaglio dell'algoritmo di sync.

### Auth & RBAC

L'autenticazione (Google SSO) e l'enforcement dei permessi avvengono lato `qa-hub-frontend`; il backend espone i dati su cui si basa la RBAC e resta volutamente "open" (nessun middleware di auth sulle route API).

- **`roles`** (key, label, is_system, permissions JSON, updated_at) e **`users`** (con FK `role` → `roles.key`, default `"guest"`): tabelle introdotte dalla migration `0008`, che le seed con i 5 ruoli di sistema (`superadmin`, `qa_manager`, `qa_analyst`, `qa_engineer`, `guest`).
- **`app/core/permissions.py`**: `ACTION_CATALOG` definisce le 10 azioni RBAC (es. `view:bdd`, `manage:bdd`, `sync:trigger`, ...) raggruppate per categoria, `ACTION_KEYS` la lista delle chiavi, `EDITABLE_ROLES` i ruoli non di sistema editabili da `superadmin`, `compute_role_matrix` calcola la matrice ruolo→permessi.
- **`POST /api/v1/users/sync-login`**: chiamata dal frontend ad ogni login/refresh JWT — crea o aggiorna l'utente (email, nome, idp_sub) e restituisce ruolo e stato attivo.
- **`GET/PATCH /api/v1/users`**: lista utenti e aggiornamento ruolo/stato (`is_active`), usati dalla tab "Utenti" di `/settings/users`.
- **`GET /api/v1/roles`**: restituisce `RoleMatrixResponse` (catalogo azioni, ruoli, matrice permessi) usato da `usePermissions`/`useRoleMatrix` nel frontend.
- **`PATCH /api/v1/roles/{role}`**: aggiorna i permessi di un ruolo non di sistema (usato dalla tab "Ruoli & Permessi").

### Docs HTML proxy

`GET /api/v1/docs/proxy?url=<url>` fetches the page server-side, converts GitHub blob URLs to raw.githubusercontent.com, injects `<base href>`, strips `X-Frame-Options`, and returns an `HTMLResponse`. Used to embed external HTML pages as iframes without CSP/CORS issues.

## Design reference

See `../design_handoff_qa_webportal/README.md` for the full spec and design system.
