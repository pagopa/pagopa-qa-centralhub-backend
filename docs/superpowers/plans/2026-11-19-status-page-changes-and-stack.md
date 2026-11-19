# Status Page Change Summaries and Technology Badge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For every detected deploy, store and show the pull requests and commits between the previous and the new version in that environment, and show a technology badge (language and version) detected from each repository's latest release.

**Architecture:** A new Celery task runs after every status page sync, outside the sync's lock and transaction. It reads GitHub with `GITHUB_TOKEN` under a budget guard and fills two cache tables: `sp_change_summaries` (one row per repository + version pair, from the compare API) and `sp_tech_stacks` (one row per repository, from the files at the latest release tag). The read model joins them into the application detail (`DeployEventOut.changes`) and the grid rows (`ApplicationRowOut.stack`). The frontend adds a change popover on versions (timeline and grid cells, the latter fetching the detail on hover) and a neutral technology chip.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Celery, httpx, pytest (asyncio auto, real Postgres via `tests/_db.py`); Next.js 16, React 19, TanStack Query, Tailwind hub tokens, lucide-react, vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-11-19-status-page-changes-and-stack-design.md` (backend repo).

## Global Constraints

- Two repos. Backend tasks 1–6 on `feature/status-page-changes-and-stack` (already created from `main` after #81, holding the untracked spec and this plan). Frontend tasks 7–8 on `feature/status-page-changes-and-stack` created from an up-to-date frontend `main` (`git checkout main && git pull --ff-only`); if the frontend checkout has uncommitted changes, stop and ask.
- **Commits:** local commits only if the user authorizes them for this plan; never push, never open a PR. Conventional Commits, no AI attribution trailers. The first backend commit also adds the spec and this plan.
- Everything in the repo is English; UI copy is Italian.
- **Reliable data or nothing**: a summary is built only from a compare between two resolved tags; a version is shown only when parsed from the runtime image or a build file. Anything uncertain becomes `UNAVAILABLE` / no badge, never a guess.
- The version sync (`run_sync`) behaviour and timing are unchanged; enrichment failures never mark the sync failed.
- GitHub owner is `pagopa` (as `status_page_sources.GITHUB_OWNER`); token `settings.github_token` only. New settings: `status_page_enrich_batch: int = 40`, `status_page_github_reserve: int = 1000`.
- Cache statuses: `OK`, `UNAVAILABLE` (final), `ERROR` (retried, max 3 attempts). API-only status: `PENDING`. Change directions: `forward`, `rollback`, `diverged`, `identical`. Languages: `JAVA`, `PYTHON`, `NODE`, `RUST`, `GO`. Stack sources: `dockerfile`, `build_file`.
- Items: max 50 per summary, newest first; noise subjects dropped: containing `[skip ci]`, starting with `Merge branch ` or `Merge remote-tracking branch `.
- Backend checks: `uv run --extra dev ruff check <changed files>`, `uv run --extra dev ruff format --check <changed files>`, `uv run --extra dev mypy <changed app files>` (no new errors; the repo has ~128 pre-existing), `uv run --extra dev pytest`. Known failures on `main`: `test_ea_service::test_seed_file_imports_cleanly`, 8× `test_ea_sync_tracing`, `test_gpd_position_service::test_sync_from_source_creates_new_snapshot`. Never run `ruff --fix` on the whole repo. No real GitHub calls in tests (httpx `MockTransport`).
- OpenAPI: `uv run --extra dev python -m scripts.generate_openapi --apim-compatible`.
- Frontend checks: `pnpm test`, `pnpm lint` (0 errors), `pnpm exec tsc --noEmit`. Do not upgrade dependencies.

## Review Focus

1. A version that is not a GitHub tag (DEV branch builds, free-text manual values) must give `UNAVAILABLE`, never commits compared against a wrong ref.
2. A rate-limit response or a remaining budget below the reserve mid-batch must stop the run without writing `ERROR` rows for the untouched items; the next run picks them up.
3. A rollback pair (new version older than the previous one) must list the commits removed, not an empty summary.
4. A multi-stage Dockerfile (maven build stage, temurin runtime) must take the version from the final stage only.
5. The same version pair in DEV, UAT and PROD must cost one compare and show the same summary in the three environments.

---

### Task 1: Tables, migration 0034 and settings

**Files:**
- Modify: `app/models/status_page.py`, `app/config.py`
- Create: `alembic/versions/0034_status_page_changes_and_stack.py`
- Test: `tests/test_status_page_migration_0034.py`

**Interfaces:**
- Produces: `SpChangeSummary` (`id`, `repository`, `from_version`, `to_version`, `status`, `direction`, `from_ref`, `to_ref`, `items: list[dict]` JSONB default `[]`, `total_commits` default 0, `truncated` default false, `attempts` default 0, `error`, `fetched_at`; unique `uq_sp_change_summaries_pair` on `(repository, from_version, to_version)`); `SpTechStack` (`repository` PK, `status`, `language`, `version`, `source`, `for_version`, `ref`, `attempts`, `error`, `detected_at`); constants `ENRICH_STATUSES = ("OK", "UNAVAILABLE", "ERROR")`, `CHANGE_DIRECTIONS`, `STACK_LANGUAGES`, `STACK_SOURCES` with check constraints; settings `status_page_enrich_batch`, `status_page_github_reserve`.

- [ ] **Step 1: Write the failing tests**: a summary row with only the pair and status reads back defaults (`items == []`, `total_commits == 0`, `truncated is False`, `attempts == 0`); a duplicate pair raises `IntegrityError`; `status="X"` raises; a stack row round-trips; settings defaults are 40 and 1000.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_migration_0034.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** models, settings and the migration (`down_revision = "0033"`, downgrade drops both tables).
- [ ] **Step 4: Apply and test**: `uv run alembic upgrade head`, then once `downgrade -1` and `upgrade head`; run the tests. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: add the change summary and tech stack cache tables` (with the spec and this plan).

### Task 2: GitHub reader with budget guard and tag resolution

**Files:**
- Create: `app/services/status_page_github.py`
- Test: `tests/test_status_page_github.py`

**Interfaces:**
- Produces:
  - `class GitHubBudgetExhaustedError(Exception)`; `class GitHubNotFoundError(Exception)`.
  - `class EnrichGitHub` with `__init__(self, client: httpx.AsyncClient, reserve: int)`; `async def get_json(self, path: str) -> Any` (GET `https://api.github.com{path}`, `Authorization: Bearer {settings.github_token}`, `Accept: application/vnd.github+json`; raises `GitHubNotFoundError` on 404, `GitHubBudgetExhaustedError` on 403/429 with `x-ratelimit-remaining: 0` or when the header of any response is below `reserve` (the response that crossed the reserve is still returned once; the next call raises); other non-2xx → `httpx.HTTPStatusError`); `async def get_text(self, path: str, ref: str) -> str | None` (contents API, base64 decoded, `None` on 404); property `remaining: int | None`.
  - `async def resolve_tag(gh: EnrichGitHub, repository: str, version: str) -> str | None`: tries `version`, then `version` with a leading `v` added (if absent) or removed (if present), via `GET /repos/pagopa/{repo}/git/ref/tags/{tag}`; returns the first that exists.
- [ ] **Step 1: Write the failing tests** (httpx `MockTransport`): auth header sent; 404 → `GitHubNotFoundError`; 403 with remaining 0 → budget error; remaining below reserve → that call returns, the following raises; `resolve_tag` returns `"1.2.3"`, `"v1.2.3"` for a repo tagged with `v`, `"1.2.3"` when asked `"v1.2.3"` and only the bare tag exists, `None` when neither exists (exactly 2 lookups); `get_text` decodes base64 and returns `None` on 404.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_github.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: add a budget-guarded GitHub reader for status page enrichment`.

### Task 3: Change summaries

**Files:**
- Create: `app/services/status_page_changes.py`
- Test: `tests/test_status_page_changes.py`

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - `def summarize_commits(repository: str, commits: list[dict]) -> tuple[list[dict], bool]`: commits as returned by the compare API (oldest first); returns items newest first (max 50) and whether items were dropped by the cap. Rules: subject ending `(#N)` → `{"kind": "pr", "number": N, "title": subject without the suffix, "author": author login or None, "url": https://github.com/pagopa/{repo}/pull/N, "sha"}`; subject `Merge pull request #N from …` → `pr` titled with the first non-empty body line (fallback: the subject); noise dropped (Global Constraints); everything else → `{"kind": "commit", "number": None, "title": subject, "author", "url": commit html_url, "sha"}`.
  - `@dataclass ChangeResult` (`status`, `direction`, `from_ref`, `to_ref`, `items`, `total_commits`, `truncated`, `error`).
  - `async def fetch_change_summary(gh: EnrichGitHub, repository: str, from_version: str, to_version: str) -> ChangeResult`: resolve both tags (any `None` → `UNAVAILABLE` with error "versione non corrispondente a un tag"); `GET /repos/pagopa/{repo}/compare/{from}...{to}`; `ahead` → `forward`; `behind` → `rollback` with a second compare `{to}...{from}` whose commits are the items; `identical` → no items; `diverged` → items from the first compare; `total_commits` from the compare used for the items; `truncated` when the cap dropped items or `total_commits > len(commits)`. Lets `GitHubBudgetExhaustedError` propagate.
- [ ] **Step 1: Write the failing tests**: `summarize_commits` on fixtures with a squash `(#225)` commit, a `Merge pull request #12 from x/y` + body, a `Bump to version 1.7.8 [skip ci]`, a `Merge remote-tracking branch 'origin/main'`, a plain `readme` → 3 items in newest-first order with the right kinds/urls; 60 plain commits → 50 items, truncated. `fetch_change_summary` with MockTransport: forward, rollback (two compares, removed commits), identical, diverged, unresolved tag → `UNAVAILABLE` and no compare call, budget error propagates.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_changes.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: build status page change summaries from GitHub compares`.

### Task 4: Technology detection

**Files:**
- Create: `app/services/status_page_stack.py`
- Test: `tests/test_status_page_stack.py`

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - Pure parsers, each `-> str | None`: `runtime_from_dockerfile(text: str) -> tuple[str, str | None] | None` (last `FROM` line only; image name maps to a language: `eclipse-temurin`, `amazoncorretto`, `openjdk`, `*openjdk<N>*`, `*-jdk-*`/`*-jre*` of those → `JAVA`; `python` → `PYTHON`; `node` → `NODE`; `rust` → `RUST`; `golang` → `GO`; version = leading major(.minor) of the tag, or the `openjdk<N>` number in the image name; `nginx` or unknown images → `None`); `java_from_pom(text)` (`<java.version>`, `<maven.compiler.release>`, `<maven.compiler.source>`, in this order; `1.8` → `8`); `java_from_gradle(text)` (`JavaLanguageVersion.of(N)`, `sourceCompatibility = N` / `JavaVersion.VERSION_N`); `python_from_pyproject(text)` (`requires-python` `>=X.Y`, `~=X.Y`, `^X.Y`, `==X.Y.*` → `X.Y`; other forms → `None`); `python_from_version_file(text)`; `node_from_package(text)` (`engines.node` major from `>=20`, `20.x`, `^20`; ranges with two majors → `None`); `node_from_nvmrc(text)`; `rust_from_cargo(text)` (`rust-version`); `rust_from_toolchain(text)` (numeric channel only); `go_from_mod(text)` (`go 1.22`).
  - `@dataclass StackResult` (`status`, `language`, `version`, `source`, `ref`, `error`).
  - `async def detect_stack(gh: EnrichGitHub, repository: str, repo_version: str) -> StackResult`: resolve the tag (`None` → `UNAVAILABLE`); root listing `GET /repos/pagopa/{repo}/contents?ref={tag}`; language from build files (spec rules, `requirements.txt` included); several languages → keep the one matching the Dockerfile runtime, else `UNAVAILABLE`; no build file → `UNAVAILABLE`; version: Dockerfile runtime of the same language (`source="dockerfile"`), else the matching build-file parser (`source="build_file"`), else language only (`version=None`, `source=None`).
- [ ] **Step 1: Write the failing tests**: one test per parser with the real examples (`FROM ghcr.io/pagopa/docker-base-springboot-openjdk17:v2.2.7@sha256:…` → JAVA 17; three-stage maven/temurin Dockerfile → JAVA 21 from the last stage; `FROM python:3.12-slim@sha256:… AS runtime` → PYTHON 3.12; `FROM nginx:1.27` → None; `<java.version>17</java.version>` → 17; `requires-python = ">=3.12"` → 3.12; `">=3.9,<3.13"` → None; `engines.node ">=18 <21"` → None). `detect_stack` with MockTransport: Java repo with Dockerfile → `dockerfile`; Python repo without Dockerfile → `build_file`; frontend with `package.json` + nginx Dockerfile → NODE from `package.json`; `pom.xml` + `package.json` with a temurin runtime → JAVA; no build file → `UNAVAILABLE`; unresolved tag → `UNAVAILABLE`.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_stack.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: detect the technology of status page repositories`.

### Task 5: Enrichment runner and Celery task

**Files:**
- Create: `app/services/status_page_enrich.py`, `app/tasks/enrich_status_page.py`
- Modify: `app/tasks/sync_status_page.py` (enqueue), `app/tasks/__init__.py` (register)
- Test: `tests/test_status_page_enrich.py`, `tests/test_status_page_sync.py` (enqueue)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `ENRICH_LOCK_ID = 672`.
  - `async def run_enrichment(db: AsyncSession, *, client: httpx.AsyncClient | None = None) -> dict[str, int]` returning counters `{"summaries": n, "stacks": m, "stopped_by_budget": 0|1}`: takes `pg_try_advisory_lock(ENRICH_LOCK_ID)` on a separate connection (skip with zeros if held, like `run_sync`); returns zeros without a GitHub call when `settings.github_token` is empty; pending pairs = distinct `(application.repository, event.previous_version, event.version)` over events with both versions not null, without a cache row or with an `ERROR` row with `attempts < 3`, ordered by the newest event `detected_at`; pending stacks = repositories of applications with a REPO state version, without a row or whose `for_version` differs from that version, or `ERROR` with `attempts < 3` and same `for_version`; processes up to `status_page_enrich_batch` items, summaries first; upserts rows (`attempts` +1 on `ERROR`, reset to 0 when `for_version` changes); commits after each item; stops on `GitHubBudgetExhaustedError` without writing that item.
  - Celery task `app.tasks.enrich_status_page.enrich_status_page` (no args) calling `run_enrichment` with `async_session()`.
  - The sync task enqueues it with `.delay()` in a `finally` after `run_sync` (success, failure or lock skip); an enqueue error is logged, never raised.
- [ ] **Step 1: Write the failing tests** (real DB, GitHub via MockTransport): two events with the same pair in UAT and PROD → one row, one compare call; ERROR row with attempts 2 → retried once more then not again at 3; OK/UNAVAILABLE rows not re-fetched; baseline events skipped; batch limit respected and newest events first; budget exhaustion after the first item → one row written, counters report the stop, the rest pending on the next run; stack refreshed when the REPO version changes (attempts reset), not when unchanged; no token → no calls; lock held → skipped. Sync task: `.delay` called after success and after `run_sync` raises (patch both).
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_enrich.py tests/test_status_page_sync.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement**.
- [ ] **Step 4: Run** the files. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: enrich status page deploys after each sync`.

### Task 6: Read model, API, OpenAPI and README

**Files:**
- Modify: `app/schemas/status_page.py`, `app/services/status_page.py`, `openapi/qa-hub-be.json`, `README.md`
- Test: `tests/test_status_page_service.py`

**Interfaces:**
- Produces: `StackOut {language: str, version: str | None, source: str | None, ref: str | None}`; `ApplicationRowOut.stack: StackOut | None` (only `OK` rows); `ChangeItemOut {kind, number, title, author, url}`; `ChangeSummaryOut {status: "OK"|"UNAVAILABLE"|"ERROR"|"PENDING", direction: str | None, items: list[ChangeItemOut], total_commits: int, truncated: bool, compare_url: str | None}`; `DeployEventOut.changes: ChangeSummaryOut | None` (`None` iff `previous_version` is None; `PENDING` when no row or an `ERROR` row with attempts < 3). `compare_url = https://github.com/pagopa/{repo}/compare/{from_ref}...{to_ref}` when both refs are known.
- [ ] **Step 1: Write the failing tests**: grid row carries `stack` for an `OK` row and `None` for `UNAVAILABLE`/missing; `get_status` query count still within the current bound plus at most one query; detail events get `changes` from the cache with one query for all events, `None` for baseline, `PENDING` without a row, `ERROR` with attempts ≥ 3 → `ERROR`; rollback summary keeps `direction="rollback"` while `DeployEventOut.direction` stays the version-based value.
- [ ] **Step 2: Run them**: `uv run --extra dev pytest tests/test_status_page_service.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement**, regenerate the OpenAPI (only schemas change, no new operation), add a README paragraph (enrichment task, settings, "reliable or nothing", GitHub budget).
- [ ] **Step 4: Run** the full backend checks. Expected: only the known failures.
- [ ] **Step 5: Commit**: `feat: expose change summaries and technology on the status page API`.

### Task 7: Frontend types and technology badge

**Files:**
- Modify: `src/types/release-status.ts`, `src/components/release-status/shared.tsx`, `ApplicationRow.tsx`, `ApplicationDetailSheet.tsx`
- Test: `src/components/release-status/ApplicationDetailSheet.test.tsx`, `src/app/(portal)/releases/page.test.tsx`

**Interfaces:**
- Produces: types `TechStack`, `ChangeItem`, `ChangeSummary`, `ApplicationRow.stack: TechStack | null`, `DeployEvent.changes: ChangeSummary | null`; `StackChip({ stack })` in `shared.tsx`: neutral chip `JAVA 21` / `JAVA`, nothing for `null`, tooltip "Rilevato da Dockerfile|file di build alla release <ref>".
- [ ] **Step 1: Write the failing tests**: grid row and detail header show `JAVA 21`; `JAVA` alone when version is null; no chip when stack is null; tooltip text.
- [ ] **Step 2: Run them**: `pnpm vitest run src/components/release-status "src/app/(portal)/releases"`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** (fixtures gain `stack: null` / `changes: null` where needed).
- [ ] **Step 4: Run** the same command and `pnpm exec tsc --noEmit`. Expected: PASS.
- [ ] **Step 5: Commit**: `feat: show the detected technology on the status page`.

### Task 8: Change popover in the timeline and the grid cells

**Files:**
- Create: `src/components/release-status/ChangePopover.tsx`
- Modify: `DeployTimeline.tsx`, `VersionCell.tsx` (or `ApplicationRow.tsx`), `src/hooks/useReleaseStatus.ts` (prefetch helper if needed)
- Test: `src/components/release-status/ChangePopover.test.tsx`, `DeployTimeline` tests in `ApplicationDetailSheet.test.tsx`, `src/app/(portal)/releases/page.test.tsx`

**Interfaces:**
- Consumes: Task 7 types; `useApplicationDetail(id)`; `popover` primitive in `src/components/ui/popover.tsx`.
- Produces: `ChangePopover({ fromVersion, toVersion, changes, children })` — trigger wraps the version text (focusable button), content per spec copy: titles for forward / rollback / diverged / identical, PR list (number, title, author, link), "Altri commit" collapsed, footer "N commit · Confronto su GitHub" and "elenco parziale" when truncated; `PENDING` → "Riepilogo in preparazione"; `UNAVAILABLE` → "Riepilogo non disponibile (le versioni non corrispondono a tag GitHub)"; `ERROR` → "Riepilogo non disponibile (GitHub non ha risposto)". No trigger when `changes` is null.
- [ ] **Step 1: Write the failing tests**: popover content for each status/direction; links to PR and compare; collapsed other commits; Escape closes; timeline version opens it; a grid cell version on hover/focus calls `useApplicationDetail` with the app id and shows the latest event of that environment; no trigger for a baseline; the cell read-status tooltip still works.
- [ ] **Step 2: Run them**: `pnpm vitest run src/components/release-status "src/app/(portal)/releases"`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** (hub tokens only; light/dark readable; works at 375 px).
- [ ] **Step 4: Full frontend checks**. Expected: all pass, 0 lint errors.
- [ ] **Step 5: Commit**: `feat: show what changed in each deploy on the status page`.
