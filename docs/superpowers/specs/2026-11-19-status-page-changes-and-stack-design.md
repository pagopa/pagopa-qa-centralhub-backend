# Status Page: change summaries and technology badge

## Intent

The Status Page already records every version change per application and environment
(`sp_deploy_events`). Two enrichments make that history more useful to QA and product teams:

1. **Change summaries.** For each detected deploy, show what changed between the previous and
   the new version in that environment: the pull requests (title, number, link) and the other
   meaningful commits. Shown in a popover on the version, both in the detail timeline and in
   the grid cells.
2. **Technology badge.** Next to the AKS/AZ and GHA/AZDO chips, a badge with the language and
   its version (e.g. `JAVA 21`, `PYTHON 3.12`), detected from the repository instead of being
   typed in the catalog.

Both read GitHub when a version changes, and both follow one rule decided with the user:
**reliable data or nothing**. When a summary or a version cannot be determined with certainty,
the UI says so (or shows less) instead of guessing.

## Goals

- A change summary per deploy event, per environment: the commits between the version that was
  in that environment and the new one (a skipped version is included; a rollback shows what was
  removed).
- A technology badge per application, refreshed when a new release is detected.
- Summaries for the deploys already recorded since the status page went live (16/11/2026),
  filled automatically because their tags are known.
- No effect on the version sync: enrichment never delays, fails or rate-limits it.

## Non-goals

- Reconstructing deploys that happened before the first sync (decided: not reliable).
- Using the GitHub release notes body (often empty).
- Detecting the AKS/AZ and GHA/AZDO values (they depend on the deployment, not the code).
- Per-environment technology (the badge describes the latest release, not what runs in PROD).
- Manual overrides of the detected technology.

## Anti-goals

- A summary or a version that looks authoritative but is guessed (e.g. commits from a wrong base,
  a language read from test scripts).
- Enrichment consuming the GitHub budget needed to read the versions.

## Approaches

1. **Enrich after the sync, cache by version pair (chosen).** A separate step runs after each
   sync commits, processes deploy events without a summary (bounded batch) and stores results in
   a cache keyed by `(repository, from_version, to_version)`. The read model joins the cache.
   Identical jumps in several environments cost one call; history since 16/11 is filled by the
   same loop; the sync is untouched.
2. **Fetch inside the sync.** Simpler wiring, but every GitHub hiccup would slow or fail the
   version sync, and a long backlog would block it. Rejected.
3. **Fetch on demand from the UI.** No storage, but each hover costs GitHub calls, results are not
   shared, and rate limits would surface as broken popovers. Rejected.

Chosen: 1, because it keeps the version sync as it is and makes every result reusable.

## Design

### Data (migration `0034`)

`sp_change_summaries` (cache, one row per version pair of a repository):

- `id UUID PK`
- `repository VARCHAR(200)`, `from_version VARCHAR(100)`, `to_version VARCHAR(100)`; unique
  `(repository, from_version, to_version)`
- `status VARCHAR(12)`: `OK` | `UNAVAILABLE` | `ERROR`
- `direction VARCHAR(10)` nullable: `forward` | `rollback` | `diverged` | `identical`
- `from_ref`, `to_ref` `VARCHAR(100)` nullable: the tags actually used
- `items JSONB` (list, max 50): `{kind: "pr"|"commit", number: int|null, title, author|null,
  url, sha}`
