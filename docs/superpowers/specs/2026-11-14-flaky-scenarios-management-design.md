# Flaky scenarios management section

Date: 2026-11-14
Repos: `pagopa-qa-centralhub-backend`, `pagopa-qa-centralhub-frontend`
Status: draft, awaiting review

## 1. Intent

Today a reviewer can mark failed scenarios as flaky from the NRT details panel. The flag is stored in
`qachub.flaky_scenarios` and later runs highlight those scenarios by default. This is not enough:

- if a flaky scenario stops failing, nobody notices;
- there is no place to review flags or to remove one;
- the flag is bound to a single `test_version` (and, through `suite_id`, to a single
  `test_object_version`), so it silently disappears when a new version of the test or of the product
  is released.

The new section "Scenari flaky" gives reviewers one place where they see, for every scenario, an
objective flakiness index computed on run history, next to the current flag. It makes the two
mismatches obvious — highly flaky but not flagged, flagged but now stable — and lets them fix the
flag on the spot.

## 2. Goals, non-goals, anti-goals

Goals
- New sidebar entry and page listing scenarios with their flakiness index, history signals and flag.
- Flakiness index normalized to levels 1–5, shown as a 5-segment green→red indicator (variant A of
  the mockup) plus the 0–100 score.
- Hint boxes and row highlighting for: to mark, to unmark, broken.
- Mark/unmark from the page, tracked (who, when).
- Flag belongs to the scenario, not to a version: a new test or product version inherits it.
- Daily automatic recompute at 12:00 Europe/Rome plus a manual "Ricalcola ora".
- Read for every role, write for every role except `guest`.

Non-goals
- Automatic marking/unmarking: the index only suggests, the reviewer decides.
- Changing how Allure retries work or how runs are ingested.
- Removing the old `flaky_scenarios` table in this change (it is frozen, dropped in a follow-up).

Anti-goals (failure even if it works)
- A scenario that always fails is shown as "very flaky": it is broken, not flaky.
- Developer reruns (`MANUAL`) inflate the index of scenarios under construction.
- Flags still vanish on version bumps.
- A mark made in the NRT panel and one made in the new page disagree.

## 3. Current state (verified in code)

- `qachub.flaky_scenarios` (model `FlakyScenario` in `app/models/test_metrics.py`): PK
  `suite_id + scenario_name + test_version`; columns `test_object`, `test_object_version`,
  `inserted_at`, `is_flaky`. No "who/when changed".
- `qachub.test_suites` is unique on `test_object + test_type + test_object_version`: a new product
  version means a new `suite_id`.
- Endpoints `POST /api/v1/test-metrics/flaky-scenarios/save` and `GET /api/v1/test-metrics/flaky-scenarios`
  (service `save_or_update_flaky_scenarios`, `get_flaky_scenarios`). They check no action permission.
- `qachub.test_executions` has `status` (PASSED/FAILED/BROKEN/SKIPPED), `retries`, `scenario_name`,
  `run_id`; `qachub.test_runs` has `suite_id`, `env`, `trigger_type`
  (MANUAL/CRON/CI_PIPELINE/TAS/QA_GATE), `timestamp_start`, `test_version`, `test_origin`.
- Frontend: flags are read with `useFlakyScenarios` and toggled in `NrtDetailsPanel` /
  `NrtsTable` (also used by `OriginNrtsPage` for TAS/QG). Matching is done by
  `isFlakyScenarioEqual` in `src/utils/nrtUtils.ts` on `suite_id + scenario_name + test_version`.
- Permissions: `ACTION_CATALOG` in `app/core/permissions.py` (defaults per role); feature
  visibility: `FEATURE_CATALOG` in `app/core/features.py`; sidebar: `NAV_GROUPS` in
  `src/components/shell/Sidebar.tsx` (each item has `action` and `featureKey`).
  Action permissions are enforced in the frontend only; the backend has `require_role` in
  `app/deps.py` but no per-action check.
- Celery: `app/tasks/__init__.py` (`celery_app`), beat schedule in `app/tasks/schedule.py`.
  Beat runs in UTC.
- `audit_logs` table exists and is used by the Quality Gate service.

## 4. Approaches considered

| | Approach | Trade-off |
|---|---|---|
| A | Keep `flaky_scenarios` per version, read "latest row per scenario", add `updated_*` and a scores table | Smallest migration, but versions keep mixing and unmarking means writing a new "not flaky" row. |
| **B** | **New per-scenario registry + per-env score table; old table frozen** | **One source of truth, inheritance for free, simple queries for hints. Needs a data migration and a change in the NRT panel matching.** |
| C | Compute the score on request | Discarded: slow on `test_executions`, makes the daily job pointless. |

Chosen: **B** (agreed with the user).

## 5. Data model

### 5.1 Scenario identity

