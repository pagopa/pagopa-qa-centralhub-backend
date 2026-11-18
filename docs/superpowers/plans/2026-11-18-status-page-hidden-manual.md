# Status Page Hidden Entries and Manual Versions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Status Page catalog hold products and applications hidden from the status page, and applications whose DEV/UAT/PROD versions are entered by hand, so that the version lookup serves every application under test.

**Architecture:** Migration `0033` adds `hidden` to products and applications and `version_source` (`AUTO|MANUAL`) to applications, and allows `read_status = 'MANUAL'`. Manual versions live in `sp_version_state` and go through one service function that reuses the sync's deploy-event rules, so lookup, history, drift and run linkage need no second code path. The sync reads only REPO for manual applications; the read model filters hidden applications out of status and summary. The frontend catalog forms gain the two flags and the manual version inputs; the grid shows manual cells with their own icon.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, pytest (asyncio auto, real Postgres via `tests/_db.py`); Next.js 16, React 19, TanStack Query, Tailwind, lucide-react, vitest + Testing Library, pnpm.

**Spec:** `docs/superpowers/specs/2026-11-18-status-page-hidden-manual-design.md` (backend repo).

## Global Constraints

- Two repos. Backend tasks 1–6 on `feature/status-page-hidden-manual` (already created from `main` after #79, holding the untracked spec and this plan). Frontend tasks 7–8 on `feature/status-page-hidden-manual` created from an up-to-date frontend `main` (`git checkout main && git pull --ff-only`); the frontend checkout is on `statuspage-default-tab-uat`: if it has uncommitted changes, stop and ask the user.
- **Commits:** follow the user's CLAUDE.md. Local commits only if the user authorizes them for this plan; never push, never open a PR. Conventional Commits, no AI attribution trailers. The first backend commit also adds the spec and this plan.
- Everything in the repo is English; UI copy is Italian.
- Backward compatible: every new column has a server default (`hidden=false`, `version_source='AUTO'`); every new API input field is optional with that default; payloads without the new fields behave exactly as today (a PATCH without `version_source` never changes it).
- `version_source` values: `AUTO`, `MANUAL`. New read status: `MANUAL`. Manual targets: `DEV`, `UAT`, `PROD` only (REPO is always read from GitHub).
- Effectively hidden = `application.hidden OR product.hidden`. Hidden applications are excluded from `get_status` and `get_summary` only; `get_application_detail`, `get_version` and the catalog ignore `hidden`.
- A `MANUAL` cell is readable like `OK`: drift computed, never `UNREACHABLE`, counts as OK for `ALIGNED`.
- Manual version values: trimmed, empty string means "clear", max 100 chars, stored as typed (no `Release ` stripping).
- Audit actions: existing `status_page.application.update` for field changes; new `status_page.application.versions` with detail `{"user", "before": {target: version}, "after": {target: version}}`, written only when at least one version changed.
- Error copy: 422 `"Le versioni manuali richiedono un'applicazione manuale"` (field `manual_versions`) when manual versions are sent for an application whose resulting `version_source` is `AUTO`.
- Backend service style: `structlog`, `try/except → logger.exception(...); raise`. Checks: `uv run --extra dev ruff check <changed files>`, `uv run --extra dev ruff format --check <changed files>`, `uv run --extra dev mypy app`, `uv run --extra dev pytest`. The suite has 10 known failures also on `main` (`test_ea_service` seed, 8× `test_ea_sync_tracing`, `test_gpd_position_service`); anything else failing is yours. Never run `ruff --fix` on the whole repo.
- OpenAPI: regenerate with `uv run --extra dev python -m scripts.generate_openapi --apim-compatible` (3.0.3, `nullable: true`, no `anyOf` with null).
- Frontend checks: `pnpm test`, `pnpm lint`, `pnpm exec tsc --noEmit`. `pnpm build` currently stops at Next.js's vulnerability check on `next@16.4.1` after compiling: report it, do not upgrade dependencies.

## Review Focus

1. A sync that is reading while an application is switched to MANUAL must not overwrite its DEV/UAT/PROD states with the readings taken before the switch.
2. Re-entering the same manual version must not create a deploy event; clearing a version must keep the row and make the lookup answer 404 "Versione non ancora rilevata".
3. An existing client sending today's payloads (no `hidden`, `version_source`, `manual_versions`) must see no behaviour change, including PATCHes on a MANUAL application.
4. A product marked hidden must hide all its applications from status, summary and the Overview KPI while `/releases/versions` keeps serving them.
5. Switching AUTO → MANUAL must keep the last good version and its dates even if the last read failed, so the history and the deploy windows do not move.

---

### Task 1: Model and migration 0033

**Files:**
- Modify: `app/models/status_page.py`
- Create: `alembic/versions/0033_status_page_hidden_manual.py`
- Test: `tests/test_status_page_migration_0033.py` (or extend the existing model tests if a pattern exists)

**Interfaces:**
- Produces: `SpProduct.hidden: bool`, `SpApplication.hidden: bool`, `SpApplication.version_source: str`, constants `VERSION_SOURCES = ("AUTO", "MANUAL")`, `READ_STATUSES` extended with `"MANUAL"`.

- [ ] **Step 1: Write the failing tests**: inserting an `SpApplication` without the new fields reads back `hidden is False` and `version_source == "AUTO"`; an `SpProduct` reads back `hidden is False`; an `SpVersionState` with `read_status="MANUAL"` commits; `version_source="X"` raises `IntegrityError`.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_migration_0033.py -v`. Expected: FAIL (unknown attribute / check violation).
- [ ] **Step 3: Implement**: columns with `server_default` (`sa.false()`, `'AUTO'`), check `ck_sp_applications_version_source`, recreate `ck_sp_version_state_read_status` with `MANUAL`. Downgrade: `UPDATE sp_version_state SET read_status='NO_VERSION' WHERE read_status='MANUAL'`, restore the old check, drop the columns. `down_revision` = the `0032` revision id.
- [ ] **Step 4: Apply and test**: `uv run alembic upgrade head`, then the tests. Also run `uv run alembic downgrade -1 && uv run alembic upgrade head` once. Expected: PASS, both directions clean.
- [ ] **Step 5: Commit**: `feat: add hidden flags and manual version source to the status page tables` (with the spec and this plan).

### Task 2: Manual version write path

**Files:**
- Create: `app/services/status_page_manual.py`
- Modify: `app/services/status_page_sync.py` (`apply_reading` treats `MANUAL` like `OK`)
- Test: `tests/test_status_page_manual.py`

**Interfaces:**
- Consumes: `apply_reading(state, reading, now, application_id)` and `Reading(target, read_status, version, error_detail)`.
- Produces:
  - `MANUAL_TARGETS = ("DEV", "UAT", "PROD")`
  - `async def make_manual(db: AsyncSession, app: SpApplication, now: datetime) -> None`: creates missing rows empty (`version=None`, `read_status="MANUAL"`, `checked_at=now`), turns existing rows into `MANUAL` with `error_detail=None` and every other field unchanged. No events. Does not commit.
  - `async def set_manual_versions(db: AsyncSession, app: SpApplication, versions: dict[str, str | None], user: str, now: datetime) -> dict[str, tuple[str | None, str | None]]`: for each key sent, trimmed value; `""`/`None` clears (`version=None`, `version_since=None`, row kept, no event); a value equal to the current one only refreshes `checked_at`/`last_ok_at`; a different value goes through `apply_reading(state, Reading(target, "MANUAL", value, None), now, app.id)` and adds the returned event. Returns the changed targets `{target: (before, after)}` and writes the `status_page.application.versions` audit entry when non-empty. Does not commit. Raises `ValueError` on a key outside `MANUAL_TARGETS` or a value over 100 chars.
  - `async def manual_versions_of(db: AsyncSession, app_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict[str, str | None]]`: one query, current DEV/UAT/PROD versions per application.

- [ ] **Step 1: Write the failing tests**:
  - `apply_reading` with `read_status="MANUAL"` and a new version returns an event and sets `version_since`/`last_ok_at` (same assertions as the OK case).
  - `make_manual` on an app with an `ERROR` state holding `"1.2.0"` keeps `version="1.2.0"`, `last_ok_at`, `version_since`, clears `error_detail`, sets `read_status="MANUAL"`, creates the two missing rows empty, adds no `SpDeployEvent`.
  - `set_manual_versions(..., {"UAT": "1.3.0"})` from `1.2.0`: one event `previous_version="1.2.0"`, `previous_seen_at` = old `last_ok_at`, `is_baseline=False`; from an empty row: `is_baseline=True`.
  - same value again: no new event, `checked_at` updated.
  - `{"PROD": ""}`: row kept with `version=None`, `version_since=None`, no event; `get_version(db, key, "PROD")` raises `VersionNotFoundError("Versione non ancora rilevata")`.
  - audit entry `status_page.application.versions` with `{"user", "before": {"UAT": "1.2.0"}, "after": {"UAT": "1.3.0"}}`; none when nothing changed.
  - `{"REPO": "1"}` raises `ValueError`.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_manual.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** the module and the one-line change in `apply_reading` (`reading.read_status not in ("OK", "MANUAL")`).
- [ ] **Step 4: Run** the new tests plus `tests/test_status_page_sync.py`. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: add the manual version write path for status page applications`.

### Task 3: Catalog schemas and service

**Files:**
- Modify: `app/schemas/status_page.py`, `app/services/status_page_catalog.py`, `app/api/v1/releases.py` (422 mapping)
- Test: `tests/test_status_page_catalog.py`, `tests/test_status_page_api.py`

**Interfaces:**
- Consumes: Task 2 functions.
- Produces:
  - `VersionSource = Literal["AUTO", "MANUAL"]`; `ManualVersionsIn` = model with optional `DEV`, `UAT`, `PROD: str | None` (only sent keys count: use `model_dump(exclude_unset=True)`).
  - `ProductIn.hidden: bool = False`, `ProductPatch.hidden: bool | None = None`, `ProductOut.hidden: bool`.
  - `ApplicationIn.hidden: bool = False`, `ApplicationIn.version_source: VersionSource = "AUTO"`, `ApplicationIn.manual_versions: ManualVersionsIn | None = None`; `ApplicationPatch` same three fields, all `None` by default.
  - `ApplicationOut.hidden: bool`, `ApplicationOut.version_source: str`, `ApplicationOut.manual_versions: dict[EnvTarget, str | None] | None` (`None` for AUTO applications).
  - `class CatalogValidationError(Exception)` with `field` and `message`, mapped in the router to 422 with the same detail shape the 409 conflict mapper uses.
  - `_APP_FIELDS` gains `hidden` and `version_source` (so update/create/delete audits include them); product audits include `hidden`.

- [ ] **Step 1: Write the failing tests** (catalog service, real DB, `T7-` prefix like the existing tests):
  - create without the new fields → `hidden=False`, `version_source="AUTO"`, `manual_versions=None`, no version rows.
  - create with `version_source="MANUAL"` and `manual_versions={"UAT": "2.0.0"}` → three rows, UAT `2.0.0` with a baseline event, `ApplicationOut.manual_versions == {"DEV": None, "UAT": "2.0.0", "PROD": None}`.
  - PATCH AUTO→MANUAL plus `manual_versions={"PROD": "1.1.0"}` on an app whose PROD is `1.0.0` (state `OK`): switch first, then one event `1.0.0→1.1.0`; audits: one `update` (`version_source`) and one `versions`.
  - PATCH with `manual_versions` on an AUTO app, and PATCH `version_source="AUTO"` plus `manual_versions` → `CatalogValidationError("manual_versions", "Le versioni manuali richiedono un'applicazione manuale")`, nothing written.
  - PATCH MANUAL→AUTO keeps the rows untouched.
  - PATCH `name` only on a MANUAL app leaves `version_source` and the versions unchanged.
  - product `hidden` create/patch/out and in the product audit.
  - `list_applications` returns `manual_versions` for MANUAL apps with a bounded number of queries (one extra query, not one per app).
  - API: POST/PATCH with the new fields return them; the 422 case returns status 422 with `field == "manual_versions"`.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_catalog.py tests/test_status_page_api.py -v`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement**: in create, flush the app, then `make_manual` and `set_manual_versions` when MANUAL; in update, validate the resulting source before any write, apply field changes, `make_manual` when switching to MANUAL, then `set_manual_versions`; one `_commit` at the end. Build `ApplicationOut` through a helper that fills `manual_versions` from `manual_versions_of`.
- [ ] **Step 4: Run** the two files plus `tests/test_status_page_manual.py`. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: manage hidden flags and manual versions from the status page catalog`.

### Task 4: Sync skips manual environments

**Files:**
- Modify: `app/services/status_page_sync.py`
- Test: `tests/test_status_page_sync.py`

**Interfaces:**
- Consumes: `SpApplication.version_source`.
- Produces: no new public names; `_read_all` reads `("REPO", *ENV_TARGETS)` for AUTO apps and `("REPO",)` for MANUAL apps; the post-read `FOR UPDATE` query selects `(id, version_source)` and drops DEV/UAT/PROD readings of applications that are MANUAL at write time.

- [ ] **Step 1: Write the failing tests**:
  - a MANUAL app with DEV `1.0.0` (state `MANUAL`) and mocked readers returning `9.9.9` everywhere: after `run_sync`, REPO is `9.9.9`, DEV/UAT/PROD unchanged, no env events, and the env reader was not called for it.
  - an app switched to MANUAL while the reads are in flight (patch `_read_all` to flip `version_source` in a separate session before returning): its env states are untouched, REPO is written, the sync ends `OK`/`PARTIAL`, not `FAILED`.
  - `applications_checked` counts manual apps; `reads_failed` counts only performed reads.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_sync.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: skip the environment reads of manual status page applications`.

### Task 5: Read model — hidden filter and manual cells

**Files:**
- Modify: `app/services/status_page.py`, `app/schemas/status_page.py`
- Test: `tests/test_status_page_service.py`

**Interfaces:**
- Produces: `ApplicationRowOut.version_source: str`, `ApplicationRowOut.hidden: bool` (effective). `_load_all` excludes effectively hidden applications; the detail builds its row with `hidden=application.hidden or product.hidden`.

- [ ] **Step 1: Write the failing tests**:
  - hidden app, app in a hidden product, visible app: `get_status` lists only the visible one; a product whose apps are all hidden is absent; `get_summary.total` counts only visible apps.
  - `get_application_detail` of a hidden app works and has `application.hidden is True`; `get_version` of a hidden app works.
  - MANUAL cells: PROD `MANUAL` `1.0.0` with REPO `1.1.0` → drift `BEHIND`, flag `DRIFT`, not `UNREACHABLE`; all targets `MANUAL`/`OK` and equal → `ALIGNED`; an empty MANUAL PROD → not `ALIGNED`, not `UNREACHABLE`.
  - row carries `version_source`.
  - the query-count test still passes (`<= 6`).
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_service.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement**: readable = `read_status in ("OK", "MANUAL")` for drift, `ALIGNED` and `UNREACHABLE`; filter in the `_load_all` query (`where(not_(SpApplication.hidden), not_(SpProduct.hidden))`), not in Python, so the run and window queries load less.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: hide status page entries and show manual versions as readable`.

### Task 6: OpenAPI and README

**Files:**
- Modify: `openapi/qa-hub-be.json`, `README.md`

- [ ] **Step 1: Regenerate** the OpenAPI (command in Global Constraints) and check: no new operations, new fields present on `ProductIn/Out/Patch`, `ApplicationIn/Out/Patch`, `ApplicationRowOut`, `ManualVersionsIn`; `read_status` documentation mentions `MANUAL` if it is an enum; no `anyOf` with null.
- [ ] **Step 2: README**: in the Release Status section, describe hidden products/applications, manual applications (REPO still synced, versions entered in the catalog, served by `/releases/versions`) and the audit action.
- [ ] **Step 3: Full backend checks** (Global Constraints). Expected: only the 10 known failures.
- [ ] **Step 4: Commit**: `docs: document hidden and manual status page applications`.

### Task 7: Frontend types, manual cells and chips

**Files:**
- Modify: `src/types/release-status.ts`, `src/lib/release-status.ts`, `src/components/release-status/VersionCell.tsx`, `src/components/release-status/ApplicationRow.tsx`, `src/components/release-status/ApplicationDetailSheet.tsx`, `src/components/release-status/shared.tsx`
- Test: `src/lib/release-status.test.ts`, `src/components/release-status/VersionCell.test.tsx`, `src/components/release-status/ApplicationDetailSheet.test.tsx`, `src/app/(portal)/releases/page.test.tsx`

**Interfaces:**
- Produces: `ReadStatus` gains `"MANUAL"`; `VersionSource = "AUTO" | "MANUAL"`; `ApplicationRow.version_source`, `ApplicationRow.hidden`; `Product.hidden`; `Application.hidden`, `Application.version_source`, `Application.manual_versions: Record<EnvTarget, string | null> | null`; `ProductInput.hidden?`; `ApplicationInput.hidden?`, `ApplicationInput.version_source?`, `ApplicationInput.manual_versions?: Partial<Record<EnvTarget, string | null>>`; `CellIcon` gains `"manual"`.

- [ ] **Step 1: Write the failing tests**:
  - `cellVisual` for `MANUAL` with `1.4.2` and drift `BEHIND` → `{icon: "manual", drift: "behind", muted: false}`, tooltip contains `inserita a mano`; with `version: null` → `icon: "manual"`, tooltip `"<target>: versione manuale non impostata"`; a non-semver manual value keeps `icon: "manual"`.
  - `VersionCell` renders `role="img"` named `versione inserita a mano` (lucide `PencilLine`).
  - a MANUAL row shows a `manuale` chip next to AKS/GHA; the detail sheet shows `manuale` and, when `hidden`, `nascosta`.
- [ ] **Step 2: Run them**: `pnpm vitest run src/lib src/components/release-status "src/app/(portal)/releases"`. Expected: new tests FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the same command plus `pnpm exec tsc --noEmit`. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: show manual versions and hidden state on the status page`.

### Task 8: Catalog forms and table

**Files:**
- Modify: `src/components/release-status/catalog/ProductForm.tsx`, `ProductList.tsx`, `ApplicationForm.tsx`, `ApplicationTable.tsx`, `CheckResult.tsx`
- Test: `src/components/release-status/catalog/ApplicationForm.test.tsx`, `src/app/(portal)/releases/catalog/page.test.tsx`

**Interfaces:**
- Consumes: Task 7 types; the existing catalog mutations send `ApplicationInput` / `ProductInput` unchanged otherwise.

- [ ] **Step 1: Write the failing tests**:
  - product form: checkbox "Nascondi dalla status page" sends `hidden: true`; the list shows a `nascosto` badge.
  - application form: checkbox "Nascondi dalla status page" sends `hidden`; toggle "Versioni inserite a mano" shows inputs DEV/UAT/PROD prefilled from `manual_versions`; saving sends `version_source: "MANUAL"` and only the changed keys in `manual_versions` (an emptied field sends `null`); with the toggle off nothing about versions is sent; editing an AUTO app without touching the toggle sends neither `version_source` nor `manual_versions`.
  - "Verifica" on a manual app shows only the repository result.
  - the 422 `manual_versions` error is shown on the versions block.
  - application table: `manuale`, `nascosta`, and `nascosta (prodotto)` badges.
- [ ] **Step 2: Run them**: `pnpm vitest run src/components/release-status/catalog "src/app/(portal)/releases/catalog"`. Expected: new tests FAIL.
- [ ] **Step 3: Implement**. Inputs trimmed client side, max length 100, `font-mono`; all colours from the hub tokens.
- [ ] **Step 4: Full frontend checks** (Global Constraints). Expected: all tests pass, 0 lint errors.
- [ ] **Step 5: Commit**: `feat: manage hidden flags and manual versions from the catalog`.
