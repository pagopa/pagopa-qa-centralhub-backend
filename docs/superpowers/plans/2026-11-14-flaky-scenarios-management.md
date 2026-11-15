# Flaky Scenarios Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Scenari flaky" section to the QA Central Hub that ranks scenarios by a flakiness index computed on run history, shows flag/index mismatches, and lets reviewers mark and unmark flags that survive version changes.

**Architecture:** Backend (FastAPI + SQLAlchemy async + Alembic + Celery) gets a per-scenario flag registry, a per-environment score table filled by a pure scoring module, a recompute service guarded by an advisory lock (daily beat at 12:00 Europe/Rome and a synchronous manual endpoint), and a `/api/v1/flaky` router. Legacy flaky endpoints keep their contract but read/write the registry. Frontend (Next.js 16 + React Query + vitest) gets a new page, sidebar entry and permissions, and the NRT/TAS/QG panels switch to the version-independent scenario key.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Celery 5, pytest; Next.js 16.4, React, TanStack Query, lucide-react, vitest.

**Spec:** `docs/superpowers/specs/2026-11-14-flaky-scenarios-management-design.md` (backend repo).

## Global Constraints

- Two repos: `pagopa-qa-centralhub-backend` (tasks 1–8) and `pagopa-qa-centralhub-frontend` (tasks 9–13). In each repo create `feature/flaky-scenarios-management` from an up-to-date `main` (`git checkout main && git pull --ff-only`). The frontend is currently on `feature/nrt-tas-ui-refinements`: if it has uncommitted changes, stop and ask the user.
- **Commits:** the user commits. Each "Commit" step means: run the checks, show branch, changed files, diff summary and a Conventional Commit message, and wait. Never run `git commit`/`git push`.
- Everything in the repo is English (code, comments, logs, commit messages). UI copy is Italian, as in the rest of the portal.
- Scenario key: `test_object + test_type + scenario_name`. Never `suite_id` or `test_version`.
- Eligible triggers: `CRON`, `CI_PIPELINE`, `TAS`. Excluded: `MANUAL`, `QA_GATE`. SKIPPED executions ignored.
- Settings defaults (in `app/config.py`, env-overridable): `flaky_window_days=30`, `flaky_max_runs=30`, `flaky_half_life_days=14.0`, `flaky_min_runs=5`, `flaky_broken_streak=5`, `flaky_unmark_min_runs=10`, `flaky_level_thresholds="5,15,30,50"`.
- Levels: 1 if score < 5, 2 if 5–14, 3 if 15–29, 4 if 30–49, 5 if ≥ 50.
- Hints: `to_mark` = state OK, level ≥ 4, not flagged; `to_unmark` = flagged, state OK, level 1, runs_analyzed ≥ 10; `broken` = state BROKEN.
- Permissions `view:flaky` (all roles incl. guest) and `manage:flaky` (all but guest), category "Test Results". Feature key `flaky_scenarios`, route `/flaky-scenarios`. Enforced in the frontend only; the acting user's email travels in the body as `user`.
- Daily recompute at 12:00 Europe/Rome without changing Celery's global timezone.
- Follow the backend service style: `structlog` logger, `try/except → logger.exception(...); raise`, "query succeeded with 0 results" info logs.
- Backend checks: `ruff check .`, `mypy app` (if configured in `pyproject.toml`), `pytest`. Frontend checks: `pnpm lint`, `pnpm test`, `pnpm build`.

## Review Focus

1. **A scenario name that exists in two `test_type`s of the same `test_object`** must stay two rows, never merged — tested in Task 4.
2. **A scenario flagged in the old table whose suite has no runs in the window** must still appear in the list (state `INSUFFICIENT_DATA`, flag on) so it can be unflagged — tested in Task 6.
3. **Executions with the same `timestamp_start`** (several runs launched together) must order deterministically (tie-break on `run_id`), so the flip rate does not change between recomputes — tested in Task 4.
4. **Two recomputes at the same time** (beat at 12:00 and a click) must not interleave: the second gets 409 / is skipped, the table is never half-written — tested in Task 4 and Task 7.
5. **Unflagging from the NRT panel of an old version** must unflag the scenario for the new version too (one registry row) — tested in Task 8 and Task 9.

---

## Backend (`pagopa-qa-centralhub-backend`)

### Task 1: Permissions, feature catalog and settings

**Files:**
- Modify: `app/core/permissions.py` (`ACTION_CATALOG`, after `view:nrts_report`)
- Modify: `app/core/features.py` (`FEATURE_CATALOG`, after `nrts`)
- Modify: `app/config.py` (`Settings`)
- Test: `tests/test_permissions_catalog.py`, `tests/test_feature_visibility_service.py`

