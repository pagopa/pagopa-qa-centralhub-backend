# Release Status section (status page port)

Date: 2026-11-16
Repos: `pagopa-qa-centralhub-backend`, `pagopa-qa-centralhub-frontend`
Source being ported: `pagopa-status-page` (Flutter Web, GitHub Pages)
Status: draft, awaiting review

## 1. Intent

QA Hub is becoming the single entry point for QA and for the product teams. Together with the product
teams we want to centralize there the pagoPA status page, which today is a standalone Flutter app on
GitHub Pages.

The status page answers one question: for every pagoPA service, which version is released on GitHub and
which version runs in DEV, UAT and PROD. It has three limits:

- the service catalog is hard-coded in `lib/constant.dart`, so adding, editing or removing a service or a
  product needs a pull request and a redeploy;
- every visitor's browser makes ~216 calls per refresh (54 services × repo/DEV/UAT/PROD), and needs a
  GitHub login to avoid the anonymous rate limit;
- it shows only the current state: there is no deploy history and no link to the tests run on a version.

The new "Release Status" section in QA Hub reproduces the current view and adds:

1. management of the catalog (areas → products → applications) from the UI;
2. a deploy history per application and environment;
3. for every deployed version, the NRT / Quality Gate runs executed on that version in that environment;
4. a summary tile in the Overview page.

Once it is live, the GitHub Pages site is meant to be retired, so the catalog in QA Hub becomes the only
source of truth.

## 2. Goals, non-goals, anti-goals

Goals
- Section at `/releases` (the existing empty page) showing, per application, the latest GitHub release and
  the version deployed in DEV, UAT and PROD, grouped by area (Core / TouchPoint) and product, with search
  and filters, equivalent to the current site.
- Same icon vocabulary and colour semantics as the current site (§9.2), with a polished, high-impact look.
- Catalog management (create / edit / delete products and applications, move an application between
  products) for users with `manage:status_page`.
- Automatic refresh every 15 minutes (Celery beat) plus a manual "Sincronizza ora" for users with
  `sync:trigger`.
- Deploy history: one event each time the version observed in an environment changes.
- Test linkage: per application and environment, whether the deployed version has NRT / QG runs and their
  outcome.
- Overview tile with the counts (§8.4).
- Initial import of the 53 active services and 14 products from the current `constant.dart`.

Non-goals
- Per-product ownership (product teams editing only their own product). Management is centralized for
  now (option A); moving to per-team ownership is a possible later change, not designed here.
- Triggering deploys from the hub (the old `DeployButton` is deprecated and is not ported).
- Backfilling deploy history from GitHub Actions / Azure DevOps runs. History starts on go-live.
- Changing how the `/shared/statuspage/v1/info` endpoint or its product keys are configured (they live
  outside both repos).
- Any change to how NRT / QG runs are ingested.
- Decommissioning the GitHub Pages site (done by its owners after go-live).

Anti-goals (a failure even if it works)
- A version error (endpoint down, timeout) recorded as a deploy. Errors must never create history events.
- The hub showing "aligned" or "tested" when it does not actually know (unknown must look unknown).
- A manual refresh that blocks the HTTP request until all ~216 calls finish (APIM timeout).
- Two catalogs kept in parallel by hand after go-live.

## 3. Constraints

- Backend: FastAPI + async SQLAlchemy + Alembic + Celery (Redis broker), Python 3.12. Next migration is
  `0032`.
- Frontend: Next.js 16, React 19, TanStack Query, Tailwind, `lucide-react` icons, `Gate` component for
  permission gating (`src/lib/permissions.tsx`).
- Permissions are action keys in `app/core/permissions.py` (`ACTION_CATALOG`) with per-role defaults,
  editable from the role matrix. Today the backend does not enforce action permissions on endpoints: they
  are enforced by the frontend `Gate`, and write endpoints receive the acting `user` in the body for
  auditing (see flaky scenarios). This section follows the same pattern (see §11).