- `total_commits INT`, `truncated BOOL` (more commits than items, or GitHub's 250 cap reached)
- `attempts INT` default 0, `error VARCHAR(500)` nullable, `fetched_at TIMESTAMPTZ`

`sp_tech_stacks` (one row per repository):

- `repository VARCHAR(200) PK`
- `status VARCHAR(12)`: `OK` | `UNAVAILABLE` | `ERROR`
- `language VARCHAR(20)` nullable (`JAVA`, `PYTHON`, `NODE`, `RUST`, `GO`), `version VARCHAR(20)`
  nullable, `source VARCHAR(20)` nullable (`dockerfile` | `build_file`)
- `for_version VARCHAR(100)` (the REPO version as stored in `sp_version_state`, used to decide
  when to refresh), `ref VARCHAR(100)` nullable (the tag actually read), `attempts INT`,
  `error VARCHAR(500)`, `detected_at TIMESTAMPTZ`

Both tables are caches: no FK to applications, deleting an application leaves them (harmless and
reused if the repository comes back).

### Enrichment step

- New Celery task `app.tasks.enrich_status_page.enrich_status_page`, enqueued with `.delay()` by
  the sync task after `run_sync` returns, whatever its outcome (completed, partial, failed or
  skipped by the lock): enrichment only depends on what is already stored. No separate schedule
  and no CLI. It takes its own advisory lock (`ENRICH_LOCK_ID`, distinct from the sync's) and is
  skipped if another run holds it.
- **Scope**: every application, hidden or not, AUTO or MANUAL (hidden applications have a detail
  page; manual values that are real tags get a summary, the others become `UNAVAILABLE` after at
  most two tag lookups).
- Each run handles at most `status_page_enrich_batch` items (default 40) in this order: pending
  change summaries of the newest events first, then technology stacks. An item is one version
  pair or one repository: a pair shared by several events or environments counts once.
- **Pending** means: no cache row, or an `ERROR` row with fewer than 3 attempts. A technology
  stack is also pending when its `for_version` differs from the current REPO version (this resets
  `attempts` and re-evaluates `UNAVAILABLE` rows, e.g. a repository that publishes its first
  release). A change summary row is never refreshed once `OK` or `UNAVAILABLE`: a version pair is
  immutable.
- **Budget guard.** Only `GITHUB_TOKEN` is used. Every GitHub response's `x-ratelimit-remaining`
  is checked (no extra `/rate_limit` call): below `status_page_github_reserve` (default 1000, out
  of the 5000/hour of a token; the version sync uses about 210/hour) or on a rate-limit 403/429
  the run stops and leaves the rest for the next run. The version sync itself keeps working as
  today.
- Errors on one item never stop the batch. `ERROR` rows are retried on later runs up to 3
  attempts, then stay `ERROR` (shown as "non disponibile"). `UNAVAILABLE` is final.

### Change summaries

- **Which events**: env and REPO events with a non-null `previous_version` and `version`.
  Baselines and manual re-sets after a clear (`previous_version` null) never get a summary. Pair key =
  `(application.repository, previous_version, version)`.
- **Tag resolution**: for each version try the tag as read, then with a `v` prefix added or
  removed. If a version resolves to no tag (branch builds, odd manual values): `UNAVAILABLE`.
- **Compare**: `GET /repos/pagopa/{repo}/compare/{from_ref}...{to_ref}`:
  - `ahead` → `forward`, items from the commits;
  - `behind` → `rollback`: one more call `compare/{to_ref}...{from_ref}`, items are the commits
    removed;
  - `identical` → `identical`, no items;
  - `diverged` → `diverged`, items are the commits of `to_ref` not in `from_ref`.
- **Items**, in order:
  - a commit whose subject ends with `(#N)` (squash merge) → `pr` with that title (suffix
    removed) and `https://github.com/pagopa/{repo}/pull/N`;
  - `Merge pull request #N from …` → `pr` titled with the first non-empty body line;
  - noise is dropped: subjects containing `[skip ci]`, `Merge branch …`,
    `Merge remote-tracking branch …`;
  - anything else → `commit` with its subject and commit URL;
  - newest first, max 50; `truncated` when commits were dropped by the cap or GitHub's 250 limit.
- **Backfill**: no special code. The step processes every event lacking a summary, newest first,
  so the history since 16/11 is filled over the first runs.

### Technology badge

- **When**: a repository has no `sp_tech_stacks` row, or its REPO version changed since the row's
  `ref`. The ref read is the REPO version's tag (the latest release); repositories without a
  release are not detected (`UNAVAILABLE`).
- **Refresh**: see "Pending" above; `for_version` is compared with the REPO version exactly as
  stored, and `ref` records the tag variant that resolved.
- **Reads at that tag**: the root listing (1 call), then only the files present among
  `Dockerfile`, `pom.xml`, `build.gradle(.kts)`, `pyproject.toml`, `requirements.txt`,
  `.python-version`, `package.json`, `.nvmrc`, `Cargo.toml`, `rust-toolchain(.toml)`, `go.mod`.
- **Language**, from build files: `pom.xml`/`build.gradle*` → `JAVA`; `pyproject.toml` or
  `requirements.txt` → `PYTHON`; `package.json` → `NODE`; `Cargo.toml` → `RUST`; `go.mod` → `GO`.
  Several build files → the one matching the Dockerfile's runtime image; still ambiguous → no
  language. No build file at the root → `UNAVAILABLE` (no fallback on GitHub's language
  statistics: they count test scripts and would break the "reliable or nothing" rule).
- **Version**, the runtime wins:
  - Dockerfile **final stage** image of the same language: `eclipse-temurin:21-jre`,
    `amazoncorretto:17…`, `…openjdk17…` → 21/17; `python:3.12-slim` → 3.12; `node:20-alpine` → 20;
    `rust:1.75` → 1.75; `golang:1.22` → 1.22;
  - otherwise the build file: `<java.version>`, `<maven.compiler.release>`,
    `<maven.compiler.source>`, `JavaLanguageVersion.of(N)`; `requires-python` (`>=3.12`, `~=3.12`,
    `^3.12` → 3.12; other ranges → empty) or `.python-version`; `engines.node` (major) or `.nvmrc`;
    `rust-version` or the toolchain channel; the `go` line of `go.mod`;
  - nothing certain → language without version.
- **Frontends**: same rules; an `nginx` runtime image does not match NODE, so the version comes
  from `package.json`/`.nvmrc`.

### API (additive)

- `ApplicationRowOut.stack: {language, version} | null` (only `OK` rows; `version` may be null).
- `DeployEventOut.changes: ChangeSummaryOut | null` where `ChangeSummaryOut = {status, direction,
  items, total_commits, truncated, compare_url}`; `null` exactly when `previous_version` is null
  (baselines, manual re-sets after a clear); `{status: "PENDING"}` while not fetched yet.
  `get_application_detail` loads the summaries of all its events in one query (pairs of the
  application's repository). The existing `DeployEventOut.direction` (upgrade/rollback/... from
  the version comparison) keeps driving the timeline icon; `changes.direction` (from git) drives
  the popover title.
- The grid payload (`/status`) does not carry summaries: the cell popover reads them from the
  application detail endpoint, fetched on hover/focus and cached by TanStack Query.
- OpenAPI regenerated with `--apim-compatible`; no new operation.

### Frontend

- **Technology badge**: a neutral chip after AKS/GHA in the grid row and the detail header (not
  in the catalog table: the catalog API stays unchanged): `JAVA 21`, or `JAVA` when the version
  is unknown; nothing when unavailable. Tooltip: "Rilevato da Dockerfile | file di build alla
  release <ref>".
- **Change popover** (same content in both places):
  - title "Modifiche da <from> a <to>" (rollback: "Rollback da <from> a <to> — modifiche rimosse";
    diverged: add "le versioni non sono in sequenza");
  - PR list (number, title, author, link), then "Altri commit" collapsed by default;
  - footer: "N commit · Confronto su GitHub" (link), "elenco parziale" when `truncated`;
  - `PENDING` → "Riepilogo in preparazione"; `UNAVAILABLE`/`ERROR` → "Riepilogo non disponibile
    (le versioni non corrispondono a tag GitHub)" / "(GitHub non ha risposto)";
  - no summary (baseline) → no popover trigger.
- **Where**: on the version of each timeline entry in the detail sheet (click or hover/focus),
  and on the version in the DEV/UAT/PROD/REPO grid cells (hover/focus), showing the latest event
  of that environment, read from the application detail fetched on hover/focus. A cell whose
  latest event has no summary (`changes: null`) has no popover trigger. The existing read-status
  tooltip on the cell icons stays as it is.
- Keyboard and screen reader accessible (focusable trigger, popover role, Escape closes).

## Constraints

- Backward compatible: new tables and additive fields only; the sync behaviour is unchanged.
- GitHub calls use `GITHUB_TOKEN` (shared with other features) and keep `status_page_github_reserve`
  calls free; settings `status_page_enrich_batch`, `status_page_github_reserve` with defaults.
- English in code and docs, Italian UI copy; service style (structlog, try/except → log → raise).
- No CI/CD, infrastructure or dependency changes.

## Left to the builder

- Exact module split (e.g. `status_page_changes.py`, `status_page_stack.py`, a shared GitHub
  reader), parsing helpers and their tests.
- Popover component choice (existing `popover` primitive in `components/ui`).
- Wording of secondary UI strings beyond those above.