**Interfaces:**
- Produces: action keys `view:flaky`, `manage:flaky`; feature `flaky_scenarios` (`route="/flaky-scenarios"`, `action="view:flaky"`, `label="Scenari flaky"`); settings fields listed in Global Constraints, plus `settings.flaky_level_bounds -> tuple[int, int, int, int]` (property parsing `flaky_level_thresholds`).

- [ ] **Step 1: Write failing tests**

```python
def test_flaky_actions_defaults() -> None:
    by_key = {e["key"]: e for e in ACTION_CATALOG}
    assert by_key["view:flaky"]["defaults"] == {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": True}
    assert by_key["manage:flaky"]["defaults"] == {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False}
    assert by_key["view:flaky"]["category"] == "Test Results"

def test_flaky_feature_registered() -> None:
    feature = next(f for f in FEATURE_CATALOG if f["key"] == "flaky_scenarios")
    assert feature["route"] == "/flaky-scenarios" and feature["action"] == "view:flaky"

def test_flaky_settings_defaults() -> None:
    s = Settings()
    assert (s.flaky_window_days, s.flaky_max_runs, s.flaky_min_runs) == (30, 30, 5)
    assert s.flaky_level_bounds == (5, 15, 30, 50)
```

- [ ] **Step 2: Run** `pytest tests/test_permissions_catalog.py tests/test_feature_visibility_service.py -v` → FAIL (KeyError / StopIteration / AttributeError). Existing tests that assert catalog sizes will also need their expected counts updated.
- [ ] **Step 3: Add the two catalog entries (labels "Scenari flaky (visualizza)", "Scenari flaky (marca/smarca, ricalcola)"), the feature entry and the settings.**
- [ ] **Step 4: Run** the same tests → PASS; then full `pytest` → PASS.
- [ ] **Step 5: Commit** — proposed message `feat: add flaky scenarios permissions, feature and settings`.

### Task 2: Models and migration 0031

**Files:**
- Create: `app/models/flaky.py`
- Modify: `app/models/__init__.py` (export new models if that file aggregates models)
- Create: `alembic/versions/0031_flaky_registry_and_scores.py` (`revision="0031"`, `down_revision="0030"`)
- Test: `tests/test_flaky_models.py`

**Interfaces:**
- Produces (schema `qachub`):
  - `FlakyRegistry` (`flaky_registry`): `id` UUID PK; `test_object`, `test_type`, `scenario_name` str not null; `is_flaky` bool not null; `flagged_by` str|None; `flagged_at` tz|None; `updated_by` str|None; `updated_at` tz not null default now; `inserted_at` tz not null default now. Unique `uq_qachub_flaky_registry_key (test_object, test_type, scenario_name)`.
  - `FlakyScore` (`flaky_scores`): PK `(test_object, test_type, scenario_name, env)`; `env` uses existing `env_enum`; `score` int|None; `level` smallint|None; `state` str not null; `flip_rate`, `retry_pass_rate` Numeric(5,4) not null; `runs_analyzed` int not null; `last_failure_at` tz|None; `recent_outcomes` JSONB not null; `last_suite_id` UUID FK `test_suites.id` ON DELETE SET NULL, nullable; `computed_at` tz not null. Index on `(env, level)`.
  - `FlakyRecomputeStatus` (`flaky_recompute_status`): singleton `id=1` (check constraint like `sanp_health_sync_status`); `last_attempt_at` tz not null; `last_success_at` tz|None; `last_error` text|None; `last_trigger` str|None; `last_requested_by` str|None; `scored_count` int not null default 0.
  - Module constant in the migration: `COPY_LEGACY_FLAGS_SQL: str`.

- [ ] **Step 1: Write failing test** (real DB via `tests/_db.py`, same style as `tests/test_sanp_health_models.py`)

```python
async def test_legacy_flags_collapse_to_latest_per_scenario(db) -> None:
    # two suites = two product versions of the same test_object/test_type
    # legacy rows: (suite_v1, "S", tv1, is_flaky=True, t0), (suite_v2, "S", tv2, is_flaky=False, t1 > t0)
    await db.execute(text(migration.COPY_LEGACY_FLAGS_SQL))
    rows = (await db.execute(select(FlakyRegistry))).scalars().all()
    assert [(r.test_type, r.scenario_name, r.is_flaky, r.updated_at) for r in rows] == [("e2e", "S", False, t1)]
```

Load the migration module with `importlib.util.spec_from_file_location`. Add a second case: same `scenario_name` in a suite with a different `test_type` → two registry rows (Review Focus 1).