- Feature visibility: sections are listed in `FEATURE_CATALOG` (`app/core/features.py`).
- GitHub calls use the server-side `settings.github_token` (read-only), no per-user GitHub login.
- The version endpoint is `https://api.{dev.,uat.,}platform.pagopa.it/shared/statuspage/v1/info?product=<key>`
  and returns JSON with a `version` field. Backend and frontend services are queried the same way (the
  Flutter app has two identical code paths). It must be reachable from the backend's network for all three
  environments (§12, risk 1).
- An existing, unused `releases` table / `Release` model has a different meaning (planned releases). It is
  left untouched; new tables use the `sp_` prefix to avoid confusion.
- Repository language: English for code, comments, logs, docs. UI text is Italian, as in the rest of the hub.

## 4. Approaches considered (for the deploy history)

Everything else (catalog, permissions, test linkage, UI) is the same whichever is chosen.

1. **Periodic check that records only changes — chosen.** A Celery task every 15 minutes reads the version
   endpoint and GitHub, updates a "current state" table and appends a deploy event only when the version in
   an environment changes. Light, uniform for every application (GitHub Actions and Azure DevOps alike).
   Trade-off: deploy time is known within the polling interval, history starts on go-live.
2. Store every reading (~20k rows/day) and derive history downstream. Same information as 1, much heavier.
   Rejected.
3. Rebuild deploys from GitHub Actions `release_deploy` runs. Exact timestamps and backfill, but no Azure
   DevOps coverage, depends on how each team wrote its workflow, and a successful run does not prove what is
   actually running. Kept only as a possible future enrichment of 1.

On top of 1, the user asked that the refresh can also be launched manually.

## 5. Data model (backend, migration `0032_status_page_tables`)

All tables in the default schema used by the ORM `Base` of the other non-`qachub` features.

### 5.1 `sp_products`
| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `name` | varchar(100) unique, not null | e.g. "AFM", "Back Office" |
| `area` | varchar(20) not null, check in (`CORE`, `TOUCHPOINT`) | the two tabs |
| `created_at`, `updated_at` | timestamptz | |

The area is a field of the product, not an entity: there are two and they rarely change.

### 5.2 `sp_applications`
| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `product_id` | UUID FK → `sp_products.id` `ON DELETE RESTRICT` | |
| `name` | varchar(150) not null, unique per product (`UNIQUE(product_id, name)`) | e.g. "AFM-Calculator"; "PDF Engine" exists under two products today |
| `product_key` | varchar(100) unique, not null | key for `/statuspage/v1/info?product=` |
| `repository` | varchar(200) not null | repo name under the `pagopa` GitHub org |
| `pipeline_url` | varchar(500) null | link to the deploy pipeline |
| `runtime` | varchar(10) not null, check in (`AKS`, `AZ`) | chip, as today |
| `ci` | varchar(10) not null, check in (`GHA`, `AZDO`) | chip, as today |
| `kind` | varchar(10) not null, check in (`backend`, `frontend`), default `backend` | informative only |
| `test_object` | varchar(200) null, indexed, **not unique** | link to `qachub.test_suites.test_object`; several applications may share one (e.g. a service and its Helpdesk variant built from the same repository) |
| `ea_node_id` | varchar(200) null, FK → `ea_nodes.id` `ON DELETE SET NULL` | optional link to the EA Map component; navigation only for now |
| `created_at`, `updated_at` | timestamptz | |

The `host` attribute present in `constant.dart` is unused by the current site and is not ported. The Azure
OpEx dashboard link shown today is derived from `repository` (§9.3) and is not stored.

### 5.3 `sp_version_state` (current state)
Primary key `(application_id, target)`; `target` in (`REPO`, `DEV`, `UAT`, `PROD`).

