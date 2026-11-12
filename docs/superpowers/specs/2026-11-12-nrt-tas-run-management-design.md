# NRT and TAS run management: refresh, delete, launch, run note

Date: 2026-11-12
Repos: `pagopa-platform-integration-test` (TAS), `pagopa-qa-centralhub-backend`, `pagopa-qa-centralhub-frontend`
Status: draft, awaiting review

## 1. Goal

The NRT and TAS pages of the Central Hub get the same operational features the Quality Gate (QG) page already has:

1. **NRT**: refresh the list manually, delete a run, launch runs (one suite or all suites) with automatic list refresh on completion, and an optional general note on the run (today notes exist only per scenario).
2. **TAS**: refresh the list, delete a run, launch a run like the QG does but with origin `TAS`. The GitHub run name must be `tas-<id>`, not `tas-qg-<id>`.

Launch and delete are protected by one permission per page (`run:nrt`, `run:tas`), as `run:qg` does for the QG.

## 2. Current state (verified)

- QG backend: `app/services/quality_gate.py` and `app/api/v1/quality_gate.py` dispatch the TAS workflow with `isQualityGateRun=true` and `correlation_id=qg-<hex>`. The workflow run name is `tas-${correlation_id}`, hence `tas-qg-<hex>`. Status is read from GitHub by run name. Nothing is stored on the Hub except an audit log.
- Delete exists only for QG: `delete_quality_gate_run` refuses any other origin (`NotAQualityGateRunError`). Scenarios go by FK cascade; the published Allure report is not removed.
- The TAS workflow (`test-automation-service.yml`) derives `TEST_ORIGIN` from `isQualityGateRun` (`QG` or `TAS`) and is already dispatchable with `correlation_id`.
- The NRT workflow (`nrt-main-dispatch-tests.yml`) is already `workflow_dispatch` with inputs `suites` (`all` or comma-separated ids from `.github/nrt-suites.json`) and `target_env` (`dev`/`uat`), plus a daily schedule. It has no `correlation_id` input and no `run-name`, so the Hub cannot follow a run it launched.
- Frontend: `OriginNrtsPage` (TAS and QG) already has the refresh button. The NRT page (`src/app/(portal)/nrts/(list)/page.tsx`) is a separate copy without it. `useQualityGateRuns` already polls and, when an active run completes, invalidates the `nrts` and `suites` queries.
- `test_runs` has no run-level note column; `test_executions.revision_note` is per scenario. `PUT /nrts/update` already sends the modified `test_run` objects.

## 3. Decisions

| Topic | Decision |
|---|---|
| Permissions | One action per page: `run:nrt`, `run:tas` (launch and delete), like `run:qg`. Default roles that have `run:qg` also get them. |
| Backend structure | Generalize the QG module by origin (QG, TAS, NRT) instead of copying it per origin. |
| NRT launch | Reuse `nrt-main-dispatch-tests.yml`: add an optional `correlation_id` input and a `run-name`. No second workflow. |
| Delete | Allowed for QG, TAS and NRT, each with its own permission; the origin is recorded in the audit log. |
| Note | New nullable `note` column on `test_runs`; edited in the NRT details panel only; shown read-only in the report. |
| NRT page | Moves onto `OriginNrtsPage` with origin `NRT`, so refresh, delete and launch exist once. |

## 4. TAS repository changes

`nrt-main-dispatch-tests.yml`:

- New optional `workflow_dispatch` input `correlation_id` (string, default empty).
- New top-level `run-name`: `nrt-<correlation_id>` when the input is set, otherwise GitHub's default title. Scheduled runs are unchanged. To verify on a real run that an empty `run-name` expression falls back to the default title.
- Nothing else changes: `suites` and `target_env` keep their meaning and validation (unknown suite ids and suite/environment mismatches already fail in the `prepare` job).

`test-automation-service.yml` needs no change: TAS runs are dispatched with `isQualityGateRun=false` and a bare `<hex>` correlation id, giving run name `tas-<hex>` and origin `TAS`.

