# Release Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the pagoPA status page into QA Hub as a "Release Status" section with a UI-managed catalog (area → product → application), a 15-minute + manual version sync, deploy history, and linkage of deployed versions to NRT / QG / TAS runs.

**Architecture:** Backend gets five `sp_*` tables (catalog, current state, deploy events, sync status) seeded with the current status page catalog, a pure semver/drift module, a sync service guarded by an advisory lock and run by a Celery task (beat every 900 s, manual trigger enqueued with `apply_async`), a test-linkage query over `qachub.test_suites`/`test_runs`, and the `/api/v1/releases` router. Frontend replaces the `/releases` stub with the status view, an application detail sheet, a catalog management page, and an Overview KPI.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Celery 5, httpx, pytest (asyncio auto); Next.js 16.4, React 19, TanStack Query, Tailwind, lucide-react 1.16, vitest, pnpm.

**Spec:** `docs/superpowers/specs/2026-11-16-release-status-design.md` (backend repo).

## Global Constraints

- Two repos: `pagopa-qa-centralhub-backend` (tasks 1–9) and `pagopa-qa-centralhub-frontend` (tasks 10–15). In each repo create `feature/release-status` from an up-to-date `main` (`git checkout main && git pull --ff-only`). Backend is on `fix/pending-runs-window`, frontend on `fix/flaky-exclude-quality-gate`: if either has uncommitted changes, stop and ask the user.
- **Commits:** the user commits. Each "Commit" step means: run the checks, show branch, changed files, diff summary and a Conventional Commit message, and wait. Never run `git commit`/`git push`. No AI attribution trailers.
- Everything in the repo is English (code, comments, logs, commit messages). UI copy is Italian.
- Tables use the `sp_` prefix in the default schema; the existing `releases` table / `Release` model is not touched.
- Targets: `REPO`, `DEV`, `UAT`, `PROD`. Read statuses: `OK`, `ERROR`, `EMPTY_BODY`, `NO_VERSION`, `NO_RELEASE`. Areas: `CORE`, `TOUCHPOINT`. Runtime `AKS|AZ`, CI `GHA|AZDO`, kind `backend|frontend`.
- Sync statuses `QUEUED|RUNNING|OK|PARTIAL|FAILED`; triggers `SCHEDULED|MANUAL`. Stale: `QUEUED` > 10 min, `RUNNING` > 30 min.
- Settings defaults: `status_page_dev_base_url="https://api.dev.platform.pagopa.it"`, `status_page_uat_base_url="https://api.uat.platform.pagopa.it"`, `status_page_prod_base_url="https://api.platform.pagopa.it"`, `status_page_info_path="/shared/statuspage/v1/info"`, `status_page_sync_concurrency=10`, `status_page_http_timeout_s=10.0`, `status_page_sync_interval_s=900.0`. GitHub uses `settings.github_token`.
- Application `DRIFT` = PROD `BEHIND` or `AHEAD` of REPO (same as the old site). `ALIGNED` = PROD `EQUAL` and every target `OK`. `UNREACHABLE` = any target `read_status != OK`. `UNTESTED` = app has `test_object` and UAT or PROD deployed version has no linked run.
- Run kind from `test_origin`: `QG` → QG, `TAS` → TAS, anything else → NRT. Hub links: `/quality-gate/report/{id}`, `/tas-nrts/report/{id}`, `/nrts/report/{id}`.
- Permissions `view:status_page` (all four roles true) and `manage:status_page` (only `qa_manager`), category "Release Status". Manual sync uses `sync:trigger`. Feature key `release_status`, route `/releases`, label "Release Status". Enforced in the frontend only; writes carry `user` (email) in the body.
- Follow the backend service style: `structlog` logger, `try/except → logger.exception(...); raise`, "query succeeded with 0 results" info logs.
- Backend checks: `uv run ruff check .`, `uv run mypy .`, `uv run pytest`. Frontend checks: `pnpm lint`, `pnpm test`, `pnpm build`.
- After backend API changes regenerate `openapi/qa-hub-be.json` with `uv run --extra dev python -m scripts.generate_openapi --output openapi/qa-hub-be.json`.

## Review Focus

1. **A target that errors and then comes back with the same version** must not create a deploy event, and its cell must keep showing the last good version greyed — tested in Task 4.
2. **A beat run and a manual click at the same time** must not both run: the click gets 409 while one is QUEUED/RUNNING, the second task exits on the advisory lock, and a stale row (dead worker) does not block forever — tested in Tasks 5 and 8.
3. **Versions with a `v` prefix or a pre-release suffix** (`v1.4.2`, `1.4.3-1-feature`) must compare correctly and match `test_object_version` after normalization — tested in Tasks 2 and 6.
4. **Two applications sharing one `test_object`** (service and Helpdesk variant) must both show the runs — tested in Task 6.
5. **Deleting a product that still has applications** must be refused with a clear message, not cascade — tested in Task 7 (API) and Task 14 (UI).

---

## Backend (`pagopa-qa-centralhub-backend`)

### Task 1: Permissions, feature catalog and settings

**Files:**
- Modify: `app/core/permissions.py` (`ACTION_CATALOG`, new category "Release Status" at the end)
- Modify: `app/core/features.py` (`FEATURE_CATALOG`, after `sanp_health`)
- Modify: `app/config.py` (`Settings`)
- Test: `tests/test_permissions_catalog.py`, `tests/test_feature_visibility_service.py`, `tests/test_status_page_config.py`

**Interfaces:**
- Produces: action keys `view:status_page`, `manage:status_page`; feature `release_status`; settings fields from Global Constraints.

