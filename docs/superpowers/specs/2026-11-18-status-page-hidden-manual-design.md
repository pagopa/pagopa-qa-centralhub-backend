# Status Page: hidden entries and manual versions

## Intent

The status page database (`sp_*` tables) is becoming the single source of truth for the
versions of the `test_object`s we test: test pipelines read the deployed version through
`GET /api/v1/releases/versions?product_key=…&env=…` and store it with the run.

Some applications under test (e.g. checkout) have no standard `/shared/statuspage/v1/info`
endpoint, so they are not on the status page and their tested version cannot be traced.
This change lets the catalog hold them anyway:

- a **product** or an **application** can be marked **hidden**: it stays in the catalog and in
  the version lookup, but is not shown on the status page;
- an application can be marked **manual**: its DEV, UAT and PROD versions are entered by hand
  in the hub, while the REPO version is still read from GitHub by the sync.

## Goals

- Census applications without an `/info` endpoint and serve their versions through the
  existing version lookup, history and run linkage, exactly like the other applications.
- Hide products and applications from the status page (grid, KPIs, Overview) independently
  of their version source.
- Full backward compatibility: existing rows, API clients and the current frontend keep
  working unchanged.

## Non-goals

- Updating manual versions from a CI pipeline (`PUT /releases/versions` or similar). It is a
  planned follow-up: the design keeps the write path in one service function so an API can
  reuse it later.
- Manual REPO versions: the repository version is always read from GitHub.
- Per-team ownership or new permissions.

## Anti-goals

- Two code paths for "read" and "manual" versions in the lookup, history, drift or run
  linkage: whoever reads a version must not care where it came from.
- A manual version silently overwritten by the sync.

## Approaches

1. **Manual versions in `sp_version_state` (chosen).** A new `version_source` column on the
   application (`AUTO` | `MANUAL`); manual versions are stored as version states with
   `read_status = 'MANUAL'`, and a change creates a deploy event like the sync does. Lookup,
   history, drift and run linkage work unchanged. Cost: a new value in the `read_status` check
   constraint.
2. **Separate `sp_manual_versions` table.** Keeps manual data isolated, but the lookup, the
   history, the cells and the flags would have to merge two sources: more code and a risk of
   diverging behaviour between automatic and manual applications.

Chosen: 1, because readers of a version must not care where it came from.

## Design

### Data (migration `0033`)

- `sp_products.hidden BOOLEAN NOT NULL DEFAULT false`
- `sp_applications.hidden BOOLEAN NOT NULL DEFAULT false`
- `sp_applications.version_source VARCHAR(10) NOT NULL DEFAULT 'AUTO'`, check
  `version_source IN ('AUTO','MANUAL')`
- `ck_sp_version_state_read_status` extended with `'MANUAL'`.
- Existing rows get the defaults: nothing changes for them. The downgrade drops the columns
  and restores the constraint after turning `MANUAL` states into `NO_VERSION` (data loss is
  acceptable on downgrade).

An application is **effectively hidden** when `application.hidden OR product.hidden`.

### Manual versions

- Stored in `sp_version_state` for `DEV`, `UAT`, `PROD` with `read_status = 'MANUAL'`,
  `error_detail = NULL`, `checked_at = now`, `last_ok_at = now` when a version is set.
- Setting a version that differs from the current one creates an `SpDeployEvent`
  (`previous_version` = old version, `previous_seen_at` = old `last_ok_at`,
  `is_baseline = previous is None`), and `version_since = now`. Same rules as
  `apply_reading`, so the history and the deploy windows of the run linkage just work.
- Setting the same version again only refreshes `checked_at` / `last_ok_at` (no event).
- Clearing a version (empty value) sets `version = NULL`, `version_since = NULL`; no event.
  The lookup then answers 404 "Versione non ancora rilevata", as for an application never read.
- Values are trimmed, max 100 chars (column size); the `Release ` prefix is not stripped.
- Every change is written to the audit log (`status_page.application.versions`, with user,
  before and after per environment).

- A manual application always has the three DEV/UAT/PROD rows (*builder's call, confirmed in
  review*): they are created with `version = NULL`, `read_status = 'MANUAL'`,
  `checked_at = now` when the application becomes manual (on create or switch), so the grid
  shows an empty manual cell ("—" with the manual icon) instead of a missing cell. Clearing a
  value updates the row, never deletes it.
- Values are compared after `normalize_version` for drift and for the run linkage, as for read
  versions; no extra normalization is needed (with `STATUS_PAGE_RUNS_MATCH_VERSION=false` the
  linkage uses the deploy windows anyway).

### Switching the version source