A scenario is identified by **`test_object + test_type + scenario_name`**. Product version
(`test_object_version`, i.e. `suite_id`) and `test_version` are not part of the key, so flag and
history carry over across versions (user decision).

### 5.2 `qachub.flaky_registry` (new) — the flag

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `test_object` | string, not null | |
| `test_type` | string, not null | |
| `scenario_name` | string, not null | |
| `is_flaky` | bool, not null | |
| `flagged_by` | string, null | user who last set `is_flaky=true` |
| `flagged_at` | timestamptz, null | |
| `updated_by` | string, null | user of the last change (mark or unmark) |
| `updated_at` | timestamptz, not null | |
| `inserted_at` | timestamptz, not null, default now | |

Unique `(test_object, test_type, scenario_name)`. Rows are created only when someone flags a
scenario (from the NRT panel or the new page); unmarking keeps the row with `is_flaky=false`.

### 5.3 `qachub.flaky_scores` (new) — the computed index

One row per scenario **per environment**.

| Column | Type | Notes |
|---|---|---|
| `test_object`, `test_type`, `scenario_name` | string | scenario key |
| `env` | `env_enum` | |
| `score` | int 0–100, null | null when insufficient data |
| `level` | smallint 1–5, null | null when insufficient data |
| `state` | string | `OK`, `INSUFFICIENT_DATA`, `BROKEN` |
| `flip_rate` | numeric(5,4) | weighted, 0–1 |
| `retry_pass_rate` | numeric(5,4) | weighted, 0–1 |
| `runs_analyzed` | int | non-skipped executions in the window |
| `last_failure_at` | timestamptz, null | |
| `recent_outcomes` | JSONB | up to 30 items `{run_id, ts, outcome: P/R/F}`, oldest first |
| `last_suite_id` | UUID | suite of the most recent execution (for links and for the suite filter) |
| `computed_at` | timestamptz | |

PK `(test_object, test_type, scenario_name, env)`. Rows not produced by a recompute (no
executions left in the window) are deleted by that recompute.

### 5.4 `qachub.flaky_recompute_status` (new, singleton row `id = 1`)

Same pattern as `sanp_health_sync_status`: `last_attempt_at`, `last_success_at`, `last_error`,
`last_trigger` (`SCHEDULE`/`MANUAL`), `last_requested_by`, `scored_count`. Used by the page header
("Ultimo ricalcolo …"). Concurrency is guarded by a Postgres advisory transaction lock, as
`sanp_health.sync_from_source` does.

### 5.5 Migration (Alembic `0031_flaky_registry_and_scores`)

1. Create the three tables.
2. Copy `flaky_scenarios` into `flaky_registry`: join `test_suites` on `suite_id` to get
   `test_type`; group by the new key; for each group keep the `is_flaky` of the row with the latest
   `inserted_at`; set `flagged_at`/`updated_at` to that `inserted_at`, `flagged_by`/`updated_by`
   null.
3. Leave `flaky_scenarios` in place, no longer written. Dropping it is a follow-up.

## 6. Flakiness algorithm

### 6.1 Input

- Executions joined to their run, for scenarios matching the key.
- Only runs with `trigger_type` in `CRON`, `CI_PIPELINE`, `TAS`. Excluded: `MANUAL` (developer
  reruns) and `QA_GATE` (release candidates, where a failure is usually a real regression).
  *(Constant list in one place, easy to change.)*
- Window: `timestamp_start` in the **last 30 days**, then at most the **30 most recent** executions
  per scenario and env.
- Per environment: `DEV`, `UAT`, `PROD` computed separately.
- Outcome of each execution:
  - `P` = PASSED with `retries` 0 or null
  - `R` = PASSED with `retries` > 0 (passed on retry)
  - `F` = FAILED or BROKEN
  - SKIPPED executions are ignored.

### 6.2 Signals

Recency weight of an execution: `w = 0.5 ^ (age_days / 14)` (half-life 14 days).

- **Flip rate**: over consecutive pairs of outcomes, a flip is a change between "pass" (`P`/`R`)
  and `F`. `flip_rate = Σ w_pair · flip / Σ w_pair`, with `w_pair` the weight of the newer item.
- **Retry-pass rate**: `Σ w · [outcome = R] / Σ w`.

### 6.3 Score, level, state

- `score = round(100 · max(flip_rate, retry_pass_rate))`.
- Level: `1` if score < 5, `2` if 5–14, `3` if 15–29, `4` if 30–49, `5` if ≥ 50.
- `state`:
  - `INSUFFICIENT_DATA` when `runs_analyzed < 5` (score and level null).
  - `BROKEN` when the last 5 outcomes are all `F` (score/level still stored, but the UI shows
    "Rotto").
  - `OK` otherwise.

Thresholds, window, half-life and minimum runs live in one config object
(`app/config.py` settings with defaults) so they can be tuned without code changes.