- [ ] **Step 1: Write failing tests**

```python
def test_status_page_actions_defaults() -> None:
    by_key = {e["key"]: e for e in ACTION_CATALOG}
    assert by_key["view:status_page"]["defaults"] == {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": True}
    assert by_key["manage:status_page"]["defaults"] == {"qa_manager": True, "qa_analyst": False, "qa_engineer": False, "guest": False}
    assert by_key["view:status_page"]["category"] == "Release Status"

def test_release_status_feature_registered() -> None:
    f = next(f for f in FEATURE_CATALOG if f["key"] == "release_status")
    assert (f["route"], f["action"], f["label"], f["external"]) == ("/releases", "view:status_page", "Release Status", False)

def test_status_page_settings_defaults() -> None:
    s = Settings()
    assert s.status_page_prod_base_url == "https://api.platform.pagopa.it"
    assert (s.status_page_info_path, s.status_page_sync_concurrency, s.status_page_sync_interval_s) == ("/shared/statuspage/v1/info", 10, 900.0)
```

- [ ] **Step 2: Run** `uv run pytest tests/test_permissions_catalog.py tests/test_feature_visibility_service.py tests/test_status_page_config.py -v` → FAIL. Tests asserting catalog sizes need their expected counts updated.
- [ ] **Step 3: Add entries** (labels "Release Status (visualizza)", "Release Status (gestione catalogo)") and settings. No role migration: `compute_role_matrix` falls back to `defaults` for missing keys.
- [ ] **Step 4: Run** the same tests, then `uv run pytest` → PASS.
- [ ] **Step 5: Commit** — `feat: add release status permissions, feature and settings`.

### Task 2: Version comparison module (pure)

**Files:**
- Create: `app/services/status_page_versions.py`
- Test: `tests/test_status_page_versions.py`

**Interfaces:**
- Produces:
  - `class Drift(StrEnum): EQUAL; BEHIND; AHEAD; UNKNOWN`
  - `def normalize_version(raw: str) -> str` — trim, strip leading `Release `, strip leading `v`/`V`.
  - `def compare_versions(a: str, b: str) -> int | None` — `-1/0/1` per SemVer 2.0 precedence on normalized values (build metadata ignored), `None` if either does not parse as `MAJOR.MINOR.PATCH[-pre][+build]`.
  - `def drift(env_version: str | None, repo_version: str | None) -> Drift` — `BEHIND` when env < repo.

- [ ] **Step 1: Write failing tests**

```python
@pytest.mark.parametrize("a,b,expected", [
    ("1.4.2", "1.4.2", 0), ("v1.4.2", "1.4.2", 0), ("Release 1.4.2", "1.4.2", 0),
    ("2.0.0", "1.9.9", 1), ("1.9.9", "2.0.0", -1),          # major sign (old Flutter bug)
    ("1.10.0", "1.9.0", 1), ("1.4.10", "1.4.9", 1),
    ("1.4.3-1-feature", "1.4.3", -1), ("1.4.3-1-feature", "1.4.2", 1),
    ("1.0.0-alpha", "1.0.0-alpha.1", -1), ("1.0.0-alpha.1", "1.0.0-beta", -1), ("1.0.0+build.5", "1.0.0", 0),
    ("latest", "1.0.0", None), ("1.0", "1.0.0", None), ("", "1.0.0", None),
])
def test_compare_versions(a, b, expected): assert compare_versions(a, b) == expected

def test_drift():
    assert drift("1.4.1", "1.4.2") is Drift.BEHIND
    assert drift("1.4.3-1-feature", "1.4.2") is Drift.AHEAD
    assert drift("1.4.2", "v1.4.2") is Drift.EQUAL
    assert drift(None, "1.4.2") is Drift.UNKNOWN and drift("abc", "1.4.2") is Drift.UNKNOWN
```

- [ ] **Step 2: Run** `uv run pytest tests/test_status_page_versions.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement.** Hand-written parser (regex `^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$`); pre-release identifiers compared per SemVer §11 (numeric < alphanumeric, numeric compared as ints, shorter list lower when prefix equal). No new dependency.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: add semver comparison for release status`.

### Task 3: Models and migration 0032 with catalog seed

**Files:**
- Create: `app/models/status_page.py`
- Modify: `app/models/__init__.py` (export new models, as the other features do)
- Create: `alembic/versions/0032_status_page_tables.py` (`revision="0032"`, `down_revision="0031"`)
- Test: `tests/test_status_page_models.py`

**Interfaces:**
- Produces (default schema, columns per spec §5):
  - `SpProduct` (`sp_products`): `id` UUID PK, `name` str(100) unique, `area` str(20) check `ck_sp_products_area`, `created_at`, `updated_at`; relationship `applications`.
  - `SpApplication` (`sp_applications`): `id`, `product_id` FK `ON DELETE RESTRICT`, `name` str(150), `product_key` str(100) unique, `repository` str(200), `pipeline_url` str(500)|None, `runtime`, `ci`, `kind` (checks `ck_sp_applications_runtime|ci|kind`), `test_object` str(200)|None indexed, `ea_node_id` str(200)|None FK `ea_nodes.id ON DELETE SET NULL`, timestamps. `UniqueConstraint("product_id", "name", name="uq_sp_applications_product_name")`.
  - `SpVersionState` (`sp_version_state`): PK `(application_id, target)`, FK `ON DELETE CASCADE`, `version` str(100)|None, `read_status` str(20), `error_detail` str(500)|None, `checked_at`, `last_ok_at`|None, `version_since`|None. Checks on `target`, `read_status`.
  - `SpDeployEvent` (`sp_deploy_events`): `id`, `application_id` FK cascade, `target`, `previous_version`|None, `version`, `detected_at`, `previous_seen_at`|None, `is_baseline` bool default false. Index `ix_sp_deploy_events_app_target_detected (application_id, target, detected_at)`.
  - `SpSyncStatus` (`sp_sync_status`): singleton `id=1` (`ck_sp_sync_status_singleton`), fields per spec §5.5, `errors` JSONB default `[]`.
  - Migration module constants: `SEED_PRODUCTS: list[tuple[str, str]]` (name, area) and `SEED_APPLICATIONS: list[dict[str, str]]` (keys `product`, `name`, `product_key`, `repository`, `pipeline_url`, `runtime`, `ci`, `kind`).