| column | type | notes |
|---|---|---|
| `application_id` | UUID FK `ON DELETE CASCADE` | |
| `target` | varchar(4) | |
| `version` | varchar(100) null | last successfully read version (kept across errors) |
| `read_status` | varchar(20) not null | `OK`, `ERROR`, `EMPTY_BODY`, `NO_VERSION`, `NO_RELEASE` |
| `error_detail` | varchar(500) null | HTTP status / exception summary |
| `checked_at` | timestamptz not null | time of the last attempt |
| `last_ok_at` | timestamptz null | time of the last successful read |
| `version_since` | timestamptz null | detection time of the current `version` |

`version` keeps the last good value when a later read fails, so the UI can show "1.4.2 (non raggiungibile
dalle 10:15)" instead of losing information. Whether the cell is shown as an error is driven by
`read_status`.

### 5.4 `sp_deploy_events` (history)
| column | type | notes |
|---|---|---|
| `id` | UUID PK | |
| `application_id` | UUID FK `ON DELETE CASCADE`, indexed with `target, detected_at desc` | |
| `target` | varchar(4) | `REPO` events = new GitHub release published |
| `previous_version` | varchar(100) null | null for the baseline event |
| `version` | varchar(100) not null | |
| `detected_at` | timestamptz not null | time of the read that saw the new version |
| `previous_seen_at` | timestamptz null | last read that still saw `previous_version`; the deploy happened between this and `detected_at` |
| `is_baseline` | bool not null default false | first observation ever for this application/target |

Rules (sync, §6.3):
- only a read with `read_status = OK` can create an event;
- an event is created when the read version differs from `sp_version_state.version` (the last good one), so
  `1.4.1 → ERROR → 1.4.1` creates nothing and `1.4.1 → ERROR → 1.4.2` creates one event;
- the first OK read for an application/target creates a baseline event (`is_baseline = true`), shown as
  "prima rilevazione", never as a deploy;
- a downgrade is a normal event (the UI marks it as rollback when the semver compare says it is older).

### 5.5 `sp_sync_status` (singleton row, same pattern as SANP Health)
`id` (fixed 1), `status` (`QUEUED`, `RUNNING`, `OK`, `PARTIAL`, `FAILED`), `trigger` (`SCHEDULED`,
`MANUAL`), `requested_by` (null for scheduled), `queued_at`, `started_at`, `finished_at`, `last_success_at`,
`applications_checked`, `reads_failed`, `errors` (JSONB, summarized list, max ~20 entries), `celery_task_id`.

`PARTIAL` = sync completed but some reads failed (normal: a few endpoints are often down). `FAILED` = the
sync itself failed (e.g. DB error, GitHub token missing).

