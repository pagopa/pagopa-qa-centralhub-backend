# NRT and TAS run management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the NRT and TAS pages of the Central Hub refresh, run deletion and run launching (plus a run-level note on NRT), matching the Quality Gate page.

**Architecture:** The backend QG service becomes origin-parametric (QG, TAS, NRT) and exposes per-origin endpoints; the NRT workflow gets a `correlation_id` input and `run-name` so the Hub can follow its runs; the frontend moves the NRT page onto `OriginNrtsPage`, makes the QG hooks origin-parametric and adds launch panels.

**Tech Stack:** GitHub Actions; FastAPI, SQLAlchemy async, Alembic, pytest (uv, ruff); Next.js 16, React 19, react-query, vitest, eslint, pnpm.

**Spec:** `docs/superpowers/specs/2026-11-12-nrt-tas-run-management-design.md` (backend repo)

**Repos and branches** (each cut from an updated `main`; stop and ask if a repo has uncommitted changes):
- TAS `/Users/andrea.ferracci/projects/pagopa-platform-integration-test`: `feature/nrt-dispatch-correlation-id`
- Backend `/Users/andrea.ferracci/projects/pagopa-qa-centralhub-backend`: `feature/nrt-tas-run-management`
- Frontend `/Users/andrea.ferracci/projects/pagopa-qa-centralhub-frontend`: `feature/nrt-tas-run-management`

## Global Constraints

- **No commits, pushes or PRs**: the user does them. Every task ends with its checks passing and the changes left in the working tree. Do not add AI attribution anywhere.
- Everything in the repositories (code, comments, logs, docs, test names) is in English; UI strings stay in Italian like the existing ones.
- Run ids: QG `qg-<hex32>` (run name `tas-qg-<hex32>`); TAS `<hex32>` (run name `tas-<hex32>`, `isQualityGateRun=false`); NRT `<hex32>` (run name `nrt-<hex32>`). `<hex32>` = `uuid.uuid4().hex`.
- Hub-run recognition uses exact regexes: `^tas-qg-[0-9a-f]{32}$`, `^tas-[0-9a-f]{32}$`, `^nrt-[0-9a-f]{32}$`.
- New actions `run:tas`, `run:nrt`; defaults mirror `run:qg` (`qa_manager` True, `qa_analyst` False, `qa_engineer` True, `guest` False). Catalog size 24 → 26.
- Endpoints (under `/api/v1`): QG unchanged (`/quality-gate/...`); TAS `/test-metrics/tas/runs`, `/test-metrics/tas/runs/{id}`, `DELETE /test-metrics/tas/test-runs/{id}`; NRT `/test-metrics/nrts/runs`, `/test-metrics/nrts/runs/{id}`, `GET /test-metrics/nrts/launch-options`, `DELETE /test-metrics/nrts/test-runs/{id}`.
- Backend: run `ruff` only on explicit files (never `--fix` on the whole tree); local DB-backed tests that fail with `OSError` are an environment issue, not a regression.
- Frontend: before editing `.tsx`/`.ts`, follow the existing component style; run `pnpm vitest run <file>` and `pnpm eslint <files>`.
- Each repo's README/docs gate: assess documentation once per repo at the end (Task 15); the TAS repo's own `AGENTS.md` rules apply there (`.github/instructions/` only for `.py` files, none here).

## Review Focus

1. Runs started by other systems (`tas-<their id>`) and QG runs (`tas-qg-…`) must never appear in the TAS list; NRT runs must not appear in either (Task 4).
2. An NRT selection with an unknown suite id, or "all" for an environment with no configured suite, is rejected with 422 before dispatching (Task 5).
3. Clearing a run note (empty string) stores null and the note can be removed through `PUT /nrts/update` (Task 2).
4. After moving the NRT page to `OriginNrtsPage`, a 404 "no NRTs" is still an empty list and flaky scenarios still reach the table (Task 10).
5. Deleting the run whose details are open closes the details and the list refreshes; deleting is hidden without the page's permission (Task 11).

---

### Task 1: NRT workflow accepts a correlation id (TAS repo)

**Files:**
- Modify: `.github/workflows/nrt-main-dispatch-tests.yml`

**Interfaces:**
- Produces: dispatch input `correlation_id` (string, optional, default `''`); run name `nrt-<correlation_id>` when set.