- [ ] **Step 1: Write failing tests** (real DB via `tests/_db.py`, style of `tests/test_sanp_health_models.py`; load the migration with `importlib.util.spec_from_file_location`)

```python
def test_seed_matches_status_page_catalog() -> None:
    assert len(mig.SEED_PRODUCTS) == 14 and len(mig.SEED_APPLICATIONS) == 53
    assert sum(1 for _, a in mig.SEED_PRODUCTS if a == "TOUCHPOINT") == 1
    assert "GPS - Donations" not in {p for p, _ in mig.SEED_PRODUCTS} | {a["product"] for a in mig.SEED_APPLICATIONS}
    keys = [a["product_key"] for a in mig.SEED_APPLICATIONS]; assert len(keys) == len(set(keys))
    pdf = [a for a in mig.SEED_APPLICATIONS if a["name"] == "PDF Engine"]
    assert {a["product"] for a in pdf} == {"Ricevute PDF", "Stampa Avvisi"}

async def test_same_name_allowed_in_two_products(db) -> None: ...   # insert succeeds
async def test_same_name_in_same_product_rejected(db) -> None: ...  # IntegrityError
async def test_product_with_applications_cannot_be_deleted(db) -> None: ...  # IntegrityError (RESTRICT)
async def test_application_delete_cascades_state_and_events(db) -> None: ...
async def test_ea_node_delete_sets_null(db) -> None: ...
```

- [ ] **Step 2: Run** `uv run pytest tests/test_status_page_models.py -v` → FAIL.
- [ ] **Step 3: Implement models and migration.** Seed data is transcribed by hand from `pagopa-status-page/lib/constant.dart` (`projectsCore` → `CORE`, `projectsTouchPoint` → `TOUCHPOINT`; field map `product→product_key`, `pipeline→pipeline_url`, `env→runtime`, `actions→ci`, `type→kind` default `backend`; skip the commented-out "GPS - Donations" block; drop `host`). `upgrade()` creates the tables then inserts via `op.bulk_insert`; `downgrade()` drops the five tables.
- [ ] **Step 4: Run** `uv run alembic upgrade head`, `uv run alembic downgrade -1 && uv run alembic upgrade head` → no errors; tests → PASS.
- [ ] **Step 5: Commit** — `feat: add release status tables seeded from the status page catalog`.

### Task 4: Source readers and sync service

**Files:**
- Create: `app/services/status_page_sources.py` (HTTP reads)
- Create: `app/services/status_page_sync.py` (orchestration, events, status row)
- Test: `tests/test_status_page_sources.py`, `tests/test_status_page_sync.py`

**Interfaces:**
- Consumes: Task 2 `normalize_version`; Task 3 models.
- Produces:
  - `@dataclass(frozen=True) class Reading: target: str; read_status: str; version: str | None; error_detail: str | None`
  - `async def read_repo(client: httpx.AsyncClient, repository: str) -> Reading`
  - `async def read_env(client: httpx.AsyncClient, target: str, product_key: str) -> Reading` (base URL from settings by target)
  - `def apply_reading(state: SpVersionState | None, reading: Reading, now: datetime, application_id: uuid.UUID) -> tuple[SpVersionState, SpDeployEvent | None]` — pure event rules.
  - `SYNC_LOCK_ID: int` (new constant, distinct from other lock ids in the repo)
  - `class StatusPageSyncInProgressError(Exception)`
  - `async def run_sync(db: AsyncSession, *, trigger: str, requested_by: str | None) -> SpSyncStatus`
  - `async def request_manual_sync(db: AsyncSession, *, user: str, now: datetime | None = None) -> SpSyncStatus` — raises `StatusPageSyncInProgressError` when not stale; sets `QUEUED`, commits. The caller enqueues.
  - `async def get_sync_status(db: AsyncSession) -> SpSyncStatus | None`

- [ ] **Step 1: Write failing tests for readers** (`httpx.MockTransport`)

```python
# read_env
200 {"version": "1.4.2"}            -> Reading("DEV", "OK", "1.4.2", None)
200 b""                              -> read_status "EMPTY_BODY"
200 {"name": "x"}                    -> "NO_VERSION"
503                                  -> "ERROR", error_detail "HTTP 503"
httpx.ConnectTimeout twice           -> "ERROR" (exactly 2 attempts); once then 200 -> "OK"
URL called == "https://api.uat.platform.pagopa.it/shared/statuspage/v1/info?product=afmcalculator" for target "UAT"
# read_repo
200 {"tag_name": "Release 1.4.2"}    -> "OK", version "1.4.2"
404                                  -> "NO_RELEASE"
403 with header x-ratelimit-remaining: 0 -> "ERROR", error_detail "GitHub rate limit"
Authorization header == f"Bearer {settings.github_token}"
```