- [ ] **Step 2: Run** `pytest tests/test_flaky_models.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement models and migration.** `COPY_LEGACY_FLAGS_SQL` inserts into `flaky_registry` from `flaky_scenarios fs JOIN test_suites ts ON ts.id = fs.suite_id`, picking per `(ts.test_object, ts.test_type, fs.scenario_name)` the row with the latest `fs.inserted_at` (`DISTINCT ON ... ORDER BY ... inserted_at DESC`); `flagged_at = inserted_at` only when `is_flaky`; `updated_at = inserted_at`; `ON CONFLICT DO NOTHING`. `upgrade()` creates the 3 tables then runs it; `downgrade()` drops the 3 tables. Legacy table untouched.
- [ ] **Step 4: Run** `alembic upgrade head` on the local DB, then `alembic downgrade -1 && alembic upgrade head` → no errors; `pytest tests/test_flaky_models.py -v` → PASS.
- [ ] **Step 5: Commit** — `feat: add flaky registry, scores and recompute status tables`.

### Task 3: Scoring module (pure)

**Files:**
- Create: `app/services/flaky_scoring.py`
- Test: `tests/test_flaky_scoring.py`

**Interfaces:**
- Produces:
  - `class Outcome(StrEnum): PASSED="P"; PASSED_ON_RETRY="R"; FAILED="F"`
  - `def outcome_for(status: str, retries: int | None) -> Outcome | None` (None for SKIPPED; BROKEN→F)
  - `@dataclass(frozen=True) class ExecutionPoint: run_id: uuid.UUID; ts: datetime; outcome: Outcome; suite_id: uuid.UUID`
  - `@dataclass(frozen=True) class ScoringConfig: half_life_days: float; min_runs: int; broken_streak: int; level_bounds: tuple[int, int, int, int]` with `@classmethod from_settings(cls) -> ScoringConfig`
  - `@dataclass(frozen=True) class ScoreResult: score: int | None; level: int | None; state: Literal["OK", "INSUFFICIENT_DATA", "BROKEN"]; flip_rate: float; retry_pass_rate: float; runs_analyzed: int; last_failure_at: datetime | None`
  - `def compute_score(points: Sequence[ExecutionPoint], now: datetime, cfg: ScoringConfig) -> ScoreResult` — `points` already filtered and ordered oldest → newest.
  - `def level_for(score: int, bounds: tuple[int, int, int, int]) -> int`

- [ ] **Step 1: Write failing tests** (`now` fixed; helper `pts("PFPF...", days_ago=0)` builds points)

```python
CFG = ScoringConfig(half_life_days=14, min_runs=5, broken_streak=5, level_bounds=(5, 15, 30, 50))

def test_always_passing_is_level_1(): r = compute_score(pts("P"*10), NOW, CFG); assert (r.score, r.level, r.state) == (0, 1, "OK")
def test_strict_alternation_is_level_5(): r = compute_score(pts("PF"*5), NOW, CFG); assert (r.flip_rate, r.score, r.level) == (1.0, 100, 5)
def test_all_passed_on_retry(): r = compute_score(pts("R"*6), NOW, CFG); assert (r.retry_pass_rate, r.flip_rate, r.score) == (1.0, 0.0, 100)
def test_retry_counts_as_pass_for_flips(): assert compute_score(pts("PRPRPR"), NOW, CFG).flip_rate == 0.0
def test_last_five_failed_is_broken(): r = compute_score(pts("PPPPPFFFFF"), NOW, CFG); assert r.state == "BROKEN" and r.level is not None
def test_insufficient_data(): r = compute_score(pts("PFPF"), NOW, CFG); assert (r.state, r.score, r.level, r.runs_analyzed) == ("INSUFFICIENT_DATA", None, None, 4)
def test_recent_flips_weigh_more():
    old = compute_score(pts("PFPF", days_ago=28) + pts("PPPPPP", days_ago=0), NOW, CFG)
    new = compute_score(pts("PPPPPP", days_ago=28) + pts("PFPF", days_ago=0), NOW, CFG)
    assert new.score > old.score