- [ ] **Step 1:** In the TAS repo: `git checkout main && git pull --ff-only && git checkout -b feature/nrt-dispatch-correlation-id`.
- [ ] **Step 2:** Under `on.workflow_dispatch.inputs` add `correlation_id` (description "Optional id from the caller (e.g. the Central Hub) to locate this run", `required: false`, `type: string`, `default: ''`).
- [ ] **Step 3:** Add a top-level `run-name:` (next to `permissions:`) with the expression `${{ inputs.correlation_id != '' && format('nrt-{0}', inputs.correlation_id) || '' }}`. Scheduled runs have no inputs, so the expression is empty and GitHub keeps its default title.
- [ ] **Step 4:** Verify the YAML parses (`ruby -ryaml -e 'YAML.load_file(".github/workflows/nrt-main-dispatch-tests.yml")'`) and `git diff` shows only the input and `run-name`.
- [ ] **Step 5:** Note in the final report that behavior with an empty `run-name` and the hub-style dispatch must be checked with a real run (cannot run workflows locally).

### Task 2: Run note: column, model, schemas, update (backend)

**Files:**
- Create: `alembic/versions/0030_add_test_runs_note.py`
- Modify: `app/models/test_metrics.py` (TestRun), `app/schemas/test_metrics.py` (`TestRunCreate`, `TestRunOut`), `app/services/test_metrics.py:143-176` (`update_nrt`)
- Test: `tests/test_test_metrics_note.py`

**Interfaces:**
- Produces: `TestRun.note` (`Column(String, nullable=True)`), `note: str | None = None` in `TestRunCreate`/`TestRunOut`; `update_nrt` persists `note`, storing `""` as `None`.

- [ ] **Step 1:** In the backend repo: `git checkout main && git pull --ff-only && git checkout -b feature/nrt-tas-run-management`.
- [ ] **Step 2:** Write `tests/test_test_metrics_note.py` (mock `AsyncSession` like `tests/test_quality_gate.py::_fake_db`): `test_test_run_out_exposes_note` (`TestRunOut.model_validate` of an object with `note="x"` gives `.note == "x"`); `test_update_nrt_persists_note` (call `update_nrt` with `UpdateNrtsObject(test_runs=[TestRunCreate(id=..., note="n")])`; assert the executed update values contain `{"id": ..., "note": "n", ...}`); `test_update_nrt_clears_empty_note` (note `""` is sent as `None`).
- [ ] **Step 3:** Run `uv run pytest tests/test_test_metrics_note.py -v`; expected FAIL.
- [ ] **Step 4:** Add the model column and schema fields; add `"note"` to the `include` set in `update_nrt` and normalize `""` to `None` before the update.
- [ ] **Step 5:** Write migration `0030` (revision `"0030"`, down `"0029"`) like `0029`: upgrade `ALTER TABLE qachub.test_runs ADD COLUMN IF NOT EXISTS note VARCHAR`, downgrade `op.drop_column("test_runs", "note", schema="qachub")`.
- [ ] **Step 6:** Run the new tests: expect PASS. Run `uv run ruff check` and `uv run ruff format --check` on the touched files only.

### Task 3: Permissions `run:tas` and `run:nrt` (backend)

**Files:**
- Modify: `app/core/permissions.py` (after the `run:qg` entry), `tests/test_permissions_catalog.py`

**Interfaces:**
- Produces: catalog entries `run:tas` (label "TAS (lancia ed elimina esecuzione)", category "Test automation service - Test Results") and `run:nrt` (label "NRTs (lancia ed elimina esecuzione)", category "Test Results"), defaults as in Global Constraints.

- [ ] **Step 1:** Update `test_action_catalog_size` to 26 and add `test_run_actions_mirror_run_qg_defaults` asserting `run:tas` and `run:nrt` have the same `defaults` as `run:qg`. Run: FAIL.
- [ ] **Step 2:** Add the two catalog entries. Run `uv run pytest tests/test_permissions_catalog.py -v`: PASS.

### Task 4: Origin-parametric status, list and delete (backend service)

**Files:**
- Modify: `app/services/quality_gate.py`, `app/config.py` (`nrt_workflow_file: str = "nrt-main-dispatch-tests.yml"`)
- Test: `tests/test_quality_gate.py` (extend), `tests/test_run_origins.py`