- `AUTO → MANUAL`: the current DEV/UAT/PROD states are kept as the initial manual values:
  `read_status` becomes `MANUAL`, `error_detail` is cleared, `version` (the last good one, even
  if the last read failed), `checked_at`, `last_ok_at` and `version_since` are left as they are,
  so the history and the deploy windows do not move. Missing rows are created empty. No deploy
  event.
- One request that switches to `MANUAL` and sends `manual_versions` applies the switch first,
  then the sent keys (a deploy event only for values that differ). The audit log gets the usual
  `status_page.application.update` entry for field changes and a separate
  `status_page.application.versions` entry only when a version changed.
- `MANUAL → AUTO`: states are left as they are; the next sync reads the endpoints and applies
  the usual rules (a different version creates an event).

### Sync

- Applications with `version_source = 'MANUAL'` are read for `REPO` only; their DEV/UAT/PROD
  states are never touched. `applications_checked` still counts them.
- Hidden applications are synced like the others (the lookup must stay fresh).
- If the source changes to `MANUAL` while a sync is reading, the readings of that application
  for DEV/UAT/PROD are dropped at write time (re-check `version_source` on the rows locked
  with `FOR UPDATE`, as already done for deleted applications).

### Read model and flags

- `get_status`, `get_summary` (and therefore the Overview KPI) skip effectively hidden
  applications; a product with no visible application is not listed.
- `get_application_detail` and `get_version` ignore `hidden`: the detail of a hidden
  application can still be opened from the catalog, and the lookup serves it.
- A `MANUAL` cell is readable: drift is computed against the repo as for `OK`, it never counts
  as unreachable, and `ALIGNED` treats it like `OK`.
- `ApplicationRowOut` gets `version_source` and `hidden` (effective: application or product).
  `ApplicationDetailOut.application` is an `ApplicationRowOut`, so the detail sheet gets both
  and can show the "manuale" and "nascosta" chips; in the grid `hidden` is always `false`.
- No other code reads the `sp_*` tables (checked: only `app/services/status_page*`), so no
  other view needs the hidden filter.

### API (all additive)

- `ProductIn` / `ProductPatch` / `ProductOut`: `hidden: bool` (in: optional, default `false`).
- `ApplicationIn` / `ApplicationPatch` / `ApplicationOut`: `hidden: bool`,
  `version_source: "AUTO" | "MANUAL"` (in: optional, defaults `false` / `AUTO`).
- `ApplicationIn` and `ApplicationPatch` also accept
  `manual_versions: {DEV?: str|null, UAT?: str|null, PROD?: str|null}`; only the keys sent
  are changed. It is accepted only when the resulting `version_source` is `MANUAL` (else 422
  "Le versioni manuali richiedono un'applicazione manuale").
- `ApplicationOut` gets `manual_versions: {DEV, UAT, PROD}` (current values, `null` for AUTO
  applications) so the form can be filled.
- `CheckIn` unchanged; for manual applications the frontend checks the repository only.
- The write path is one service function `set_manual_versions(db, app, versions, user)` used
  by create and update (and by the future CI endpoint).
- OpenAPI regenerated with `--apim-compatible`; no new operation, so APIM only needs the
  updated schemas.

### Frontend

- Catalog, product form: "Nascondi dalla status page" checkbox; hidden products show a
  "nascosto" badge in the list.
- Catalog, application form:
  - "Nascondi dalla status page" checkbox;
  - "Versioni inserite a mano" toggle; when on, three inputs DEV / UAT / PROD (prefilled with
    the current versions) replace the environment part of "Verifica", which checks the repo only;
  - the application table shows "nascosta" and "manuale" badges; an application inside a
    hidden product shows "nascosta (prodotto)".
- Status page: a `MANUAL` cell shows a hand-edited icon (`PencilLine`) with the tooltip
  "versione inserita a mano" plus the usual dates; the row and the detail sheet show a
  "manuale" chip next to AKS/GHA.
- Types: `ReadStatus` gets `"MANUAL"`; `cellVisual` maps it to a new `manual` icon, readable
  (not muted).

## Constraints

- Backward compatible: defaults on every new column and field; existing API payloads valid.
- English in code, comments, docs and commits; UI copy in Italian as the rest of the hub.
- Manual-version editing gated by `manage:status_page` (frontend enforcement, like the rest of
  the catalog).
- No changes to CI/CD, infrastructure or dependency versions.

## Left to the builder

- Exact component split in the frontend form and test file names.
- Wording of badges and tooltips beyond the strings above.
- Index choices for the new columns (none expected to be needed).

## Prerequisite

The version lookup (`GET /releases/versions`) lives on `feature/status-page-version-lookup`;
this work builds on it and should start after it is merged into `main` (new branch from
`main`, per the repository's branching rules).