- [ ] **Step 2: Write failing tests for `apply_reading`** (pure)

```python
def test_first_ok_is_baseline(): state, ev = apply_reading(None, ok("1.4.1"), T0, APP); assert ev.is_baseline and ev.previous_version is None and state.version_since == T0
def test_same_version_no_event(): _, ev = apply_reading(st("1.4.1", last_ok=T0), ok("1.4.1"), T1, APP); assert ev is None
def test_error_keeps_last_version_no_event():
    state, ev = apply_reading(st("1.4.1", last_ok=T0), err(), T1, APP)
    assert ev is None and state.version == "1.4.1" and state.read_status == "ERROR" and state.last_ok_at == T0 and state.checked_at == T1
def test_error_then_new_version_one_event():
    s1, _ = apply_reading(st("1.4.1", last_ok=T0), err(), T1, APP); s2, ev = apply_reading(s1, ok("1.4.2"), T2, APP)
    assert (ev.previous_version, ev.version, ev.previous_seen_at, ev.detected_at, ev.is_baseline) == ("1.4.1", "1.4.2", T0, T2, False)
def test_first_read_error_creates_state_without_version(): state, ev = apply_reading(None, err(), T0, APP); assert ev is None and state.version is None
def test_downgrade_is_event(): _, ev = apply_reading(st("1.4.2", last_ok=T0), ok("1.4.1"), T1, APP); assert ev.version == "1.4.1"
def test_no_release_is_not_ok(): state, ev = apply_reading(None, Reading("REPO", "NO_RELEASE", None, None), T0, APP); assert ev is None
```

- [ ] **Step 3: Write failing tests for `run_sync` / `request_manual_sync`** (real DB, readers patched with `AsyncMock`)
  - two apps, all OK → status `OK`, 8 state rows, 8 baseline events, `applications_checked=2`, `reads_failed=0`, `last_success_at` set;
  - one reading ERROR → `PARTIAL`, `reads_failed=1`, `errors` has one entry naming app and target;
  - DB failure during persist → status `FAILED` recorded in a separate transaction, exception re-raised;
  - advisory lock held by another session → `run_sync` returns without touching the status row;
  - `request_manual_sync` with status `RUNNING` started 5 min ago → raises; started 31 min ago → returns `QUEUED`, `trigger="MANUAL"`, `requested_by` set; `QUEUED` 11 min ago → not blocking.
- [ ] **Step 4: Run** `uv run pytest tests/test_status_page_sources.py tests/test_status_page_sync.py -v` → FAIL.
- [ ] **Step 5: Implement.** One `httpx.AsyncClient(timeout=settings.status_page_http_timeout_s)`; `asyncio.Semaphore(settings.status_page_sync_concurrency)` around each read; `asyncio.gather` over apps × 4 targets. Retry once only on `httpx.TimeoutException`/`httpx.TransportError`. Lock via `pg_try_advisory_lock(SYNC_LOCK_ID)` on a dedicated connection, released in `finally`. `errors` capped at 20 entries (`"{app name} {target}: {detail}"`). Persist states and events in one transaction; failure path mirrors `sanp_health._record_failed_sync_status`.
- [ ] **Step 6: Run** → PASS.
- [ ] **Step 7: Commit** — `feat: sync release versions from GitHub and the statuspage endpoint`.

### Task 5: Celery task and 15-minute schedule

**Files:**
- Create: `app/tasks/sync_status_page.py`
- Modify: `app/tasks/schedule.py`, `app/tasks/__init__.py` (register like `sync_sanp_health`)
- Test: `tests/test_status_page_task.py`

**Interfaces:**
- Consumes: Task 4 `run_sync`.
- Produces: task name `app.tasks.sync_status_page.sync_status_page`, signature `sync_status_page(trigger: str = "SCHEDULED", requested_by: str | None = None) -> dict[str, Any]`; beat entry `"sync-status-page"` with `schedule=settings.status_page_sync_interval_s`.

- [ ] **Step 1: Write failing tests:** beat schedule contains `"sync-status-page"` with task name above and `900.0`; calling the task function with `run_sync` patched passes `trigger`/`requested_by` through and returns the status as a JSON-serializable dict (pattern of `tests/test_sanp_health_task.py`).
- [ ] **Step 2: Run** `uv run pytest tests/test_status_page_task.py -v` → FAIL.
- [ ] **Step 3: Implement** with `run_async_task` like `sync_sanp_health`.
- [ ] **Step 4: Run** → PASS; `uv run pytest tests/test_task_event_loops.py tests/test_celery_queue.py` still PASS.
- [ ] **Step 5: Commit** — `feat: schedule release status sync every 15 minutes`.

### Task 6: Read model — status, summary, detail, test linkage

**Files:**
- Create: `app/schemas/status_page.py`
- Create: `app/services/status_page.py`
- Test: `tests/test_status_page_service.py`