**Interfaces:**
- Produces:
  - `ORIGINS: dict[str, OriginConfig]` for `"QG"`, `"TAS"`, `"NRT"`; `OriginConfig` is a frozen dataclass `(name, workflow_setting: str, run_name_prefix: str, correlation_regex: re.Pattern[str], title_regex: re.Pattern[str])` (workflow_setting is the `settings` attribute name: `tas_workflow_file` or `nrt_workflow_file`).
  - `get_quality_gate_run_status(correlation_id: str, origin: str = "QG") -> QualityGateRunStatus` (run name `f"{prefix}{correlation_id}"`, prefix `tas-` for QG/TAS, `nrt-` for NRT).
  - `list_quality_gate_runs(origin: str = "QG") -> list[QualityGateRunStatus]` keeping only `display_title` matching the origin's `title_regex` (same active/recent rules as today).
  - `delete_quality_gate_run(db, run_id, origin: str = "QG") -> None` using `origin_matches`; audit detail includes `"origin"`.
- Existing QG signatures keep working (default `origin="QG"`).

- [ ] **Step 1:** Write `tests/test_run_origins.py`: `test_title_regexes_keep_origins_apart` (parametrized: `tas-qg-<hex>` matches only QG; `tas-<hex>` matches only TAS; `tas-checkout-123` and `tas-12345` match none; `nrt-<hex>` matches only NRT); `test_list_runs_per_origin` (a fake GitHub list with all four kinds returns only the requested origin's runs, and the TAS correlation id is the bare hex); `test_status_run_name_per_origin` (`find_workflow_run_by_name` called with `tas-qg-…`, `tas-<hex>`, `nrt-<hex>` respectively, and `settings.nrt_workflow_file` used for NRT); `test_delete_per_origin` (TAS run deletes with TAS origin and audits `"origin": "TAS"`; a QG run asked as TAS raises `NotAQualityGateRunError`; NRT origin accepts an `origin=None` run and refuses TAS/QG runs). Run: FAIL.
- [ ] **Step 2:** Implement `OriginConfig`/`ORIGINS` and add the `origin` parameter to the three functions, preserving current QG behavior (`RUN_NAME_PREFIX` constant may be replaced by the config).
- [ ] **Step 3:** Run `uv run pytest tests/test_run_origins.py tests/test_quality_gate.py -v`: PASS (existing QG tests untouched). Ruff on touched files.

### Task 5: Launch for TAS and NRT (backend service, schemas)

**Files:**
- Modify: `app/services/quality_gate.py`, `app/schemas/quality_gate.py`
- Test: `tests/test_run_origins.py`

**Interfaces:**
- Consumes: `ORIGINS` from Task 4.
- Produces:
  - `launch_quality_gate_run(db, body: QualityGateRunCreate, origin: str = "QG") -> QualityGateRunOut`: for `TAS` the correlation id is `uuid.uuid4().hex`, `isQualityGateRun` is `"false"`, `run_name` is `tas-<hex>`.
  - `NrtRunCreate(BaseModel)`: `suites: Literal["all"] | list[Annotated[str, Field(pattern=SUITE_PATTERN)]] = "all"`, `target_env: Literal["dev", "uat"] = "uat"`.
  - `NrtSuiteOut(BaseModel)`: `id: str`, `label: str`, `envs: list[str]`; `NrtLaunchOptionsOut`: `available: bool`, `suites: list[NrtSuiteOut]`.
  - `async def get_nrt_launch_options() -> NrtLaunchOptionsOut` reading `.github/nrt-suites.json` at `settings.tas_ref` through `GitHubClient.get_file_content`, cached 60 s (reuse the pattern of `get_suite_catalog`); `available=False, suites=[]` when unreadable.
  - `async def launch_nrt_run(db, body: NrtRunCreate) -> QualityGateRunOut`: dispatches `settings.nrt_workflow_file` with inputs `suites` (`"all"` or comma-joined), `target_env`, `correlation_id=<hex32>`; audits like QG; run name `nrt-<hex>`.
  - `class NrtSuiteSelectionError(Exception)` with `.unknown: list[str]` and `.target_env: str`, raised when an id is not in the registry or no suite supports `target_env` (only when the registry is readable).
- Reuses `QualityGateConfigError`, the allowlist (QG only), audit helper.

- [ ] **Step 1:** Tests: `test_launch_tas_uses_bare_hex_and_origin_flag` (inputs have `isQualityGateRun == "false"`, `correlation_id` is 32 hex, `run_name == f"tas-{id}"`, audit written); `test_launch_nrt_dispatches_nrt_workflow` (workflow file, `suites` joined `"a,b"`, `correlation_id` present, `run_name == f"nrt-{id}"`); `test_launch_nrt_rejects_unknown_suite_and_empty_env` (registry fake: unknown id → `NrtSuiteSelectionError` with `.unknown`; `all` with `target_env` no suite supports → error); `test_nrt_launch_options_cached_and_unavailable` (parsed list from a fake registry JSON; `available False` on `httpx.HTTPError`). Run: FAIL.
- [ ] **Step 2:** Implement the schemas, `launch_nrt_run`, `get_nrt_launch_options`, the `origin` parameter on the QG launch (TAS keeps the same allowlist and suite/type catalog validation as QG, since both dispatch the same workflow).
- [ ] **Step 3:** Run the tests: PASS. Ruff on touched files.

### Task 6: Endpoints for TAS and NRT (backend API)

**Files:**
- Create: `app/api/v1/run_launch.py`
- Modify: `app/api/v1/router.py` (include with `prefix="/test-metrics"`, tag `test-metrics`), `app/schemas/quality_gate.py` (path patterns)
- Test: `tests/test_run_launch_api.py`

**Interfaces:**
- Consumes: Tasks 4 and 5.
- Produces (router paths relative to `/api/v1/test-metrics`): `POST /tas/runs` (body `QualityGateRunCreate`, 202 `QualityGateRunOut`), `GET /tas/runs`, `GET /tas/runs/{correlation_id}` (pattern `^[0-9a-f]{32}$`), `DELETE /tas/test-runs/{run_id}` (204); `POST /nrts/runs` (body `NrtRunCreate`, 202), `GET /nrts/runs`, `GET /nrts/runs/{correlation_id}`, `GET /nrts/launch-options`, `DELETE /nrts/test-runs/{run_id}`.
- Error mapping identical to `app/api/v1/quality_gate.py`: config 503; suite not allowed / type mismatch / `NrtSuiteSelectionError` 422 with a readable `detail` (`"Unknown suite(s): a, b"` or `"No suite is configured for 'dev'"`); `httpx.HTTPError` 502; delete 404 not found, 403 wrong origin.

- [ ] **Step 1:** Tests mirroring the existing QG API tests (`client` fixture, monkeypatched services): 202 body for both launch routes; 422 for invalid bodies (bad `target_env`, bad suite pattern); error mapping per exception; status/list routes call the service with the right `origin`; bad correlation id → 422; delete 204/404/403 with `origin` `"TAS"`/`"NRT"`; `launch-options` returns the options. Run: FAIL.
- [ ] **Step 2:** Implement `run_launch.py` with small origin-bound handlers (a helper that builds the status/list/delete routes per origin keeps TAS and NRT symmetrical) and include it in `router.py`.
- [ ] **Step 3:** Run `uv run pytest tests/test_run_launch_api.py tests/test_quality_gate.py -v`: PASS. Ruff on touched files.

### Task 7: OpenAPI spec and backend checks (backend)

**Files:**
- Modify: `openapi/qa-hub-be.json` (generated)

- [ ] **Step 1:** Regenerate: `uv run --extra dev python -m scripts.generate_openapi --apim-compatible`; confirm the diff only adds the new routes and `note`.
- [ ] **Step 2:** Run `uv run pytest tests/test_generate_openapi.py tests/test_permissions_catalog.py tests/test_quality_gate.py tests/test_run_origins.py tests/test_run_launch_api.py tests/test_test_metrics_note.py -v`: PASS; ruff on all touched files (restore by hand any unrelated hunk `ruff format` rewrites in old code).

### Task 8: Frontend branch, types and origin-parametric hooks

**Files:**
- Modify: `src/types/index.ts` (`TestRun.note?: string | null`), `src/utils/nrtUtils.ts` (`RunOrigin = "TAS" | "QG" | "NRT"`), `src/hooks/useQualityGate.ts`
- Test: `src/hooks/useQualityGate.test.tsx`

**Interfaces:**
- Produces in `useQualityGate.ts`:
  - `const RUN_API: Record<RunOrigin, string>` = `{QG: "/api/v1/quality-gate", TAS: "/api/v1/test-metrics/tas", NRT: "/api/v1/test-metrics/nrts"}`.
  - `useLaunchRun<T>(origin: RunOrigin)` returning a mutation of `(request: T) => QualityGateRunResponse` posting to `${RUN_API[origin]}/runs`; invalidates `["runs", origin]`.
  - `useRuns(origin: RunOrigin, pendingCorrelationId?: string)` (query key `["runs", origin]`, same polling and completion invalidation as today's `useQualityGateRuns`).
  - `useDeleteRun(origin: RunOrigin)` calling `DELETE ${RUN_API[origin]}/test-runs/{id}` and invalidating `nrts` and `suites`.
  - `NrtRunRequest {suites: "all" | string[]; target_env: QualityGateEnvironment}`, `NrtLaunchOptions {available: boolean; suites: {id: string; label: string; envs: string[]}[]}`, `useNrtLaunchOptions()` (GET `${RUN_API.NRT}/launch-options`).
  - The old exports `useLaunchQualityGateRun`, `useQualityGateRuns`, `useDeleteQualityGateRun` remain as thin wrappers with `origin="QG"` so existing tests and call sites keep working until Tasks 11-12 migrate them.

- [ ] **Step 1:** In the frontend repo: `git checkout main && git pull --ff-only && git checkout -b feature/nrt-tas-run-management` (stop and ask if there are uncommitted changes).
- [ ] **Step 2:** Tests (mock `apiClient`): `useLaunchRun("TAS")` posts to `/api/v1/test-metrics/tas/runs`; `useDeleteRun("NRT")` issues `DELETE /api/v1/test-metrics/nrts/test-runs/<id>`; `useRuns("TAS")` fetches `/api/v1/test-metrics/tas/runs` and invalidates `["nrts"]` and `["suites"]` when an active run disappears from the active set; `useNrtLaunchOptions` fetches the options path. Run `pnpm vitest run src/hooks/useQualityGate.test.tsx`: FAIL.
- [ ] **Step 3:** Implement; keep the polling interval (10 s) and the completion logic unchanged. PASS; eslint on touched files.

### Task 9: Shared runs list and launch-error messages

**Files:**
- Create: `src/components/nrts/RunsStatusList.tsx` (+ `RunsStatusList.test.tsx`)
- Modify: `src/components/nrts/QualityGateLaunchPanel.tsx`

**Interfaces:**
- Produces: `RunsStatusList({ runs, label }: { runs: QualityGateRunStatus[]; label: string })` (the current `RunsList` and `describeRun`/`formatStarted`/`TONE_CLASS`, with the `aria-label` built from `label`, e.g. "Run Quality Gate recenti"); `launchErrorMessage(error: unknown, what: string): string` exported from the same file (current `errorMessage`, with "Impossibile avviare {what}." as fallback); `withPendingRun(launch, listed)` helper returning the runs array with the just-dispatched run shown as `pending`.

- [ ] **Step 1:** Write `RunsStatusList.test.tsx`: renders status labels for `pending`/`queued`/`in_progress`/`completed` with success, failure, cancelled; renders nothing for an empty list; `launchErrorMessage` maps 503, 422 (uses `detail` string when present), 502 and the fallback. Run: FAIL.
- [ ] **Step 2:** Move the code out of `QualityGateLaunchPanel.tsx` and use it there; the existing `QualityGateLaunchPanel.test.tsx` must still pass unchanged. PASS; eslint.

### Task 10: NRT page on `OriginNrtsPage`

**Files:**
- Modify: `src/components/nrts/OriginNrtsPage.tsx`, `src/app/(portal)/nrts/(list)/page.tsx`
- Test: `src/app/(portal)/nrts/(list)/page.test.tsx` (new, modeled on `tas-nrts/(list)/page.test.tsx`)

**Interfaces:**
- Consumes: `RunOrigin` with `"NRT"` (Task 8).
- Produces: `OriginNrtsPage` accepts `origin: "TAS" | "QG" | "NRT"`; for `NRT` it does not send `origin` to `useNrts`/`useNrtsSuites`/`useSuites`, passes `isTasOnly={false}`, loads `useFlakyScenarios()` and passes its data as `flakyScenariosData` (empty array for the other origins). The NRT page becomes `<OriginNrtsPage origin="NRT" title="NRTs (Non-Regression Test)">` with the NRT launch panel slot added in Task 13.

- [ ] **Step 1:** Tests: the NRT page renders the refresh button and total; a 404 error renders the empty table and not the error text; selecting a suite card filters (`useNrts` called with that `test_object` and without `origin`); flaky data from `useFlakyScenarios` reaches `NrtsTable` (assert via a mocked `NrtsTable` prop); the title text is unchanged. Run: FAIL.
- [ ] **Step 2:** Implement; delete the duplicated state and JSX from the NRT page. PASS, plus `pnpm vitest run src/app` for the TAS/QG pages; eslint.

### Task 11: Delete per origin in `NrtsTable`

**Files:**
- Modify: `src/components/nrts/NrtsTable.tsx` (lines ~110-165, ~380-451), `src/components/nrts/DeleteRunDialog.tsx` (comment only: "a run")
- Test: `src/components/nrts/NrtsTable.test.tsx`

**Interfaces:**
- Consumes: `useDeleteRun(origin)` (Task 8). `NrtsTable` `origin` prop widens to `RunOrigin`; NRT page passes `origin="NRT"`.
- Produces: `const DELETE_ACTION: Record<RunOrigin, string> = {QG: "run:qg", TAS: "run:tas", NRT: "run:nrt"}`; `canDeleteRuns = can(DELETE_ACTION[origin])`. Wherever the table derives behavior from `isTasOnly`/`origin === "QG"`, NRT keeps today's NRT behavior (`isTasOnly={false}`).

- [ ] **Step 1:** Update the test "hides the delete icon without the run:qg permission and on the TAS list" and add: for each of TAS and NRT, the trash icon shows with its action and is hidden without it; confirming calls the origin's endpoint (assert `DELETE` URL via the mocked `apiClient`); after success a selected run's details are closed (`onRowClick(undefined)` called). Run: FAIL.
- [ ] **Step 2:** Implement. PASS; eslint.

### Task 12: Launch panel for QG and TAS

**Files:**
- Create: `src/components/nrts/TestRunLaunchPanel.tsx` (+ `TestRunLaunchPanel.test.tsx`)
- Delete: `src/components/nrts/QualityGateLaunchPanel.tsx` and its test (replaced; keep the same test cases ported to the new file)
- Modify: `src/app/(portal)/quality-gate/(list)/page.tsx`, `src/app/(portal)/tas-nrts/(list)/page.tsx`

**Interfaces:**
- Produces: `TestRunLaunchPanel({ origin, suites }: { origin: "QG" | "TAS"; suites?: string[] })`: the current form, with `aria-label` "Lancia Quality Gate" / "Lancia TAS", heading and button text following the origin, the datalist id unique per origin, using `useLaunchRun<QualityGateRunRequest>(origin)`, `useRuns(origin, pendingId)`, `RunsStatusList` and `launchErrorMessage` (Task 9). The QG page keeps `<Gate action="run:qg">`; the TAS page gets `<Gate action="run:tas"><TestRunLaunchPanel origin="TAS" suites={...} /></Gate>` using `useSuites({ origin: "TAS" })`.

- [ ] **Step 1:** Port the existing `QualityGateLaunchPanel` tests to `TestRunLaunchPanel.test.tsx` for `origin="QG"` and add TAS cases: the form posts to the TAS endpoint, the label/heading say TAS, the run list uses the TAS run name format (`tas-<hex>`), and the TAS page hides the panel without `run:tas` (extend `tas-nrts/(list)/page.test.tsx`). Run: FAIL.
- [ ] **Step 2:** Implement, migrate both pages, delete the old panel and its test. Remove the now-unused QG wrappers in `useQualityGate.ts` if no call site remains. PASS; eslint; `pnpm vitest run src` for the nrts and app folders.

### Task 13: NRT launch panel

**Files:**
- Create: `src/components/nrts/NrtLaunchPanel.tsx` (+ `NrtLaunchPanel.test.tsx`)
- Modify: `src/app/(portal)/nrts/(list)/page.tsx`

**Interfaces:**
- Consumes: `useNrtLaunchOptions`, `useLaunchRun<NrtRunRequest>("NRT")`, `useRuns("NRT", pendingId)`, `RunsStatusList`, `launchErrorMessage`, `DeleteRunDialog`-style confirmation (use the `Dialog` primitives from `src/components/ui/dialog`).
- Produces: `NrtLaunchPanel()` — environment select (`dev`/`uat`, default `uat`); suite checkboxes of the options whose `envs` include the environment, plus an "Tutte le suite" choice (default selected); submit disabled when no suite is selected; submitting "Tutte" opens a confirmation dialog ("Lanciare tutte le suite NRT su {env}?") before the request is sent; request body `{suites: "all" | string[], target_env}`; when `options.available` is false the suite list is replaced by a free-text field for comma-separated ids, with a hint. Rendered in the NRT page inside `<Gate action="run:nrt">`.

- [ ] **Step 1:** Tests: switching the environment hides suites that do not support it; "Tutte" requires the confirmation and sends `suites: "all"`; selecting two suites sends them without confirmation; 422 `detail` is shown; unavailable options show the text field and still submit; the run list shows an `nrt-<hex>` run in progress. Run: FAIL.
- [ ] **Step 2:** Implement and wire into the NRT page. PASS; eslint.

### Task 14: Run note in the NRT details and report

**Files:**
- Modify: `src/components/nrts/NrtDetailsPanel.tsx`, `src/components/nrts/NrtsTable.tsx` (note change handler), `src/components/nrts/QualityGateReportView.tsx`
- Test: `src/components/nrts/NrtDetailsPanel.test.tsx`, `src/components/nrts/QualityGateReportView.test.tsx`, `src/components/nrts/NrtsTable.test.tsx`

**Interfaces:**
- Produces: `NrtDetailsPanel` prop `handleRunNoteChange: (run: NrtsListObject, event: ChangeEvent<HTMLTextAreaElement>) => void`; a labeled textarea "Note" (placed above the scenarios title, only when `!isTasOnly` i.e. NRT runs) with value `details.test_run.note ?? ""`, calling `onModifiedChange(true)`. In `NrtsTable`, the handler updates `test_run.note` in the local rows, adds the run id to `modifiedRunIds`, and is persisted by the existing Save (payload `test_run.note`). `QualityGateReportView` shows the note read-only (heading "Note", preserving line breaks) when `run.test_run.note` is a non-empty string.

- [ ] **Step 1:** Tests: the textarea appears for NRT, not for TAS/QG; typing calls the handler and marks the panel modified; Save sends the run with `note` in the `PUT /nrts/update` payload (`NrtsTable` test with mocked `apiClient`); clearing the text sends `note: ""`; the report view shows the note, and omits the section when empty. Update the existing panel tests' `renderPanel` helper with the new prop. Run: FAIL.
- [ ] **Step 2:** Implement. PASS; eslint.

### Task 15: Final verification and documentation assessment (all repos)

- [ ] **Step 1 (frontend):** `pnpm vitest run` (full), `pnpm eslint .` on touched files only if the whole tree has unrelated warnings, `pnpm tsc --noEmit`; all green.
- [ ] **Step 2 (backend):** the full test list of Task 7 plus `uv run pytest -q` (ignore the known `OSError` DB-environment failures).
- [ ] **Step 3 (visual check, optional but recommended):** run the frontend against the backend locally (backend CORS allows `http://localhost:3000`) and check that the NRT page shows refresh, trash icon and launch panel, the note field saves, and the TAS page shows its launch panel; use temporary files only and remove them.
- [ ] **Step 4 (docs):** per repo, assess whether README/docs mention the QG-only launch/delete or the NRT dispatch inputs (`docs/tas/tas-architecture.md`, backend README, frontend README) and update them (TAS: apply the `ppa-readme` skill per `AGENTS.md` before any commit); a justified "no update" is valid.
- [ ] **Step 5:** `git status` in the three repos: only the intended files changed, nothing committed. Prepare, without running them, branch name, changed files, diff summary and the commit and PR texts (English, Conventional Commits, no AI attribution) for each repo, plus the release order from the spec (TAS and backend, APIM re-import of the OpenAPI spec, then frontend).