def test_level_bounds(): assert [level_for(s, CFG.level_bounds) for s in (0, 4, 5, 14, 15, 29, 30, 49, 50, 100)] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
def test_last_failure_at_is_latest_F(): ...
def test_outcome_for(): assert [outcome_for(s, r) for s, r in [("PASSED", 0), ("PASSED", None), ("PASSED", 2), ("FAILED", 0), ("BROKEN", 1), ("SKIPPED", 0)]] == ["P", "P", "R", "F", "F", None]
```

- [ ] **Step 2: Run** `pytest tests/test_flaky_scoring.py -v` → FAIL (import error).
- [ ] **Step 3: Implement.** Algorithm (spec §6):

```
w(p) = 0.5 ** (max(0, (now - p.ts).total_seconds() / 86400) / half_life_days)
is_pass(p) = p.outcome in (P, R)
flip_rate = Σ_{i≥1} w(p_i)·[is_pass(p_i) != is_pass(p_{i-1})] / Σ_{i≥1} w(p_i)   (0.0 if n < 2)
retry_pass_rate = Σ w(p)·[p.outcome == R] / Σ w(p)                                (0.0 if n == 0)
score = round(100 · max(flip_rate, retry_pass_rate))
state = INSUFFICIENT_DATA if n < min_runs (score, level = None)
        BROKEN if the last broken_streak outcomes are all F
        OK otherwise
```
Rates rounded to 4 decimals.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: add flakiness scoring algorithm`.

### Task 4: Recompute service

**Files:**
- Create: `app/services/flaky.py`
- Test: `tests/test_flaky_service.py`