### 5.6 Initial data
The migration (or a data step run by it) inserts the 14 products and 53 applications from
`pagopa-status-page/lib/constant.dart` at the time of writing: products from the keys of `projectsCore`
(area `CORE`) and `projectsTouchPoint` (area `TOUCHPOINT`); applications with `name`, `product`→`product_key`,
`repository`, `pipeline`→`pipeline_url`, `env`→`runtime`, `actions`→`ci`, `type`→`kind`. `test_object` is
left null (filled from the UI). "GPS - Donations" is commented out in `constant.dart` (its endpoint response is
not compliant yet) and is not imported (builder's call, can be added from the UI). The data lives as a literal list in the migration, so the migration does not
depend on the other repository. `downgrade()` drops the tables.

## 6. Sync (backend)

### 6.1 Configuration (`app/config.py`)
- `status_page_dev_base_url` default `https://api.dev.platform.pagopa.it`
- `status_page_uat_base_url` default `https://api.uat.platform.pagopa.it`
- `status_page_prod_base_url` default `https://api.platform.pagopa.it`
- `status_page_info_path` default `/shared/statuspage/v1/info`
- `status_page_sync_concurrency` default 10, `status_page_http_timeout_s` default 10
- reuses `github_token`.

### 6.2 Triggers
- Celery beat entry `sync-status-page` every 900 s, task `app.tasks.sync_status_page.sync_status_page`
  (registered in `app/tasks/schedule.py` and `app/tasks/__init__.py` like the other tasks), argument
  `trigger="SCHEDULED"`.
- Manual: `POST /api/v1/releases/sync` with body `{ "user": "<email>" }`:
  - if `sp_sync_status.status` is `QUEUED` or `RUNNING` and it is not stale → `409` "Sincronizzazione già in
    corso";
  - otherwise set `status=QUEUED`, `trigger=MANUAL`, `requested_by`, `queued_at`, enqueue with
    `apply_async` (pattern of `app/api/v1/ea.py`), store `celery_task_id`, return `202` with the status row.
  - "stale" = `QUEUED` for more than 10 minutes or `RUNNING` for more than 30 minutes (a worker died); a
    stale row does not block a new request.
- The task itself takes a Postgres advisory lock (`pg_try_advisory_lock` on a dedicated lock id, as in
  `sanp_health`); if it cannot take it, it exits without doing anything (covers a scheduled and a manual run
  overlapping).

### 6.3 Steps of one sync
1. Set `RUNNING`, `started_at`.
2. Load all applications.
3. With an `httpx.AsyncClient` and a semaphore of `status_page_sync_concurrency`:
   - `REPO`: `GET https://api.github.com/repos/pagopa/{repository}/releases/latest` with the token. `200` →
     `tag_name`; `404` → `NO_RELEASE`; other → `ERROR`. Rate-limit responses (`403/429` with
     `x-ratelimit-remaining: 0`) are `ERROR` with detail "GitHub rate limit".
   - `DEV`/`UAT`/`PROD`: `GET {base}{info_path}?product={product_key}`. `200` with body → `version` field, or
     `NO_VERSION` if missing; `200` empty → `EMPTY_BODY`; other / timeout → `ERROR`.
   - One retry on timeout / connection error; no retry on HTTP status errors.
4. Normalize versions for storage: trim; strip a leading `Release ` (seen in the current site); keep the
   leading `v` as read (normalization for comparison is in §7).
5. In one transaction: upsert `sp_version_state` and insert `sp_deploy_events` per §5.4.
6. Set the final status (`OK` / `PARTIAL` / `FAILED`), counts, errors, `finished_at`, and `last_success_at`
   when not `FAILED`. On exception, record `FAILED` in a separate transaction (pattern of
   `sanp_health._record_failed_sync_status`).

Applications created or edited between syncs are read at the next sync. Creating an application does not
trigger a sync by itself; the UI suggests "Sincronizza ora" after saving.

## 7. Derived information (computed on read)

### 7.1 Version comparison
A small `semver` helper (`app/services/status_page_versions.py`):
- strip a leading `v`/`V`; parse `MAJOR.MINOR.PATCH[-PRERELEASE][+BUILD]`;
- compare major, minor, patch numerically, then "no pre-release > pre-release", then pre-release identifiers
  per SemVer 2.0;
- anything that does not parse is "non-standard": no drift is computed for it (shown as today with the
  info icon).

This replaces the Flutter `compareTo`, whose major comparison has the opposite sign of minor/patch.

### 7.2 Drift per environment cell (vs `REPO`)
- `EQUAL`: same version as the latest release;
- `BEHIND`: older than the latest release (normal for UAT/PROD between releases);
- `AHEAD`: newer than the latest release (unusual: e.g. a deploy from a branch);
- `UNKNOWN`: repo or env not readable, or a non-standard version.

### 7.3 Application status (used by filters and the Overview tile)
- `ALIGNED`: PROD `EQUAL` to the latest release and all targets readable;
- `DRIFT`: PROD is `BEHIND` or `AHEAD` of the latest release. Same rule as the current site
  (`_hasVersionDrift` compares only PROD with the repo). DEV and UAT still show their own drift arrow per
  cell, but do not make the application `DRIFT` (a DEV pre-release such as `1.4.3-1-feature` being "ahead"
  is normal);
- `UNREACHABLE`: at least one target with `read_status ≠ OK` (takes precedence for the filter
  "non raggiungibili", but an app can be both drift and unreachable; filters are independent);
- `UNTESTED`: at least one of UAT/PROD whose deployed version has no linked run of any kind (only for
  applications with a `test_object`).

### 7.4 Test linkage
For an application with `test_object = T`, environment `E` and deployed version `V`:

```
qachub.test_suites s JOIN qachub.test_runs r ON r.suite_id = s.id
WHERE s.test_object = T
  AND normalize(s.test_object_version) = normalize(V)   -- normalize = strip leading v, trim
  AND r.env = E
```

All runs count, whatever their origin. The run kind shown and the hub link are chosen by `test_origin`, as
the rest of the hub does: `QG` → `/quality-gate/report/{id}`, `TAS` → `/tas-nrts/report/{id}`, anything else
→ `/nrts/report/{id}`.

Runs are grouped by kind, derived from `test_origin` as in the rest of the hub: `QG`, `TAS`, everything else
= `NRT`. For each (application, env, version) and each kind with at least one run, the cell shows the outcome
of the **latest** run of that kind by `timestamp_start`:
- reviewed run (`revisioned = true`, `revision_result` `OK`/`KO`): the review decides;
- not reviewed (`revision_result = PENDING`, the server default): the counts decide — `PASSED` when
  `failed_scenario + broken_scenario = 0`, else `FAILED` — and NRT/QG runs carry a small "da revisionare"
  hint. TAS runs are read-only in the portal and are never reviewed, so they never carry the hint;
- a run without counts → neutral.

No run of any kind → "non testata" (counts toward `UNTESTED`). Applications without `test_object` show no test marker at all (not "non testata").

## 8. API (backend, router `app/api/v1/releases.py`, prefix `/api/v1/releases`)

Schemas in `app/schemas/status_page.py`, service in `app/services/status_page.py`. Names below are
indicative; the builder may adjust them following the existing conventions.

### 8.1 Read
- `GET /status` → `{ sync: SyncStatusOut | null, areas: [ { area, products: [ { id, name, applications:
  [ ApplicationRowOut ] } ] } ] }`. `ApplicationRowOut`: catalog fields, `repo` cell, `envs: { DEV, UAT,
  PROD }` cells. A cell: `version`, `read_status`, `error_detail`, `checked_at`, `last_ok_at`,
  `version_since`, `drift` (env only), `test` (env only: `{ outcome, run_id, run_url, trigger_type,
  timestamp_start } | null` replaced by `tests: [ { kind: NRT|QG|TAS, outcome, needs_review, run_id, run_url,
  timestamp_start } ]`, one entry per kind present). Plus `status_flags` (§7.3). One query set, no N+1 (all 54×4 cells and test
  linkage in a few queries).
- `GET /summary` → `{ total, aligned, drift, unreachable, untested, last_success_at }` for the Overview tile.
- `GET /applications/{id}` → catalog fields, current cells, and `history: { DEV|UAT|PROD|REPO: [ { version,
  previous_version, detected_at, previous_seen_at, is_baseline, direction: upgrade|rollback|unknown, runs: [
  RunRefOut ] } ] }`. `runs` (env targets only) lists all runs linked to that version in that env, newest
  first, capped at 20 per version. `RunRefOut`: `run_id`, `trigger_type`, `timestamp_start`, outcome,
  passed/failed counts, `allure_url`, `gha_run_url`, link target in the hub (NRT or QG report page).
- `GET /sync-status` → `SyncStatusOut | null`.

### 8.2 Sync
- `POST /sync` (body `{ user }`) → `202 SyncStatusOut` / `409` (§6.2).

### 8.3 Catalog (gated by `manage:status_page` in the UI; body carries `user` for audit)
- `GET /catalog/products`, `POST /catalog/products`, `PATCH /catalog/products/{id}`,
  `DELETE /catalog/products/{id}` (`409` "Il prodotto contiene ancora applicazioni" if not empty).
- `GET /catalog/applications`, `POST /catalog/applications`, `PATCH /catalog/applications/{id}` (includes
  moving to another `product_id`), `DELETE /catalog/applications/{id}` (cascades state and history; the UI
  asks for confirmation stating that the history is deleted too).
- `POST /catalog/applications/check` body `{ product_key, repository }` → `{ repo: { ok, detail },
  envs: { DEV|UAT|PROD: { ok, version, detail } } }`: a live probe used by the form before saving. It never
  blocks saving; it only produces warnings.
- Validation: unique `name` within the product and unique `product_key` → `409` with a field-specific message;
  `repository` must match `^[A-Za-z0-9._-]+$`; `pipeline_url` must be `https://`.
- Every write inserts an `AuditLog` row (`action` e.g. `status_page.application.update`, `resource_type`
  `sp_application` / `sp_product`, `detail` = JSON with `user` and before/after of changed fields).

### 8.4 Overview tile
`GET /summary` is consumed by the Overview page. Counts: total applications, aligned, with drift,
unreachable, untested (UAT/PROD). Each count links to `/releases` with the corresponding filter preselected
(query string).

### 8.5 OpenAPI
The operations are added to `openapi/qa-hub-be.json` (only imported operations are reachable behind APIM),
as done for the other features.

## 9. Frontend

### 9.1 Structure
- Page `src/app/(portal)/releases/page.tsx` (replaces the stub), title "Release Status".
- Catalog management page `src/app/(portal)/releases/catalog/page.tsx`, reachable from a "Gestisci catalogo"
  button wrapped in `<Gate action="manage:status_page">`.
- Components in `src/components/release-status/`, hooks in `src/hooks/useReleaseStatus.ts`, pure helpers
  (filters, labels) in `src/lib/release-status.ts` with unit tests, following the SANP Health split.
- Feature entry in `FEATURE_CATALOG`: key `release_status`, label "Release Status", route `/releases`,
  action `view:status_page`.
- Overview: a new tile component in `src/components/overview/`.

### 9.2 Icons (same vocabulary as the current site)
The current site uses Material Icons plus two Font Awesome icons. The hub uses `lucide-react`; each icon is
mapped to its closest `lucide-react` equivalent, keeping the same meaning and colour role, so no new icon
dependency is added:

| current site | meaning | lucide-react | colour |
|---|---|---|---|
| `check_circle_outline` | version read | `CircleCheck` | primary/accent |
| `FontAwesome equals` | equal to repo | `Equal` | neutral |
| `arrow_downward` | behind repo | `ArrowDown` | warning (tertiary today) |
| `arrow_upward` | ahead of repo | `ArrowUp` | danger |
| `cloud_off_outlined` | endpoint error | `CloudOff` | danger |
| `warning_amber` | empty body / no version | `TriangleAlert` | warning |
| `info_outline` | non-standard version | `Info` | warning |
| `collections_bookmark` | GitHub repository link | `LibraryBig` | tertiary |
| `rocket_launch` | pipeline link | `Rocket` | primary |
| `show_chart` | Azure OpEx dashboard link | `ChartLine` | primary |
| `widgets_outlined` | "N servizi" counter | `LayoutGrid` | neutral |
| `sync_problem_outlined` | "N con versioni diverse" counter | `RefreshCwOff` | warning |
| `account_balance_wallet_rounded` | Core tab | `WalletMinimal` | – |
| `account_balance` | TouchPoint tab | `Landmark` | – |
| `search` / `clear` / `search_off` | search, clear, no results | `Search` / `X` / `SearchX` | – |
| `refresh` / `update` | manual refresh | `RefreshCw` | – |
| `schedule` | last update time | `Clock` | – |
| `open_in_new` | external link | `ExternalLink` | – |
| `FontAwesome github` | GitHub | inline SVG GitHub mark component (lucide 1.x has no brand icons) | – |

New markers (not in the current site): deploy history `History`, rollback `Undo2`, and one small test icon
per run kind present on that version, coloured by outcome (green / red / neutral):
`FlaskConical` = NRT, `ShieldCheck` = Quality Gate, `Bot` = TAS. A tiny dot on the NRT/QG icon means "da
revisionare". Kinds without runs show no icon; no run at all shows a single muted `FlaskConical` with the
tooltip "non testata". At most three 14 px icons per cell, so the row stays light; the tooltip lists kind,
date and outcome of each.

The builder verifies each name exists in the installed `lucide-react` version and picks the nearest one if a
name differs.

### 9.3 Main view (`/releases`)
Header: title, "Ultimo aggiornamento HH:MM · automatico/manuale (by user)", a `StaleDataWarning`-style
banner when the last sync is `FAILED` or older than 45 minutes, a "N letture non riuscite" note when
`PARTIAL`, the sync button (`Gate sync:trigger`) and "Gestisci catalogo" (`Gate manage:status_page`).

KPI strip (existing `Kpi` primitive): servizi, allineati, con versioni diverse, non raggiungibili, non
testati.

Tabs Core / TouchPoint; search box (application name, product, repository, product key); filter chips: tutti
/ con versioni diverse / non raggiungibili / non testati. Filter and tab are reflected in the query string
(so the Overview tile can deep-link).

Products are collapsible groups (expanded by default, sorted by name, with count and a small drift/error
badge on the header). Rows sorted by name:

```
AFM-Calculator   [AKS][GHA]   📚 🚀 📈     Repo 1.4.2   DEV ✓ 1.4.2 =  🧪●   UAT ✓ 1.4.2 =  🧪●   PROD ✓ 1.4.1 ↓  🧪●
```

- name, runtime chip, CI chip (same colour roles as today: AKS/GHA primary-ish, AZ/AZDO secondary);
- link icons: repository (`https://github.com/pagopa/{repository}`), pipeline, Azure OpEx dashboard
  (`https://portal.azure.com/#@pagopait.onmicrosoft.com/dashboard/arm/subscriptions/b9fc9419-6097-45fe-9f74-ba0641c91912/resourceGroups/dashboards/providers/Microsoft.Portal/dashboards/pagopa-p-opex_{repository}`,
  as today);
- one cell per target with the icon of §9.2, version, drift arrow, and test marker; tooltip with full
  detail (read status, error, since when, last check);
- a cell whose last read failed but has a last good version shows the version greyed with `CloudOff`.

Click on a row opens the detail (§9.4).

Look & feel: the page should feel like a polished dashboard, not a plain table: clear status colours using
the hub's existing tokens (`success`, `warning`, `danger`, `accent`, `*-soft`), generous spacing, sticky
header row per product group, subtle hover, skeleton loading for cells, smooth expand/collapse, responsive
(on narrow screens the four targets collapse into a compact two-line layout, as the current site does
below 450 px). Light and dark themes via existing tokens.

### 9.4 Application detail
A side sheet (or a page, builder's call following existing patterns) with:
- header: name, product, chips, links (including a link to the EA Map naming the component when `ea_node_id`
  is set; the EA Map has no deep-link parameter today, so it opens the map), current versions per target;
- tabs DEV / UAT / PROD / Release GitHub: a vertical timeline of `sp_deploy_events`, newest first. Each
  entry: version, upgrade/rollback marker, "rilevato il 16/11 10:15 (tra 10:00 e 10:15)", "prima
  rilevazione" for baseline events; under env entries, the linked runs (trigger type, date, outcome,
  passed/failed, links to the hub report, Allure, GitHub Actions);
- if no `test_object` is set: a hint "Collega un test object per vedere le run" (with a link to edit, gated).

### 9.5 Catalog management (`/releases/catalog`)
- Left: products grouped by area, with application counts; create / rename / change area / delete (delete
  disabled with an explanation when not empty).
- Right: applications of the selected product (or all, with search); create / edit / delete / move.
- Application form fields per §5.2. `ea_node_id` is an optional searchable picker over EA Map components
  (suggesting the one whose repository matches). `test_object` is a combobox populated from the distinct
  `test_suites.test_object` values (endpoint `GET /catalog/test-objects`), free text allowed.
- A "Verifica" button (also run automatically on save) calls `/catalog/applications/check` and shows per
  target ✓ version / ✗ detail. Warnings do not block saving.
- After saving, a toast suggests "Sincronizza ora" (if the user has `sync:trigger`).

## 10. Permissions

New entries in `ACTION_CATALOG`, category "Release Status":
- `view:status_page` — "Release Status (visualizza)": `qa_manager`, `qa_analyst`, `qa_engineer`, `guest` all
  true (the current site is public).
- `manage:status_page` — "Release Status (gestione catalogo)": only `qa_manager` true.

Manual sync reuses `sync:trigger`. Existing roles stored in the DB receive the new keys the same way
previous features added theirs (via the role matrix defaults / migration, following what `view:flaky` did).

## 11. Testing

Backend (pytest, existing fixtures):
- semver helper: ordering cases incl. major/minor/patch, pre-release, `v` prefix, non-standard strings
  (regression for the Flutter major-sign bug);
- sync event rules: first read → baseline; same version → no event; error then same → no event; error then
  new → one event with `previous_seen_at` = last good read; downgrade; REPO events; external HTTP mocked with
  `respx` or the mocking approach already used in the repo;
- read statuses for each HTTP outcome (200 / empty / no `version` / 404 / 5xx / timeout / rate limit);
- manual sync: 202 + enqueue, 409 while running, stale row does not block; advisory lock exit;
- test linkage: version normalization, env match, grouping by kind from `test_origin`, latest run per kind
  wins, revision overrides counts, `needs_review` never set for TAS, shared `test_object` across two
  applications, no `test_object` → no marker;
- catalog CRUD: uniqueness conflicts, product delete with apps → 409, move between products, cascade delete,
  audit rows written;
- `/status` query count bounded (no N+1);
- migration imports 14 products / 53 applications.

Frontend (vitest + Testing Library):
- helpers: filters, status flags, labels and icon choice per cell state;
- page: renders groups and cells, filters/search/tabs and query-string sync, gated buttons hidden without
  permissions, sync button states (idle, queued/running with polling, 409 message);
- catalog page: form validation, check results shown as warnings, delete confirmations;
- Overview tile: counts and deep links.

Lint/type-check: `ruff`, `mypy` (backend); `eslint` (frontend).

## 12. Risks and open points

1. **Network reachability of the version endpoint from the backend**, for DEV, UAT and PROD. The Flutter app
   calls it from the browser. If the backend (AKS) cannot reach one environment's APIM, those cells will all
   be `ERROR`. To verify before implementation starts; base URLs are configurable.
2. **Permissions are enforced only in the UI** (existing pattern of the hub). Anyone with a valid token could
   call the catalog write endpoints directly. Accepted for consistency now; a backend action-permission
   dependency would be a separate, hub-wide change.
3. **Catalog drift with the old site** during the transition. Mitigation: the GitHub Pages site is retired
   after go-live; until then, changes are made only in the hub.
4. **`product_key` unknown to the endpoint**: the check in the form surfaces it; the hub cannot register keys
   on the endpoint.
5. **Version formats**: `test_object_version` values and deployed versions must match after normalization
   for the test linkage to work. If teams use different formats, some versions will look "non testata". The
   normalization is in one helper, easy to extend.

## 13. Left to the builder

- Exact endpoint / schema / component names, following the existing conventions.
- Sheet vs page for the application detail.
- Exact visual styling within the hub's tokens and primitives, aiming at the "polished dashboard" goal of
  §9.3.
- Choosing the nearest `lucide-react` icon when a name in §9.2 does not exist in the installed version.