### 6.4 Hints (derived at query time)

| Hint | Rule |
|---|---|
| **Da marcare** | `state = OK`, `level ≥ 4`, not flagged |
| **Da smarcare** | flagged, `state = OK`, `level = 1`, `runs_analyzed ≥ 10` |
| **Rotto** | `state = BROKEN` (flagged or not) |

### 6.5 Recompute

- Celery task `app.tasks.recompute_flaky_scores.recompute_flaky_scores` computes all scores with
  one SQL pass over the window (window functions for "last 30 per scenario/env"), the signals in
  Python, then replaces `flaky_scores` in a single transaction.
- Beat entry daily at **12:00 Europe/Rome**: `crontab(hour=12, minute=0, nowfun=<now in
  Europe/Rome>)`, so DST is handled and the global Celery timezone (other jobs assume UTC) is not
  touched.
- Manual trigger: the API calls the same service function synchronously, like the SANP Health
  "sync" button (the computation is one SQL pass over 30 days). If the advisory lock is held,
  return 409 and the UI shows "Ricalcolo in corso".

## 7. Backend API

New router `app/api/v1/flaky.py`, prefix `/api/v1/flaky`:

| Method | Path | Action | Purpose |
|---|---|---|---|
| GET | `/scenarios` | `view:flaky` | List. Query: `env` (required, default UAT in the UI), `test_object` (multi), `flag` (`all`/`flagged`/`unflagged`), `min_level`, `hint` (`to_mark`/`to_unmark`/`broken`), `q` (name search), paging/sorting as in other paginated lists. Rows join `flaky_scores` with `flaky_registry` (scenarios flagged but without scores also appear, with `state = INSUFFICIENT_DATA`). |
| GET | `/summary` | `view:flaky` | Counts for the hint boxes for the current filters except `hint`: to mark, to unmark, broken, flagged, analyzed. |
| GET | `/scenarios/history` | `view:flaky` | Key + env → executions in the window with run id, timestamp, outcome, retries, error message, `allure_url`, test version. For the side panel. |
| PUT | `/scenarios/flag` | `manage:flaky` | Body: key + `is_flaky`. Upserts the registry row, sets `updated_*` (and `flagged_*` when true), writes an `audit_logs` entry (`flaky.mark` / `flaky.unmark`). |
| POST | `/recompute` | `manage:flaky` | Run recompute now; 200 with the new status, 409 if running. |
| GET | `/recompute/status` | `view:flaky` | Last status row. |

Existing endpoints keep their contract so the NRT/TAS/QG panels keep working:

- `POST /test-metrics/flaky-scenarios/save`: same body; the server resolves `test_type` from
  `suite_id` and upserts `flaky_registry` (no more writes to `flaky_scenarios`). The user comes
  from the token.
- `GET /test-metrics/flaky-scenarios`: returns registry rows with `is_flaky = true` for the
  requested `test_object`; the response keeps the old fields where they make sense and adds
  `test_type`.

Authorization and identity: the backend has no authentication today (`verify_jwt` in
`app/core/auth.py` is a stub and no endpoint reads the caller). As in the rest of the Hub, the
"Action" column is enforced by the frontend (`RouteGuard`, `usePermissions().can`), and the user's
email is sent in the payload, as `NrtsTable` does for `revisioned_by`:
`PUT /scenarios/flag` and `POST /recompute` take `user` (email) in the body; the legacy
`flaky-scenarios/save` takes an optional `user` field. Backend enforcement is a follow-up that
depends on real JWT verification.

## 8. Permissions and feature visibility

`ACTION_CATALOG`, category "Test Results":

| Key | Label | qa_manager | qa_analyst | qa_engineer | guest |
|---|---|---|---|---|---|
| `view:flaky` | Scenari flaky (visualizza) | ✓ | ✓ | ✓ | ✓ |
| `manage:flaky` | Scenari flaky (marca/smarca, ricalcola) | ✓ | ✓ | ✓ | ✗ |

`FEATURE_CATALOG`: new feature `flaky_scenarios`.

## 9. Frontend

### 9.1 Navigation

`NAV_GROUPS` → "Test Results", right after "Non-Regression Tests (NRT)":
`{ href: "/flaky-scenarios", label: "Scenari flaky", icon: <lucide icon, e.g. Bug or Shuffle>,
action: "view:flaky", featureKey: "flaky_scenarios" }`. Route `src/app/(portal)/flaky-scenarios/`.

### 9.2 Page (see mockup)

Header: title, "Ultimo ricalcolo: <time> (automatico|manuale, by user)", window description;
buttons "ⓘ Come si calcola" (popover explaining signals, levels and hint rules) and
"↻ Ricalcola ora" (hidden without `manage:flaky`; spinner while the request runs, list and summary
refetched when it returns, "Ricalcolo in corso" on 409).