**Interfaces:**
- Consumes: Task 2 models, Task 3 `compute_score`, `outcome_for`, `ScoringConfig.from_settings`.
- Produces:
  - `RECOMPUTE_LOCK_ID = 670` (next to SANP's 669)
  - `ELIGIBLE_TRIGGERS = (TriggerTypeEnum.CRON, TriggerTypeEnum.CI_PIPELINE, TriggerTypeEnum.TAS)`
  - `class FlakyRecomputeInProgressError(RuntimeError)`
  - `ScenarioKey = tuple[str, str, str]` (test_object, test_type, scenario_name)
  - `async def load_execution_points(db: AsyncSession, now: datetime) -> dict[tuple[ScenarioKey, EnvEnum], list[ExecutionPoint]]`
  - `async def recompute_scores(db: AsyncSession, *, trigger: Literal["SCHEDULE", "MANUAL"], requested_by: str | None, now: datetime | None = None) -> FlakyRecomputeStatus`

- [ ] **Step 1: Write failing DB tests** (seed suites/runs/executions with a small factory in the test module)
  - `test_recompute_scores_one_row_per_scenario_and_env` — same scenario in UAT and DEV → 2 rows.
  - `test_manual_and_qa_gate_runs_are_ignored` — only MANUAL/QA_GATE runs → no row.
  - `test_window_and_max_runs` — executions 31 days old excluded; 35 eligible → `runs_analyzed == 30`.
  - `test_same_name_different_test_type_kept_apart` (Review Focus 1).
  - `test_same_timestamp_is_ordered_by_run_id` — recompute twice → identical `recent_outcomes` (Review Focus 3).
  - `test_recent_outcomes_payload` — list of `{"run_id": str, "ts": iso, "outcome": "P"|"R"|"F"}`, oldest first, length ≤ 30; `last_suite_id` = suite of newest execution.
  - `test_stale_rows_are_removed` — a score row whose scenario has no execution left is deleted.
  - `test_status_row_updated` — `last_success_at`, `last_trigger`, `last_requested_by`, `scored_count`; on exception `last_error` set, `last_success_at` unchanged, error re-raised.
  - `test_concurrent_recompute_raises` — hold `pg_advisory_xact_lock(670)` in another session → `FlakyRecomputeInProgressError` (Review Focus 4).
- [ ] **Step 2: Run** `pytest tests/test_flaky_service.py -v` → FAIL.
- [ ] **Step 3: Implement.** `load_execution_points`: one query `test_executions JOIN test_runs JOIN test_suites`, filters `trigger_type IN ELIGIBLE_TRIGGERS`, `status != SKIPPED`, `timestamp_start >= now - window_days`, `ROW_NUMBER() OVER (PARTITION BY test_object, test_type, scenario_name, env ORDER BY timestamp_start DESC, run_id DESC) <= max_runs`; group in Python, reverse to oldest-first. `recompute_scores`: `pg_try_advisory_xact_lock(RECOMPUTE_LOCK_ID)` (false → raise), compute all, `DELETE FROM flaky_scores` + bulk insert in the same transaction, upsert status row, commit. Record failures in the status row in a separate transaction (pattern of `sanp_health.sync_from_source`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: add flaky scores recompute service`.

### Task 5: Celery task and 12:00 Europe/Rome schedule

**Files:**
- Create: `app/tasks/recompute_flaky_scores.py`
- Modify: `app/tasks/schedule.py`
- Test: `tests/test_celery_queue.py` (extend), `tests/test_flaky_task.py`

**Interfaces:**
- Consumes: `recompute_scores`.
- Produces: task `app.tasks.recompute_flaky_scores.recompute_flaky_scores() -> dict[str, Any]` (uses `run_async_task`, like `sync_sanp_health`; logs and returns `{"status": "skipped"}` on `FlakyRecomputeInProgressError`); beat entry `"recompute-flaky-scores-daily"`.

- [ ] **Step 1: Write failing tests**

```python
def test_flaky_beat_entry_runs_at_noon_rome() -> None:
    entry = schedule.celery_app.conf.beat_schedule["recompute-flaky-scores-daily"]
    assert entry["task"] == "app.tasks.recompute_flaky_scores.recompute_flaky_scores"
    sched = entry["schedule"]
    assert sched.hour == {12} and sched.minute == {0}
    assert sched.nowfun().tzinfo == ZoneInfo("Europe/Rome")

def test_task_skips_when_running(monkeypatch) -> None: ...  # recompute_scores raises InProgress → {"status": "skipped"}
```
The existing parametrized queue test picks the new entry up automatically.
- [ ] **Step 2: Run** `pytest tests/test_celery_queue.py tests/test_flaky_task.py -v` → FAIL.
- [ ] **Step 3: Implement.** `crontab(minute=0, hour=12, nowfun=_now_rome)` with module-level `def _now_rome() -> datetime: return datetime.now(ZoneInfo("Europe/Rome"))`; register the task import in `schedule.py`.
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: schedule daily flaky scores recompute`.

### Task 6: Query service and schemas

**Files:**
- Create: `app/schemas/flaky.py`
- Modify: `app/services/flaky.py`
- Test: `tests/test_flaky_service.py` (extend)

**Interfaces:**
- Produces schemas: `FlakyHint = Literal["to_mark", "to_unmark", "broken"]`; `OutcomePointOut(run_id: UUID, ts: datetime, outcome: Literal["P","R","F"])`; `FlakyScenarioRowOut(test_object, test_type, scenario_name: str; env: str; score: int|None; level: int|None; state: str; flip_rate: float|None; retry_pass_rate: float|None; runs_analyzed: int; last_failure_at: datetime|None; recent_outcomes: list[OutcomePointOut]; last_suite_id: UUID|None; is_flaky: bool; flagged_by: str|None; flagged_at: datetime|None; hint: FlakyHint|None)`; `FlakySummaryOut(to_mark, to_unmark, broken, flagged, analyzed: int)`; `FlakyHistoryItemOut(run_id: UUID, ts: datetime, outcome: str, status: str, retries: int|None, error_message: str|None, allure_url: str|None, test_version: str|None, test_object_version: str)`; `FlakyFlagIn(test_object, test_type, scenario_name: str; is_flaky: bool; user: str)`; `FlakyFlagOut` (registry row); `FlakyRecomputeIn(user: str)`; `FlakyRecomputeStatusOut` (status row fields).
- Produces service functions:
  - `async def list_scenarios(db, *, env: EnvEnum, test_objects: list[str] | None, flag: Literal["all","flagged","unflagged"], min_level: int | None, hint: FlakyHint | None, q: str | None, page: int, page_size: int) -> PaginatedResponse[FlakyScenarioRowOut]`
  - `async def get_summary(db, *, env, test_objects, flag, min_level, q) -> FlakySummaryOut`
  - `async def get_history(db, *, env: EnvEnum, test_object: str, test_type: str, scenario_name: str) -> list[FlakyHistoryItemOut]` (same eligibility/window as recompute, newest first)
  - `async def set_flag(db, body: FlakyFlagIn, now: datetime | None = None) -> FlakyRegistry` (upsert; `updated_by/at` always; `flagged_by/at` set when `is_flaky` true, kept when false; adds an `AuditLog` with `action="flaky.mark"` or `"flaky.unmark"`, `resource_type="flaky_scenario"`, `resource_id=<registry id>`, `detail` JSON with key and user — an audit failure is logged, never raised, as in `quality_gate._audit`)
  - `async def get_recompute_status(db) -> FlakyRecomputeStatus | None`

- [ ] **Step 1: Write failing DB tests**
  - `test_list_hints` — seed scores/registry so each hint rule from Global Constraints hits once and its boundary misses (level 3 unflagged → no hint; flagged level 1 with 9 runs → no hint).
  - `test_list_includes_flagged_without_score` — registry flagged, no score in env → row with `state="INSUFFICIENT_DATA"`, `is_flaky=True` (Review Focus 2).
  - `test_list_filters_and_default_sort` — `test_objects`, `flag`, `min_level`, `q` (case-insensitive contains); order level desc nulls last, score desc, scenario_name asc; paging totals.
  - `test_summary_ignores_hint_filter` — counts match the rows of each hint.
  - `test_set_flag_upsert_and_audit_fields` — mark then unmark: one row, `flagged_by` kept, `updated_by` changes, two `audit_logs` rows `flaky.mark` then `flaky.unmark`.
  - `test_get_history_newest_first_and_eligible_only`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Rows = `flaky_scores` (filtered by env) FULL OUTER JOIN `flaky_registry` on the key, keeping registry-only rows only when `is_flaky` true; `hint` computed in SQL with a `CASE` so filtering and counting share one expression (helper `_hint_expr(settings)`).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** — `feat: add flaky scenarios query service`.

### Task 7: `/api/v1/flaky` router

**Files:**
- Create: `app/api/v1/flaky.py`
- Modify: `app/api/v1/router.py` (`router.include_router(flaky.router, prefix="/flaky", tags=["flaky"])`)
- Test: `tests/test_flaky_api.py`

**Interfaces:**
- Consumes: Task 6 functions, Task 4 `recompute_scores`.
- Produces endpoints (spec §7): `GET /scenarios` (query `env` default `UAT`, `test_object` repeated, `flag` default `all`, `min_level`, `hint`, `q`, `page` default 1, `page_size` default 50 max 200), `GET /summary`, `GET /scenarios/history` (query `env`, `test_object`, `test_type`, `scenario_name`), `PUT /scenarios/flag` (body `FlakyFlagIn`), `POST /recompute` (body `FlakyRecomputeIn`; calls `recompute_scores(trigger="MANUAL", requested_by=body.user)`; 409 `"Flaky scores recompute is already running"` on `FlakyRecomputeInProgressError`), `GET /recompute/status` (`FlakyRecomputeStatusOut | None`).

- [ ] **Step 1: Write failing API tests** with the `client` fixture, patching the service functions (same style as `tests/test_sanp_health_api.py`): status codes and payload shapes for each endpoint; 409 on recompute in progress; 422 on unknown `hint`/`flag`; `PUT` without `user` → 422.
- [ ] **Step 2: Run** `pytest tests/test_flaky_api.py -v` → FAIL.
- [ ] **Step 3: Implement** the router (errors → 500 with detail, as `test_metrics.py` does).
- [ ] **Step 4: Run** → PASS; regenerate/verify OpenAPI if `tests/test_generate_openapi.py` requires a committed spec file.
- [ ] **Step 5: Commit** — `feat: expose flaky scenarios API`.

### Task 8: Legacy endpoints on the registry

**Files:**
- Modify: `app/schemas/test_metrics.py` (`FlakyScenarioIn` add `user: str | None = None`; `FlakyScenarioOut` add `test_type: str`, make `suite_id`, `test_object_version`, `test_version` optional)
- Modify: `app/services/test_metrics.py` (`get_flaky_scenarios`, `save_or_update_flaky_scenarios`)
- Test: `tests/test_flaky_legacy.py`

**Interfaces:**
- Consumes: `set_flag` from Task 6.
- Produces: unchanged paths and request body; `GET /test-metrics/flaky-scenarios?test_object=` returns registry rows with `is_flaky=true` (`test_object`, `test_type`, `scenario_name`, `is_flaky`, `inserted_at`; version fields null). Save resolves `test_type` from `suite_id` and calls `set_flag(..., user=item.user)`; no writes to `flaky_scenarios`.

Note: the current GET types `test_object` as `uuid.UUID` while the frontend sends the `test_object` name; switch the query param to `str` (matches the column).

- [ ] **Step 1: Write failing DB tests**
  - `test_save_writes_registry_not_legacy_table`.
  - `test_unflag_from_old_version_unflags_scenario` — flag via suite v1/test_version 1, unflag via suite v2/test_version 2 → one registry row `is_flaky=False` (Review Focus 5).
  - `test_get_returns_flagged_registry_rows_for_test_object`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** full `pytest`, `ruff check .`, type check → PASS.
- [ ] **Step 5: README gate + Commit.** Update the backend README (new tables, endpoints, beat job, settings) if it documents those; then propose `feat: route legacy flaky endpoints to the scenario registry`.

---

## Frontend (`pagopa-qa-centralhub-frontend`)

Before Task 9 read the relevant guide in `node_modules/next/dist/docs/` (repo `AGENTS.md` rule).

### Task 9: Types, hooks and version-independent matching

**Files:**
- Modify: `src/types/index.ts` (`FlakyScenario` add `test_type: string`, `user?: string`; version fields optional)
- Create: `src/types/flaky.ts` (mirrors Task 6 schemas: `FlakyHint`, `OutcomePoint`, `FlakyScenarioRow`, `FlakySummary`, `FlakyHistoryItem`, `FlakyRecomputeStatus`, `FlakyFilters`)
- Create: `src/hooks/useFlaky.ts`
- Modify: `src/utils/nrtUtils.ts`, `src/hooks/useNrts.ts`, `src/components/nrts/NrtsTable.tsx`
- Test: `src/utils/nrtUtils.test.ts` (create if missing), `src/hooks/useFlaky.test.tsx`, existing `NrtsTable.test.tsx`, `NrtDetailsPanel.test.tsx`

**Interfaces:**
- Produces hooks: `useFlakyScenarios(filters: FlakyFilters, page: number)`, `useFlakySummary(filters)`, `useFlakyHistory(key: {env, test_object, test_type, scenario_name} | null)`, `useSetFlakyFlag()`, `useRecomputeFlaky()`, `useFlakyRecomputeStatus()`; query keys rooted at `["flaky", ...]`. `FlakyFilters = { env: "DEV"|"UAT"|"PROD"; testObjects: string[]; flag: "all"|"flagged"|"unflagged"; minLevel: number | null; hint: FlakyHint | null; q: string }`.
- `isFlakyScenarioEqual(a, b)` compares `test_object`, `test_type`, `scenario_name` only; `createFlakyScenario(scenario, suite, testVersion)` sets `test_type` from `suite.test_type`.

- [ ] **Step 1: Write failing tests**
  - `isFlakyScenarioEqual` true across different `suite_id`/`test_version`, false across `test_type` (Review Focus 1, 5).
  - `useSaveFlakyScenarios` invalidates `["flaky-scenarios"]` and `["flaky"]`.
  - `NrtsTable` save sends `user` (session email) in each flaky item.
  - `useSetFlakyFlag` sends `PUT /api/v1/flaky/scenarios/flag` with `user`.
- [ ] **Step 2: Run** `pnpm test` → FAIL.
- [ ] **Step 3: Implement.** `useFlakyScenarios` serializes filters to query params (`test_object` repeated).
- [ ] **Step 4: Run** `pnpm test` → PASS.
- [ ] **Step 5: Commit** — `feat: match flaky scenarios independently of version`.

### Task 10: Route, sidebar and page shell

**Files:**
- Modify: `src/components/shell/Sidebar.tsx` (`NAV_GROUPS` "Test Results", after NRT: `{ href: "/flaky-scenarios", label: "Scenari flaky", icon: Shuffle, action: "view:flaky", featureKey: "flaky_scenarios" }`)
- Modify: `src/lib/features.ts` (`{ route: "/flaky-scenarios", key: "flaky_scenarios" }`)
- Create: `src/app/(portal)/flaky-scenarios/layout.tsx` (`<RouteGuard action="view:flaky">`)
- Create: `src/app/(portal)/flaky-scenarios/page.tsx`
- Create: `src/components/flaky/FlakyFilters.tsx`, `src/components/flaky/FlakyHintBoxes.tsx`
- Test: `Sidebar.test.tsx`, `src/lib/features.test.ts`, `src/components/flaky/FlakyHintBoxes.test.tsx`, `src/app/(portal)/flaky-scenarios/page.test.tsx`

**Interfaces:**
- Consumes: Task 9 hooks/types.
- Produces: `FlakyHintBoxes({ summary: FlakySummary; active: FlakyHint | null; onToggle(h: FlakyHint): void })`; `FlakyFilters({ value: FlakyFilters; onChange(v): void; suites: string[] })`. Page keeps filters in the URL search params (`env`, `test_object`, `flag`, `min_level`, `hint`, `q`, `page`), default `env=UAT`.

- [ ] **Step 1: Write failing tests**
  - Sidebar shows "Scenari flaky" for a guest matrix and hides it when the feature is disabled.
  - `featureKeyForPath("/flaky-scenarios") === "flaky_scenarios"`.
  - Hint boxes: four boxes with labels "Da marcare", "Da smarcare", "Rotti", "Marcati flaky" and counts; clicking an active box calls `onToggle` with the same hint (page clears it); "Marcati flaky" text "su N scenari analizzati" and not clickable.
  - Page header: "Ultimo ricalcolo" from status (or "mai eseguito"), filters reflected in the URL.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**, following the filter components of `src/components/sanp-health/SanpHealthFilters.tsx` and the suite list from `useSuites`.
- [ ] **Step 4: Run** `pnpm test` → PASS.
- [ ] **Step 5: Commit** — `feat: add flaky scenarios page shell and navigation`.

### Task 11: Flakiness table (variant A)

**Files:**
- Create: `src/components/flaky/FlakinessLevel.tsx`, `src/components/flaky/OutcomeStrip.tsx`, `src/components/flaky/FlakyTable.tsx`
- Modify: `src/app/globals.css` (tokens `--flaky-l1` … `--flaky-l5` for light and dark; l1 = `--success`, l3 = `--warning`, l5 = `--danger`, l2/l4 intermediate as in the mockup: `oklch(0.76 0.15 120)`, `oklch(0.70 0.17 55)`)
- Test: `FlakinessLevel.test.tsx`, `OutcomeStrip.test.tsx`, `FlakyTable.test.tsx`

**Interfaces:**
- Produces: `FlakinessLevel({ level, score, state })` — 5 segments, first `level` filled with `--flaky-l{level}`, number and grey score; `state="BROKEN"` → chip "Rotto"; `INSUFFICIENT_DATA` → chip "Dati insuff."; `aria-label="Flakiness livello N su 5, punteggio S"`. `OutcomeStrip({ outcomes: OutcomePoint[]; size?: number = 30 })` — green P, yellow R, red F, grey placeholders to `size`; `title` per cell with date and outcome. `FlakyTable({ rows; canManage: boolean; onToggleFlag(row, next: boolean); onRowClick(row) })` with columns Flakiness, Scenario (+ `test_object · test_type`), Ultime run, Flip, Pass on retry, Run, Ultimo KO (relative date via `src/lib/format.ts`), Flag, Marcato da, Hint; row class by hint for the 3px left bar (red/green/dark grey); legend below.

- [ ] **Step 1: Write failing tests**
  - Level 3 renders 3 filled segments of 5 and text "3"; BROKEN renders "Rotto".
  - Strip of 3 outcomes renders 30 cells, the last 3 colored in order.
  - Toggle disabled when `canManage` false; clicking calls `onToggleFlag(row, true)`.
  - Hint chips: "Da marcare", "Da smarcare", "Rotto: apri bug"; row highlight classes.
  - Toggling the flag is optimistic and rolls back on API error (page-level test with mocked `useSetFlakyFlag`).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**; wire into the page with `canManage = can("manage:flaky")` and `user = session.user.email`.
- [ ] **Step 4: Run** `pnpm test` → PASS.
- [ ] **Step 5: Commit** — `feat: add flaky scenarios table with flakiness indicator`.

### Task 12: Side panel, recompute button and "Come si calcola"

**Files:**
- Create: `src/components/flaky/FlakyScenarioPanel.tsx`, `src/components/flaky/FlakyRecomputeButton.tsx`, `src/components/flaky/FlakyHowItWorks.tsx`
- Test: matching `*.test.tsx`

**Interfaces:**
- Produces: `FlakyScenarioPanel({ row: FlakyScenarioRow | null; env; canManage; onClose; onToggleFlag })` — summary (level, rates, runs), history from `useFlakyHistory` (date, outcome, retries, test version, error message, Allure link, link to `/nrts/report/<run_id>`), flag toggle. `FlakyRecomputeButton({ user })` — hidden without `manage:flaky`; spinner while pending; on success invalidates `["flaky"]`; on 409 shows "Ricalcolo in corso". `FlakyHowItWorks()` — popover text: eligible triggers, window 30 giorni / 30 run, flip rate, pass on retry, half-life 14 giorni, level thresholds 5/15/30/50, rules of the three hints.

- [ ] **Step 1: Write failing tests** for each behavior above (409 message, invalidation, hidden for guest, history rows and links).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** using the existing drawer/panel pattern (e.g. `src/components/sanp-health/SanpHealthResultDetails.tsx` or `NrtDetailsPanel`).
- [ ] **Step 4: Run** `pnpm test` → PASS.
- [ ] **Step 5: Commit** — `feat: add flaky scenario details panel and manual recompute`.

### Task 13: Verification and README gate

- [ ] **Step 1:** Frontend `pnpm lint && pnpm test && pnpm build` → all pass. Backend `ruff check . && pytest` → all pass.
- [ ] **Step 2:** Run backend (`uvicorn app.main:app`) with migrated local DB, trigger `POST /api/v1/flaky/recompute`, open `/flaky-scenarios` in the dev server: check sidebar entry, hint boxes counts vs table, flag toggle reflected in an NRT report panel of a different version, guest role sees no toggles and no recompute button.
- [ ] **Step 3:** README gate in both repos (new section, permissions, tables, scheduled job); justify if no update is needed.
- [ ] **Step 4: Commit** (frontend README/docs if changed) and prepare both PR descriptions (context, summary, testing, follow-ups: drop `flaky_scenarios`, backend auth enforcement, Jira from "Rotto", score trend, notifications). The user opens the PRs; backend must be deployed first.