## 5. Backend design

### 5.1 Origin-parametric launcher

`app/services/quality_gate.py` keeps its public behavior for QG and gains an origin configuration. Per origin:

| | QG | TAS | NRT |
|---|---|---|---|
| Workflow | `settings.tas_workflow_file` | `settings.tas_workflow_file` | `nrt-main-dispatch-tests.yml` (new setting `nrt_workflow_file`) |
| Correlation id | `qg-<hex32>` | `<hex32>` | `<hex32>` |
| Run name | `tas-qg-<hex32>` | `tas-<hex32>` | `nrt-<hex32>` |
| Recognition regex (run name) | `^tas-qg-[0-9a-f]{32}$` | `^tas-[0-9a-f]{32}$` | `^nrt-[0-9a-f]{32}$` |
| Dispatch inputs | existing, `isQualityGateRun=true` | existing, `isQualityGateRun=false` | `suites`, `target_env`, `correlation_id` |

The exact-regex recognition keeps runs started by other systems (which use their own `tas-<their id>` names) out of the Hub lists. Hub-launched TAS and QG runs cannot be confused (`tas-qg-` has a non-hex segment).

Shared behavior, unchanged from QG: configuration check (503), suite allowlist, suite/type validation against the TAS repository catalog, audit log entry on dispatch (success and failure), GitHub errors mapped to 502, status read from GitHub, active runs always listed and finished ones for one hour, at most 20.

### 5.2 NRT launch options and validation

- A new function reads `.github/nrt-suites.json` from the TAS repository (same GitHub client and short cache as the suite catalog) and returns `[{id, label, envs}]`.
- `POST /nrts/runs` takes `{suites: "all" | [ids], target_env: "dev" | "uat"}`. The server rejects unknown ids and a selection with no suite configured for the environment (422), mirroring the workflow's own check. When the registry cannot be read, validation is skipped and the workflow remains the final check (same rule as the QG catalog).

### 5.3 Endpoints (all under `/api/v1`)

- QG, unchanged: `POST /quality-gate/runs`, `GET /quality-gate/runs`, `GET /quality-gate/runs/{id}`, `DELETE /quality-gate/test-runs/{id}`.
- TAS: `POST /tas/runs`, `GET /tas/runs`, `GET /tas/runs/{id}`, `DELETE /tas/test-runs/{id}`.
- NRT: `POST /nrts/runs`, `GET /nrts/runs`, `GET /nrts/runs/{id}`, `GET /nrts/launch-options`, `DELETE /nrts/test-runs/{id}`.

Exact paths follow the existing report routes (`/tas/test-runs/{id}`, `/nrts/test-runs/{id}` under `test_metrics`); the plan fixes the router prefixes. The correlation id path pattern is per origin.

### 5.4 Delete

`delete_quality_gate_run` becomes `delete_run(db, run_id, origin)`: 404 when missing, 403 when the run does not belong to the origin asked for (`origin_matches`, so the NRT origin means "any origin other than TAS and QG"). The audit entry records the origin. Scenarios are removed by FK cascade; flaky-scenario rows and the published Allure report are untouched.

### 5.5 Run note

- Alembic `0030`: `ALTER TABLE test_runs ADD COLUMN note TEXT NULL` (schema `qachub`), reversible.
- `TestRun.note` in the model, `note` in `TestRunOut` and in the update payload of `PUT /nrts/update`. Empty string is stored as null.

### 5.6 Permissions

`ACTION_CATALOG` gets `run:nrt` and `run:tas` (labels in Italian like the others); their `defaults` mirror `run:qg` (`qa_manager` and `qa_engineer` true, `qa_analyst` and `guest` false). Role defaults live only in the catalog (`compute_role_matrix`), so no data migration is needed; a role that stored an explicit override for an action is not affected by new defaults. `test_permissions_catalog.py` counts are updated (24 → 26). As for the QG, the permission is enforced in the UI (`Gate`, `can()`); the backend endpoints have no per-action guard (access goes through APIM).

