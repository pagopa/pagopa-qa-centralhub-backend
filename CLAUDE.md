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
