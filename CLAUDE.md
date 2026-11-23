# QA Hub — Backend

See `README.md` for setup, commands and module overviews. This file holds only durable rules.

## Status Page (Release Status)

Code: `app/services/status_page*.py`, `app/tasks/{sync,enrich}_status_page.py`,
`app/models/status_page.py`, `sp_*` tables. Details in `README.md` ("Release Status") and
`docs/status-page/`.

- **Reliable data or nothing.** Never show a guessed value: a version that is not a resolvable
  tag gives `UNAVAILABLE`; a technology version comes only from the runtime image or a build
  file, never from GitHub language statistics. A derived image's tag is not a language version.
- **QA process.** UAT is the certification environment; tests never run on PROD and only
  occasionally on DEV. Runs are linked by deploy window (`STATUS_PAGE_RUNS_MATCH_VERSION=false`),
  not by `test_object_version`.
- **Flags.** `UNDER_QA` = the application has a `test_object` (the "Sotto test QA" filter).
  `UNTESTED` is legacy, kept only for API compatibility; remove it once no client reads it.
- **No history backfill** from GitHub: history starts at the first sync (16/11/2026) by design.
- **Hidden / manual entries** stay in the catalog, are still synced (REPO for manual ones) and
  are still served by `/applications/{id}` and `/versions`.
- **Enrichment** (change summaries, technology) runs as its own Celery task after each sync and
  must never fail or starve the sync: own advisory lock (sync 671, enrich 672), GitHub budget
  reserve (`STATUS_PAGE_GITHUB_RESERVE`), stop on any rate limit, 3 retries per item, per-item
  DB isolation, circuit breaker on repeated outages.
- **GitHub calls** build paths only through `repo_path` / `contents_path` / `compare_path` in
  `status_page_github.py` (validated, percent-encoded); the token goes only to `api.github.com`.
- **Caches** `sp_change_summaries` and `sp_tech_stacks` have no FK and can be cleared: to fix
  wrongly cached values after a parser fix, delete the rows in a data migration (see `0035`).
- **API changes are additive** so frontend and backend can deploy in any order; when they are
  not, say which deploys first. After schema changes regenerate the OpenAPI with
  `uv run --extra dev python -m scripts.generate_openapi --apim-compatible` (APIM re-import).
- **Tests** for the service layer hit the local Postgres (`tests/_db.py`); `get_status` has a
  query-count bound in `test_status_page_service.py` — update it deliberately.

## Flaky Scenarios

Code: `app/services/flaky{,_scoring}.py`, `app/models/flaky.py`, `app/api/v1/flaky.py`,
`app/tasks/recompute_flaky_scores.py`, `flaky_*` tables. Details in `README.md` ("Scenari flaky")
and `docs/superpowers/specs/2026-11-14-flaky-scenarios-management-design.md`.

- **Scenario key** is `test_object` + `test_type` + `scenario_name`, never `suite_id` or a
  version: a flag carries over to every new version until a reviewer removes it.
- **Registry vs scores.** `flaky_registry` holds reviewers' decisions and is never touched by
  the recompute; `flaky_scores` (one row per scenario and env) is derived and fully rewritten at
  each recompute. They are joined on the scenario key with no FK on purpose (different
  cardinality and lifecycle); the only FK is `last_suite_id` → `test_suites` (`SET NULL`).
- **Legacy `flaky_scenarios`** is no longer written: the old `test_metrics` flag endpoints are
  kept for compatibility and write the registry through `apply_flag`, which is a no-op when the
  flag is unchanged (the run panels re-post every flag; author and audit must not change).
- **Eligible runs**: triggers `CRON`, `CI_PIPELINE`, `TAS`; `MANUAL` and origin `QG` excluded.
  Quality Gate is identified by `test_origin = 'QG'` (ingestion sends only `NRT`/`TAS`/`QG`;
  `QA_GATE` is never sent as trigger), here and in the pending-runs counter.
- **Score** measures instability, not failures: `max(flip_rate, retry_pass_rate)` with recency
  weighting; a scenario failing every recent run is `BROKEN` (a bug), even with score 0. All
  parameters are `FLAKY_*` settings; if they change, update the "Come funziona" copy in the
  frontend and the Grafana panel descriptions.
- **Recompute** runs daily at 12:00 Europe/Rome (beat `nowfun`, Celery timezone unchanged) and on
  demand, under advisory lock 670 (status page uses 671/672). A new task module must be imported
  in `app/tasks/__init__.py` or the worker never registers it.
- **Permissions** `view:flaky` (all roles) and `manage:flaky` (all but guest) are enforced only in
  the frontend; the backend takes the acting user's email from the request body.
- **ADX mirror.** The `flaky_*` tables are fully replaced at every ADX sync
  (`pagopa-platform-integration-test`, `.github/scripts/qa_hub_adx_sync.py`). A column change
  needs the sync `TableSpec`, the ADX schema in `pagopa-infra`
  (`create_adx_nrt_tables.dexp`) and the Grafana "QA - NRT metrics" queries updated together.

## Messaging Hub

Code: `app/messaging/`, `app/api/v1/messaging.py`, `app/tasks/messaging.py`, `msg_*` tables. Details in
`README.md` ("QA Messaging Hub"), `docs/messaging/event-catalog.md` and
`docs/superpowers/specs/2026-11-22-qa-messaging-hub-design.md`.

- **Only `app/messaging` reads or writes `msg_*` tables.** Everyone else uses `publish()`,
  `clear_condition()` and the inbox service, so the Postgres transport stays replaceable by Service Bus.
- **Every event type declares `occurrence` or `condition`** in the catalog (startup validation). Scanners call
  `clear_condition` when their condition is empty; otherwise the next occurrence would stay silent.
- **Handlers are idempotent and keyed by `event.id`**; delivery is at-least-once, with no ordering guarantee.
  Handlers never read delivery state and never commit (the dispatcher commits after success).
- **Routing is evaluated once, at acceptance**, in `rules.py` (not in subscription filters); a rule naming an
  unknown consumer, role or event type fails at startup.
- **The inbox stores a rendered snapshot** (title, body, link); a template change never rewrites history. A new
  message for a condition marks the user's older unread messages with the same key as read (`auto_read`).
- **`view:inbox` gates the bell and the page; the rules gate the audience** of each message.
- **Never fail a producer's job because of the hub:** `emit_*` helpers swallow and log errors; scanners run
  isolated from each other.
- A new Celery task module must be imported in `app/tasks/__init__.py` or the worker never registers it.
- The status page sync failure is intentionally not published in phase 1 (inbox flooding).