### 5.7 OpenAPI

`openapi/qa-hub-be.json` is regenerated with `--apim-compatible`. After deploy, the spec must be re-imported in APIM, otherwise the new routes are not exposed (same as `/meta/openapi`).

## 6. Frontend design

- `RunOrigin` becomes `"TAS" | "QG" | "NRT"`. `OriginNrtsPage` handles `NRT`: no `origin` query parameter to the list endpoint, `isTasOnly=false`, and the flaky-scenarios data the NRT page uses today. `src/app/(portal)/nrts/(list)/page.tsx` becomes a thin wrapper like the TAS and QG pages.
- `useQualityGate.ts` hooks become origin-parametric (`useLaunchRun(origin)`, `useRuns(origin, pendingId)`, `useDeleteRun(origin)`) with per-origin query keys and the new endpoints. The completion mechanism is unchanged: poll every 10 s while a run is active or not yet listed; when an active run completes, invalidate `nrts` and `suites`.
- Launch panels:
  - `TestRunLaunchPanel` (origin `QG` or `TAS`): today's form (suite, test type, environment, tags, Allure) with origin-specific title, button label and `Gate` action.
  - `NrtLaunchPanel`: multi-select of suites (plus "All"), filtered by the chosen environment using `/nrts/launch-options`; environment `dev`/`uat`; confirmation dialog when "All" is selected.
  - Shared: the runs list (status chips, link to GitHub Actions) and the error messages are extracted from `QualityGateLaunchPanel`.
- Delete: `NrtsTable` enables the trash icon by page origin and permission (`run:qg`, `run:tas`, `run:nrt`) and calls the origin's endpoint; `DeleteRunDialog` is unchanged.
- Note: a "Note" textarea above the scenarios in `NrtDetailsPanel` for NRT runs, saved with the existing Save button (the run joins `modifiedRunIds`); shown read-only in `RunReportPage` when present.
- Permissions: `run:nrt` and `run:tas` added to the frontend action list and role matrix.

## 7. Errors and edge cases

- Launch errors use the QG messages: 503 not configured, 422 invalid parameters or unknown suite, 502 GitHub rejected the dispatch.
- A run not yet listed by GitHub shows "Avvio in corso…" until the poll finds it.
- Launching "all" NRT suites is expensive: the UI asks for confirmation. No server-side concurrency limit is added (YAGNI); the audit log records every dispatch.
- Deleting an NRT or TAS run is permanent and loses its scenarios and any run note; the confirmation dialog already says so.

## 8. Testing

- Backend (pytest): run-name and correlation-id formats per origin, recognition regexes (including that `tas-qg-…` is not a TAS run and external `tas-<id>` runs are ignored), NRT validation (unknown id, env mismatch), delete per origin (404/403/204) with audit, note persistence through `PUT /nrts/update`, permission catalog counts and guards. DB-backed tests that already fail locally for environment reasons stay out of scope.
- Frontend (vitest): the NRT page after the move (existing flows, flaky scenarios, refresh, delete), launch panels (QG and TAS forms, NRT suite filter by environment, confirmation for "All"), note field in the details panel and in the report, permission gating of launch and delete per page.
- TAS: the workflow change cannot run locally; verified by a manual dispatch with and without `correlation_id` and by a scheduled run staying unchanged.

## 9. Delivery

One feature branch per repo, cut from an updated `main`, no commits until the user decides:

1. TAS: `nrt-main-dispatch-tests.yml`.
2. Backend: migration, service, endpoints, permissions, OpenAPI.
3. Frontend: the changes above.

Release order: TAS and backend (migration included) first, then APIM re-import of the OpenAPI spec, then the frontend, since the UI calls the new routes. Repository documentation (READMEs) is assessed per repo as required by each repo's instructions.

## 10. Out of scope

- Run note on TAS and QG pages (the column supports it; no UI).
- Server-side limits on concurrent launches.
- Removing the published Allure report when a run is deleted.
- Cancelling a running workflow from the Hub.