Filters, as in the other list pages: Ambiente (single, default UAT), Suite (`test_object`,
multi), Flag (all/flagged/unflagged), Livello minimo, search by name. Filters are kept in the URL.

Hint boxes (4, clickable, toggle the `hint` filter; active one is marked):
1. **Da marcare** (red), 2. **Da smarcare** (green), 3. **Rotti** (dark grey),
4. **Marcati flaky** (neutral, "su N scenari analizzati").

Table, default sort level desc then score desc:

| Column | Content |
|---|---|
| Flakiness | Variant A: 5 segments filled up to the level, colors green → yellow → orange → red; level number; score in small grey. `Rotto` chip for BROKEN, `Dati insuff.` chip for INSUFFICIENT_DATA. |
| Scenario | Name; `test_object · test_type` in small text. |
| Ultime run | Strip of the `recent_outcomes` (green P, yellow R, red F, grey placeholders up to 30). Tooltip per item: date, outcome. |
| Flip | `flip_rate` % |
| Pass on retry | `retry_pass_rate` % |
| Run | `runs_analyzed` |
| Ultimo KO | relative date of `last_failure_at` |
| Flag | Toggle; read-only without `manage:flaky`. Optimistic update, rollback on error. |
| Marcato da | `flagged_by · flagged_at` (date) when flagged |
| Hint | Chip: "Da marcare", "Da smarcare", "Rotto: apri bug" |

Row highlighting with a 3px left bar: red for "Da marcare", green for "Da smarcare", dark grey for
"Rotto". Legend under the table.

Row click opens a side panel: flakiness summary, outcome history from `/scenarios/history` (date,
outcome, retries, test version, error message, link to Allure and to the NRT run), flag toggle.

Colors use the existing tokens (`--success`, `--warning`, `--danger`, `--neutral-soft`) plus two
intermediate level colors defined next to them in `globals.css`, for both light and dark theme.

### 9.3 Existing NRT/TAS/QG panels

- `isFlakyScenarioEqual` / `isScenarioInFlakyScenarios` match on
  `test_object + test_type + scenario_name` (suite → test_type is available on the suite object).
- `FlakyScenario` type gains `test_type`; `suite_id`/`test_version` stay in the save payload.
- Saving still goes through `useSaveFlakyScenarios`; it also invalidates the new `flaky` queries.
- Result: a flag set in the NRT panel shows in the new page and vice versa, for every version.

## 10. Testing

Backend (pytest, existing style):
- Algorithm unit tests on synthetic outcome sequences: always P → level 1; strict alternation →
  level 5; all R → high retry-pass; last 5 F → BROKEN; < 5 runs → INSUFFICIENT_DATA; MANUAL and
  QA_GATE runs ignored; SKIPPED ignored; executions older than 30 days ignored; recency weighting
  makes an old burst count less than a recent one.
- Migration test: duplicated `flaky_scenarios` rows across versions collapse into one registry row
  with the latest `is_flaky`.
- API tests: filters and hints; 409 on concurrent recompute; legacy `flaky-scenarios/save` writes
  the registry with `updated_by` from the body.
- Beat schedule test in `tests/test_celery_queue.py` style: entry exists, 12:00 Europe/Rome.

Frontend (vitest, existing style):
- Page renders levels, chips, highlights; hint box toggles filter; flag toggle hidden/disabled for
  guest; recompute button states.
- `nrtUtils` matching across versions.
- Sidebar shows the entry for every role, respecting feature visibility.

## 11. Rollout

1. Backend: migration, models, service, task, API, permissions/feature catalog.
2. First recompute runs at deploy (manual trigger or next 12:00).
3. Frontend: page, sidebar, matching change in NRT panels.
Backend first: the legacy endpoints keep their contract, so the current frontend keeps working
between the two deploys.

## 12. Calls made without explicit user input (please check)

- Scenario key `test_object + test_type + scenario_name` (product version also dropped from the
  key, not only `test_version`).
- Flag is environment-independent; score is per environment, default view UAT.
- Excluded triggers: `MANUAL` and `QA_GATE`.
- Window 30 days / max 30 runs, half-life 14 days, minimum 5 runs, level thresholds 5/15/30/50.
- "Da smarcare" requires level 1 on at least 10 runs.
- Permissions enforced in the frontend only, like every other section (the backend has no auth
  yet); the acting user's email travels in the request body.
- Old `flaky_scenarios` table frozen, dropped later.

## 13. Follow-ups (out of scope)

- Drop `flaky_scenarios`.
- Backend enforcement of `manage:flaky` once JWT verification exists.
- "Rotto: apri bug" creating a Jira issue directly (the Hub already has a Jira integration).
- Score trend over time per scenario (store daily snapshots).
- Notification (existing notification rules) when the "Da marcare" count grows.