**Interfaces:**
- Consumes: Tasks 2, 3, 4 (`get_sync_status`).
- Produces (schemas, pydantic, `from_attributes=True` where read from ORM):
  - `SyncStatusOut` (all `SpSyncStatus` fields except `id`, `celery_task_id`).
  - `RunRefOut(run_id: UUID, kind: Literal["NRT","QG","TAS"], outcome: Literal["PASSED","FAILED","NEUTRAL"], needs_review: bool, trigger_type: str | None, timestamp_start: datetime | None, passed: int | None, failed: int | None, allure_url: str | None, gha_run_url: str | None, hub_url: str)`
  - `CellOut(version, read_status, error_detail, checked_at, last_ok_at, version_since, drift: Drift | None, tests: list[RunRefOut])` — `drift` and `tests` empty/None for `REPO`; `tests` holds the latest run per kind present.
  - `ApplicationRowOut(id, name, product_id, product_key, repository, pipeline_url, runtime, ci, kind, test_object, ea_node_id, repo: CellOut | None, envs: dict[Literal["DEV","UAT","PROD"], CellOut | None], flags: list[Literal["ALIGNED","DRIFT","UNREACHABLE","UNTESTED"]])`
  - `ProductGroupOut(id, name, applications: list[ApplicationRowOut])`, `AreaGroupOut(area, products: list[ProductGroupOut])`, `StatusOut(sync: SyncStatusOut | None, areas: list[AreaGroupOut])`
  - `SummaryOut(total, aligned, drift, unreachable, untested: int, last_success_at: datetime | None)`
  - `DeployEventOut(version, previous_version, detected_at, previous_seen_at, is_baseline, direction: Literal["upgrade","rollback","unknown","baseline"], runs: list[RunRefOut])`
  - `ApplicationDetailOut(application: ApplicationRowOut, product_name: str, history: dict[Literal["REPO","DEV","UAT","PROD"], list[DeployEventOut]])`
  - Service: `async def get_status(db) -> StatusOut`, `async def get_summary(db) -> SummaryOut`, `async def get_application_detail(db, application_id: UUID) -> ApplicationDetailOut | None`, `def run_kind(test_origin: str | None) -> str`, `def run_outcome(run: TestRun) -> tuple[str, bool]` (outcome, needs_review).

- [ ] **Step 1: Write failing pure tests**

```python
assert run_kind("QG") == "QG" and run_kind("TAS") == "TAS" and run_kind(None) == "NRT" and run_kind("CI") == "NRT"
# run_outcome(run) -> (outcome, needs_review)
revisioned OK with failed=3                     -> ("PASSED", False)
revisioned KO with failed=0                     -> ("FAILED", False)
PENDING, failed=0 broken=0, origin NRT          -> ("PASSED", True)
PENDING, failed=0 broken=1, origin QG           -> ("FAILED", True)
PENDING, failed=2, origin TAS                   -> ("FAILED", False)   # TAS never needs review
PENDING, counts None                            -> ("NEUTRAL", True)
```

- [ ] **Step 2: Write failing DB tests** (seed products/apps/state/events and `qachub.test_suites`/`test_runs`)
  - linkage matches `test_object_version="v1.4.2"` against deployed `"1.4.2"` and env `UAT` only (a PROD run is not shown in UAT);
  - latest run per kind wins (two NRT runs → newer shown; one QG and one TAS → three entries total when an NRT exists);
  - two applications with the same `test_object` both get the run (Review Focus 4);
  - application without `test_object` → `tests == []` and never `UNTESTED`;
  - flags: PROD equal + all OK → `ALIGNED`; PROD behind → `DRIFT`; DEV ahead only → no `DRIFT`; a target `ERROR` → `UNREACHABLE` and not `ALIGNED`; UAT version without runs → `UNTESTED`;
  - `get_status` groups areas `CORE` then `TOUCHPOINT`, products and applications sorted by name, and runs a bounded number of queries (count with a `before_cursor_execute` listener: ≤ 6 regardless of app count);
  - `get_summary` counts match the flags;
  - detail: history newest first per target; baseline → `direction="baseline"`; `1.4.2 → 1.4.1` → `rollback`; non-semver → `unknown`; runs per version capped at 20; `hub_url` per kind; unknown id → `None`.
- [ ] **Step 3: Run** `uv run pytest tests/test_status_page_service.py -v` → FAIL.
- [ ] **Step 4: Implement.** Fetch catalog + states in two queries; fetch candidate runs in one query joining `test_suites`/`test_runs` filtered by `test_object IN (...)` and `env IN ('DEV','UAT','PROD')`, then match in Python on `(test_object, normalize_version(test_object_version), env)`.
- [ ] **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** — `feat: release status read model with test linkage`.

### Task 7: Catalog service (CRUD, check, audit)

**Files:**
- Create: `app/services/status_page_catalog.py`
- Modify: `app/schemas/status_page.py`
- Test: `tests/test_status_page_catalog.py`

**Interfaces:**
- Consumes: Task 3 models; Task 4 `read_repo`, `read_env`.
- Produces:
  - Schemas: `ProductIn(name, area, user)`, `ProductPatch(name|None, area|None, user)`, `ProductOut(id, name, area, application_count)`, `ApplicationIn(product_id, name, product_key, repository, pipeline_url|None, runtime, ci, kind="backend", test_object|None, ea_node_id|None, user)`, `ApplicationPatch` (all optional except `user`), `ApplicationOut`, `CheckIn(product_key, repository)`, `CheckOut(repo: CheckTargetOut, envs: dict[str, CheckTargetOut])` with `CheckTargetOut(ok: bool, version: str|None, detail: str|None)`, `EaComponentOut(id, name)`, `DeleteIn(user)`.
  - Validation in schemas: `repository` matches `^[A-Za-z0-9._-]+$`; `pipeline_url` starts with `https://`; strings stripped, non-empty.
  - Errors: `class CatalogConflictError(Exception)` with `field: str` and Italian `message`; `class CatalogNotFoundError(Exception)`.
  - Functions: `list_products`, `create_product`, `update_product`, `delete_product`, `list_applications(db, product_id: UUID | None)`, `create_application`, `update_application`, `delete_application`, `check_application(payload: CheckIn) -> CheckOut`, `list_test_objects(db) -> list[str]` (distinct, sorted), `list_ea_components(db, q: str | None) -> list[EaComponentOut]` (kind `component`, status `active`, `q` case-insensitive on id/name, limit 50).
  - Audit: every write adds `AuditLog(actor_id=None, action="status_page.<product|application>.<create|update|delete>", resource_type="sp_product"|"sp_application", resource_id=str(id), detail=json.dumps({"user": ..., "before": {...}, "after": {...}}))` in the same transaction.

- [ ] **Step 1: Write failing tests**
  - duplicate product name → `CatalogConflictError(field="name", message="Esiste già un prodotto con questo nome")`;
  - duplicate `product_key` → field `product_key`, "Chiave product già usata da un'altra applicazione"; duplicate name in same product → field `name`, "Esiste già un'applicazione con questo nome nel prodotto"; same name in another product → OK; shared `test_object` → OK;
  - `delete_product` with applications → `CatalogConflictError(field="product", message="Il prodotto contiene ancora applicazioni")` (Review Focus 5); empty → deleted;
  - `update_application(product_id=other)` moves it; audit `before/after` contain only changed fields;
  - unknown ids → `CatalogNotFoundError`;
  - `check_application` with readers patched: maps each `Reading` to `CheckTargetOut(ok=read_status=="OK", version, detail)`;
  - `list_test_objects` distinct + sorted; `list_ea_components("biz")` filters by kind/status/q.
- [ ] **Step 2: Run** `uv run pytest tests/test_status_page_catalog.py -v` → FAIL.
- [ ] **Step 3: Implement.** Translate `IntegrityError` on the named constraints into `CatalogConflictError`; pre-check product emptiness before delete.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: release status catalog management with audit log`.

### Task 8: `/api/v1/releases` router and OpenAPI

**Files:**
- Modify: `app/api/v1/releases.py` (replace the TODO stub)
- Modify: `openapi/qa-hub-be.json` (regenerated)
- Test: `tests/test_status_page_api.py`

**Interfaces:**
- Consumes: Tasks 4, 5, 6, 7.
- Produces endpoints (prefix `/api/v1/releases`):
  - `GET /status` → `StatusOut`; `GET /summary` → `SummaryOut`; `GET /applications/{application_id}` → `ApplicationDetailOut` | 404 "Applicazione non trovata"
  - `GET /sync-status` → `SyncStatusOut | None`; `POST /sync` body `ManualSyncIn(user: str)` → 202 `SyncStatusOut`, 409 "Sincronizzazione già in corso"
  - `GET|POST /catalog/products`, `PATCH|DELETE /catalog/products/{id}`; `GET|POST /catalog/applications` (`GET` query `product_id`), `PATCH|DELETE /catalog/applications/{id}`; `POST /catalog/applications/check`; `GET /catalog/test-objects`; `GET /catalog/ea-components?q=`. `DELETE` takes `DeleteIn` body. Create → 201. `CatalogConflictError` → 409 `{"detail": message, "field": field}`; `CatalogNotFoundError` → 404.

- [ ] **Step 1: Write failing tests** (pattern of `tests/test_sanp_health_api.py`, services patched with `AsyncMock`)
  - `POST /sync` → calls `request_manual_sync(user=...)` then `sync_status_page.apply_async(kwargs={"trigger": "MANUAL", "requested_by": user}, retry=False)`, stores the task id, returns 202; `StatusPageSyncInProgressError` → 409 and `apply_async` not called;
  - each catalog route maps conflict → 409 with `field`, not found → 404, create → 201;
  - `GET /applications/{bad uuid}` → 422; unknown → 404;
  - static routes (`/catalog/test-objects`, `/sync-status`) are not shadowed by path params.
- [ ] **Step 2: Run** `uv run pytest tests/test_status_page_api.py -v` → FAIL.
- [ ] **Step 3: Implement router**; regenerate the OpenAPI file (command in Global Constraints) and check `git diff openapi/` only adds `/api/v1/releases/*` operations.
- [ ] **Step 4: Run** → PASS; full `uv run pytest`, `uv run ruff check .`, `uv run mypy .` → PASS.
- [ ] **Step 5: Commit** — `feat: expose release status API`.

### Task 9: Backend README

**Files:**
- Modify: `README.md` (project structure: new router/services/task; a "Release Status" paragraph: tables, 15-minute sync, settings, the reachability requirement of spec §12.1 and the Python one-liner to verify it from a pod)

- [ ] **Step 1: Update README.** 
- [ ] **Step 2: Commit** — `docs: document the release status section`.

---

## Frontend (`pagopa-qa-centralhub-frontend`)

Frontend tasks consume the API of Task 8. Types mirror the backend schemas field by field.

### Task 10: Types, hooks and pure helpers

**Files:**
- Create: `src/types/release-status.ts`
- Create: `src/hooks/useReleaseStatus.ts`
- Create: `src/lib/release-status.ts`, `src/lib/release-status.test.ts`

**Interfaces:**
- Produces:
  - Types mirroring Task 6/7 schemas: `SyncStatus`, `RunRef`, `Cell`, `ApplicationRow`, `ProductGroup`, `AreaGroup`, `ReleaseStatus`, `ReleaseSummary`, `DeployEvent`, `ApplicationDetail`, `Product`, `Application`, `ApplicationInput`, `CheckResult`, `EaComponent`; `type Target = "REPO" | "DEV" | "UAT" | "PROD"`; `type StatusFilter = "all" | "drift" | "unreachable" | "untested"`.
  - Hooks (query key root `["release-status"]`): `useReleaseStatus()`, `useReleaseSummary()`, `useApplicationDetail(id: string | null)`, `useReleaseSyncStatus()` (refetchInterval 3000 ms while status is `QUEUED`/`RUNNING`, else off; invalidates `status`/`summary` when it leaves those states), `useTriggerReleaseSync()`, `useProducts()`, `useApplications(productId?: string)`, `useTestObjects()`, `useEaComponents(q: string)`, `useSaveProduct()`, `useDeleteProduct()`, `useSaveApplication()`, `useDeleteApplication()`, `useCheckApplication()`. Mutations add `user` from the session email (same helper pattern as `useFlaky.ts`) and invalidate the root key.
  - Helpers: `filterApplications(groups: AreaGroup[], area: "CORE"|"TOUCHPOINT", query: string, filter: StatusFilter): AreaGroup[]` (search on name, product name, repository, product_key; case-insensitive; drops empty products); `cellVisual(cell: Cell | null, target: Target): { icon: "loading"|"ok"|"error"|"warning"|"info"; drift: "equal"|"behind"|"ahead"|null; muted: boolean; tooltip: string }`; `syncLabel(sync: SyncStatus | null, now: Date): { text: string; stale: boolean }` (stale when `FAILED` or `last_success_at` older than 45 min); `parseFilterParams(params: URLSearchParams)` / `filterParams(...)` for `?area=&q=&filter=`.

- [ ] **Step 1: Write failing tests** in `src/lib/release-status.test.ts`:
  - search matches product name and `product_key`; `filter="drift"` keeps only `DRIFT`-flagged apps; empty products removed;
  - `cellVisual`: `null` → loading; `ERROR` with version → `{icon:"error", muted:true}` and tooltip contains the version and "non raggiungibile"; `EMPTY_BODY`/`NO_VERSION` → warning; non-semver OK → info; OK + `BEHIND` → `drift:"behind"`;
  - `syncLabel`: `RUNNING` → "Sincronizzazione in corso…"; OK 10:15 automatic → "Ultimo aggiornamento 10:15 · automatico"; MANUAL → "· manuale ({requested_by})"; success 50 min ago → `stale:true`;
  - params round-trip.
- [ ] **Step 2: Run** `pnpm test src/lib/release-status.test.ts` → FAIL.
- [ ] **Step 3: Implement** types, hooks, helpers.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: release status types, hooks and helpers`.

### Task 11: Route, navigation and feature wiring

**Files:**
- Modify: `src/lib/features.ts` (`{ route: "/releases", key: "release_status" }`), `src/lib/features.test.ts`
- Modify: `src/components/shell/Sidebar.tsx` (new group "Release" after "Test Results": `{ href: "/releases", label: "Release Status", icon: PackageCheck, action: "view:status_page", featureKey: "release_status" }`), `src/components/shell/Sidebar.test.tsx`
- Modify: `src/components/shell/Topbar.tsx` (`"/releases": "Release Status"`)
- Modify: `src/components/overview/sections.ts` (`TESTING_SECTIONS` entry: "Release Status", "Versioni in DEV, UAT e PROD, storico dei deploy e test eseguiti.", `PackageCheck`, `view:status_page`, `release_status`, badge "NEW", tone `accent`)

- [ ] **Step 1: Write failing tests:** `featureKeyForPath("/releases/catalog") === "release_status"`; Sidebar shows "Release Status" linking `/releases` with `view:status_page`, hidden when the feature is disabled (follow the table-driven cases already in `Sidebar.test.tsx`).
- [ ] **Step 2: Run** `pnpm test src/lib/features.test.ts src/components/shell/Sidebar.test.tsx` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: add release status navigation`.

### Task 12: Status view (`/releases`)

**Files:**
- Modify: `src/app/(portal)/releases/page.tsx` (wrap in `RouteGuard action="view:status_page"`)
- Create: `src/components/release-status/ReleaseHeader.tsx` (title, `syncLabel`, stale/partial banners, sync button in `Gate sync:trigger`, "Gestisci catalogo" link in `Gate manage:status_page`)
- Create: `src/components/release-status/ReleaseKpis.tsx` (existing `Kpi` primitive: servizi `LayoutGrid`, allineati `CircleCheck`, con versioni diverse `RefreshCwOff`, non raggiungibili `CloudOff`, non testati `FlaskConical`; each sets the filter)
- Create: `src/components/release-status/ReleaseToolbar.tsx` (tabs Core `WalletMinimal` / TouchPoint `Landmark`, search `Search`/`X`, filter chips; synced to the query string)
- Create: `src/components/release-status/ProductGroup.tsx` (collapsible, count, drift/error badges, sticky header)
- Create: `src/components/release-status/ApplicationRow.tsx` (name, runtime/CI chips via `Chip`, links repo `LibraryBig` / pipeline `Rocket` / OpEx `ChartLine` with the URL template of spec §9.3, four cells)
- Create: `src/components/release-status/VersionCell.tsx` (icons per spec §9.2 table via `cellVisual`, drift `Equal`/`ArrowDown`/`ArrowUp`, tooltip)
- Create: `src/components/release-status/TestMarkers.tsx` (≤ 3 icons 14 px: `FlaskConical` NRT, `ShieldCheck` QG, `Bot` TAS; colour `success`/`danger`/`text-dim`; small dot when `needs_review`; single muted `FlaskConical` "non testata" when `tests` empty and app has `test_object`; nothing when no `test_object`)
- Create: `src/components/release-status/GithubMark.tsx` (inline SVG, `aria-hidden`)
- Test: `src/app/(portal)/releases/page.test.tsx`, `src/components/release-status/VersionCell.test.tsx`, `src/components/release-status/TestMarkers.test.tsx`

**Interfaces:**
- Consumes: Task 10 hooks/helpers/types.
- Produces: `ApplicationRow` accepts `onOpen(applicationId: string)`; the page holds `selectedId` and renders `ApplicationDetailSheet` from Task 13 (stub import until then).

- [ ] **Step 1: Write failing tests**
  - page renders Core products sorted, switches to TouchPoint, `?filter=drift` preselects the chip, search filters rows, "Nessun risultato" with `SearchX` when empty;
  - sync button hidden without `sync:trigger`; click → mutation called, button shows "Sincronizzazione…" while `QUEUED`/`RUNNING`; 409 → inline message "Sincronizzazione già in corso";
  - "Gestisci catalogo" hidden without `manage:status_page`;
  - `VersionCell`: error with last version shows the version muted plus `CloudOff`; behind shows down arrow;
  - `TestMarkers`: NRT passed + TAS failed → two icons with accessible names "NRT: superata", "TAS: fallita"; `needs_review` → name includes "da revisionare"; no `test_object` → renders nothing.
- [ ] **Step 2: Run** `pnpm test src/app/(portal)/releases src/components/release-status` → FAIL.
- [ ] **Step 3: Implement.** Use the existing tokens (`success`, `warning`, `danger`, `accent`, `*-soft`, `border`, `surface`); skeleton cells while loading; below `sm` the four cells stack into two lines; dark mode via tokens only.
- [ ] **Step 4: Run** → PASS; check the page in the browser (`pnpm dev`) in light and dark mode at desktop and 375 px.
- [ ] **Step 5: Commit** — `feat: release status view`.

### Task 13: Application detail sheet

**Files:**
- Create: `src/components/release-status/ApplicationDetailSheet.tsx`
- Create: `src/components/release-status/DeployTimeline.tsx`
- Test: `src/components/release-status/ApplicationDetailSheet.test.tsx`

**Interfaces:**
- Consumes: `useApplicationDetail(id)`; `TestMarkers` run rendering.
- Produces: `ApplicationDetailSheet({ applicationId: string | null, onClose: () => void })`.

- [ ] **Step 1: Write failing tests:** header shows product, chips, links and, only when `ea_node_id` is set, "EA Map: {ea_node_id}" linking to `/ea-map` (the EA map has no deep-link parameter today; adding one is out of scope); tabs DEV/UAT/PROD/"Release GitHub"; timeline newest first; baseline entry "prima rilevazione"; rollback shows `Undo2`; time text "rilevato il 16/11 10:15 (tra 10:00 e 10:15)"; runs list links to `hub_url`, Allure, GitHub Actions; no `test_object` → hint "Collega un test object per vedere le run" with edit link gated by `manage:status_page`; Escape closes.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** as a right-side sheet built on `src/components/ui/dialog.tsx`; tabs with `src/components/ui/tabs.tsx`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: release status application detail with deploy history`.

### Task 14: Catalog management page

**Files:**
- Create: `src/app/(portal)/releases/catalog/page.tsx` (`RouteGuard action="manage:status_page"`)
- Create: `src/components/release-status/catalog/ProductList.tsx`, `ProductForm.tsx`, `ApplicationTable.tsx`, `ApplicationForm.tsx`, `CheckResult.tsx`, `ConfirmDelete.tsx`
- Test: `src/app/(portal)/releases/catalog/page.test.tsx`, `src/components/release-status/catalog/ApplicationForm.test.tsx`

**Interfaces:**
- Consumes: catalog hooks from Task 10.

- [ ] **Step 1: Write failing tests**
  - products grouped by area with counts; delete button disabled with tooltip "Il prodotto contiene ancora applicazioni" when count > 0; backend 409 also surfaces that message (Review Focus 5);
  - application form: required fields, `repository` pattern, `https://` pipeline; 409 with `field` shows the message under that field;
  - "Verifica" shows ✓ version / ✗ detail per target; failed checks show a warning but Save stays enabled; Save also triggers the check;
  - `test_object` combobox lists `useTestObjects()` and accepts free text; EA picker preselects the component whose id equals `repository`;
  - after save, toast "Applicazione salvata" with "Sincronizza ora" action only with `sync:trigger`;
  - delete application asks confirmation "Verranno eliminati anche lo storico dei deploy e lo stato attuale".
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: release status catalog management`.

### Task 15: Overview KPI and final verification

**Files:**
- Modify: `src/app/(portal)/page.tsx` (new `ReleaseStatusKpi` in the Testing pillar `kpis`, after `PendingRunsKpi`)
- Test: `src/app/(portal)/page.test.tsx`

**Interfaces:**
- Consumes: `useReleaseSummary()`.

- [ ] **Step 1: Write failing test:** KPI "Release allineate" shows `aligned/total` (e.g. "41/53"), lists "7 con versioni diverse", "3 non raggiungibili", "2 non testati", each linking `/releases?filter=drift|unreachable|untested`; shows "—" while loading; hidden without `view:status_page`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** with `OverviewKpi`.
- [ ] **Step 4: Run** `pnpm lint && pnpm test && pnpm build` → all PASS; manual pass in the browser against a local backend with a completed sync.
- [ ] **Step 5: Commit** — `feat: release status KPI on the overview`.
