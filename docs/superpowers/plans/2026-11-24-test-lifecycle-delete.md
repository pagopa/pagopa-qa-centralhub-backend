# Test Lifecycle Delete (Backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the hub delete a scope or a single feature, optionally deleting the linked Jira issues the user selected.

**Architecture:** A new service `app/services/test_lifecycle/deletion.py` owns the "deletable" rule, the delete previews and the deletions. Two preview endpoints and two DELETE endpoints are added to the existing router; `TlJiraClient` gets `delete_issue`. The list/scope payloads gain an additive `deletable` flag so the UI can disable the icon. The DB delete runs first (cascade FKs already cover revisions, scenarios, history, comments, anomalies); selected Jira issues are deleted afterwards, best-effort, and reported per key.

**Tech Stack:** FastAPI, SQLAlchemy async, Postgres, pytest (`uv run pytest`), ruff, httpx.

**Spec:** `/Users/andrea.ferracci/projects/pagopa-qa-centralhub-frontend/docs/superpowers/specs/2026-11-24-test-lifecycle-delete-and-restyle-design.md`
(the frontend plan `docs/superpowers/plans/2026-11-24-test-lifecycle-delete-frontend.md` in the frontend repo consumes this API; implement this plan first).

## Global Constraints

- A feature is NOT deletable when `published_revision_id` is not null, or `git_path` is not null, or its working revision has `pr_state == "open"`. Blocker reasons are exactly `"published"` and `"pr_open"`.
- A scope is deletable only if every one of its features is deletable; an empty scope is always deletable.
- Jira roles offered for deletion: `design`, `impl`, `qa_epic`. `qa_epic` has `default_selected = false`, `design`/`impl` have `true`. The feature preview never offers `qa_epic`; the scope preview offers only `TlScope.jira_qa_epic_key` (never a feature-level override epic). The `product` issue is never offered.
- Jira deletion never deletes subtasks (`deleteSubtasks=false`). A Jira 404 counts as already deleted. Jira failures never roll back the DB deletion.
- Error codes: 409 `feature_not_deletable`, 409 `scope_not_deletable` (detail has `code`, `message`, `blockers`), 422 `jira_key_not_linked`, existing 403 for unknown actor, existing 503 `jira_not_configured`, existing 404 for missing feature/scope.
- API changes are additive. Regenerate OpenAPI at the end: `uv run --extra dev python -m scripts.generate_openapi --apim-compatible`.
- Messages shown to users are Italian; code, comments, logs are English. No commits by the assistant: each task ends by proposing a commit message; the user commits.

## Review Focus

- Feature deleted twice (second call): 404, no Jira call made.
- `delete_jira_keys` contains a key not linked to the entity (or the product key): 422 `jira_key_not_linked`, nothing deleted in DB or Jira.
- Same Jira key used as design key by two features of a scope: offered and deleted once.
- Jira selected but Jira not configured: 503 before touching the DB.
- Scope with several features each having several revisions (self-referencing `base_revision_id`): the cascade delete succeeds.
- Selected key where Jira fails for one and succeeds for another: response lists both, DB rows are gone.

---

## File Structure

- Modify `app/services/test_lifecycle/jira_client.py`: `TlJiraClient.delete_issue`.
- Modify `tests/tl_fakes.py`: `FakeJiraClient.delete_issue`.
- Modify `app/schemas/test_lifecycle.py`: delete schemas, `deletable` on `TlFeatureListItem` and `TlScopeOut`.
- Create `app/services/test_lifecycle/deletion.py`: rule, previews, deletions.
- Modify `app/services/test_lifecycle/features.py` (`list_features`) and `scopes.py` (`list_scopes`/`to_out`): fill `deletable`.
- Modify `app/api/v1/test_lifecycle.py`: four endpoints and error translation.
- Create `tests/test_tl_delete.py`, `tests/test_tl_jira_client.py` additions.

### Task 1: Jira client `delete_issue` and fake

**Files:**
- Modify: `app/services/test_lifecycle/jira_client.py`
- Modify: `tests/tl_fakes.py` (class `FakeJiraClient`, ~line 220)
- Test: `tests/test_tl_jira_client.py` (follow its existing `httpx.MockTransport` style)

**Interfaces:**
- Produces: `TlJiraClient.delete_issue(self, key: str) -> None` and `FakeJiraClient.delete_issue(self, key: str) -> None` (records `("delete_issue", key)` via `_enter`, removes `key` from `self.issues`, honours `fail_once("delete_issue", ...)`).

- [ ] **Step 1: Write failing tests** in `tests/test_tl_jira_client.py`: `test_delete_issue_sends_delete_without_subtasks` (asserts method `DELETE`, path `/rest/api/3/issue/PQ-12`, query `deleteSubtasks=false`), `test_delete_issue_treats_404_as_success`, `test_delete_issue_raises_on_403` (`TlJiraError.status == 403`), `test_delete_issue_rejects_invalid_key` (`TlJiraError.status == 400`, no request sent).
- [ ] **Step 2: Run** `uv run pytest tests/test_tl_jira_client.py -k delete_issue -v`. Expected: FAIL (`AttributeError`).
- [ ] **Step 3: Implement** `delete_issue` using the same key validation as `get_issue`, then `_request("DELETE", f"/rest/api/3/issue/{key}", params={"deleteSubtasks": "false"}, ok_statuses=(404,))`. Add the fake method.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5:** Run `uv run ruff check .`; propose commit `feat(test-lifecycle): add Jira issue deletion to the client`.

### Task 2: Schemas, deletable rule, previews, `deletable` flag

**Files:**
- Modify: `app/schemas/test_lifecycle.py`
- Create: `app/services/test_lifecycle/deletion.py`
- Modify: `app/services/test_lifecycle/features.py` (`list_features`), `app/services/test_lifecycle/scopes.py` (`list_scopes`, `to_out`, `get_scope_out`)
- Modify: `app/api/v1/test_lifecycle.py`
- Test: `tests/test_tl_delete.py` (reuse fixtures `BASE, _new_feature, _use_test_db, actor, scope` exactly as imported at the top of `tests/test_tl_jira_tasks.py`; helper to mark a feature published: set `TlFeature.git_path` through `TestSession`; helper for open PR: set the working revision's `pr_state="open"`)

**Interfaces:**
- Produces schemas in `app/schemas/test_lifecycle.py`:
  - `TlDeleteJiraIssue(key: str, role: Literal["design","impl","qa_epic"], summary: str | None, status: str | None, url: str | None, feature_code: str | None, default_selected: bool)`
  - `TlDeleteBlocker(feature_id: uuid.UUID, code: str, reason: Literal["published","pr_open"])`
  - `TlDeleteAffectedFeature(id: uuid.UUID, code: str, title: str | None)`
  - `TlDeletePreview(deletable: bool, blockers: list[TlDeleteBlocker], features: list[TlDeleteAffectedFeature], scenario_count: int, jira_issues: list[TlDeleteJiraIssue])`
  - `TlDeleteRequest(actor_email: str, delete_jira_keys: list[str] = [])`
  - `TlJiraDeleteResult(key: str, status: Literal["deleted","failed"], reason: str | None = None)`
  - `TlDeleteResult(deleted_features: int, jira_results: list[TlJiraDeleteResult])`
  - `deletable: bool = True` added to `TlFeatureListItem` and `TlScopeOut`.
- Produces in `deletion.py`:
  - `def undeletable_clause() -> ColumnElement[bool]`: SQL condition on `TlFeature` for "blocked" (published revision, git path, or working revision with open PR; use an `exists` subquery on `TlFeatureRevision`).
  - `async def feature_blockers(db, feature: TlFeature) -> list[TlDeleteBlocker]` (one blocker per applicable reason, `published` first).
  - `async def preview_feature(db, feature_id: uuid.UUID) -> TlDeletePreview`, `async def preview_scope(db, scope_id: uuid.UUID) -> TlDeletePreview`.
  - Exceptions `FeatureNotFoundError`, `ScopeNotFoundError` (reuse `scopes.ScopeNotFoundError`; for features reuse the existing not-found exception of `features.py`, check its name) .
- Endpoints: `GET /features/{feature_id}/delete-preview`, `GET /scopes/{scope_id}/delete-preview`, both `response_model=TlDeletePreview`, 404 when missing.

- [ ] **Step 1: Write failing tests** in `tests/test_tl_delete.py`: `test_feature_preview_draft_is_deletable_and_lists_design_and_impl` (feature with `jira_design_key="PQ-1"`, `jira_impl_key="PQ-2"` set directly in DB with `jira_cache` summaries: both returned, `default_selected` true, no `qa_epic`, `deletable` true); `test_feature_preview_published_is_blocked` (`git_path` set: `deletable` false, blocker reason `published`); `test_feature_preview_open_pr_is_blocked` (reason `pr_open`); `test_scope_preview_lists_epic_unselected_and_dedups_shared_key` (two features sharing `PQ-1` as design key: one entry; epic entry has `role == "qa_epic"`, `default_selected` false, `feature_code` null); `test_scope_preview_blocked_names_blocking_features`; `test_scope_preview_empty_scope_is_deletable`; `test_preview_unknown_id_returns_404`; `test_list_endpoints_expose_deletable_flag` (feature list item and scope out show `deletable` false for a published feature).
- [ ] **Step 2: Run** `uv run pytest tests/test_tl_delete.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** schemas, `deletion.py` previews and the two GET endpoints. `scenario_count` = number of `TlScenario` rows of the affected features. Jira summary/status/url come from `feature.jira_cache[key]` when present (`url` falls back to `settings.jira_base_url/browse/<key>`, same as `jira_tasks.jira_links`). Fill `deletable` in `list_features` (select the `undeletable_clause()` as a labelled column; `deletable = not blocked`) and in scope output (`list_scopes`: one grouped query of scope ids having any feature matching `undeletable_clause()`; `get_scope_out`/`to_out` take the same information so single-scope responses are consistent).
- [ ] **Step 4: Run** `uv run pytest tests/test_tl_delete.py tests/test_tl_scopes.py tests/test_tl_features.py -v`. Expected: PASS.
- [ ] **Step 5:** `uv run ruff check .`; propose commit `feat(test-lifecycle): add delete previews and deletable flag`.

### Task 3: Delete a feature

**Files:**
- Modify: `app/services/test_lifecycle/deletion.py`, `app/api/v1/test_lifecycle.py`
- Test: `tests/test_tl_delete.py` (use `install_fake_jira(monkeypatch, FakeJiraClient())` from `tests/tl_fakes.py`)

**Interfaces:**
- Consumes: Task 1 `delete_issue`, Task 2 previews/blockers/schemas.
- Produces: `async def delete_feature(db, feature_id: uuid.UUID, data: TlDeleteRequest) -> TlDeleteResult`; exceptions `FeatureNotDeletableError(blockers: list[TlDeleteBlocker])`, `JiraKeyNotLinkedError(keys: list[str])`. Endpoint `DELETE /features/{feature_id}` with JSON body `TlDeleteRequest`, `response_model=TlDeleteResult`; `_translate` maps the new exceptions to the Global Constraints errors.

- [ ] **Step 1: Write failing tests**: `test_delete_feature_removes_rows_and_selected_jira` (feature with a working revision and one scenario; selecting only the design key: feature gone via `GET`→404, fake Jira `calls_of("delete_issue")` has only the design key, impl issue still in `fake.issues`, result has `deleted_features == 1` and one `deleted` entry); `test_delete_feature_without_keys_never_calls_jira`; `test_delete_published_feature_returns_409_with_blockers` (row untouched); `test_delete_feature_open_pr_returns_409`; `test_delete_feature_unlinked_key_returns_422_and_deletes_nothing` (use a key that is the feature's product key and another random key); `test_delete_feature_twice_returns_404_second_time_and_no_jira_call`; `test_delete_feature_jira_partial_failure_reports_per_key` (`fake.fail_once("delete_issue", 403, "forbidden")` with two keys: one `failed` with reason containing `forbidden`, one `deleted`, DB row gone); `test_delete_feature_jira_not_configured_returns_503_and_keeps_feature` (use `uninstall_jira`); `test_delete_feature_unknown_actor_returns_403`; `test_delete_feature_removes_open_anomalies` (create a `TlIssue` for it, assert none remain).
- [ ] **Step 2: Run** `uv run pytest tests/test_tl_delete.py -k delete_feature -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `delete_feature`: resolve actor (`actors.resolve_actor`), lock the feature row `FOR UPDATE` (404 if missing), recompute blockers (raise `FeatureNotDeletableError`), validate `delete_jira_keys` against the preview keys (raise `JiraKeyNotLinkedError`), get the Jira client via `jira_client_mod.get_jira_client()` only when keys are selected and BEFORE touching the DB, then Core `delete(TlFeature)` (not `session.delete`, to let the DB cascade) and commit; afterwards delete each selected key, catching `TlJiraError` per key into `TlJiraDeleteResult`. Log at info: actor email, feature code, deleted and failed keys.
- [ ] **Step 4: Run** `uv run pytest tests/test_tl_delete.py -v`. Expected: PASS.
- [ ] **Step 5:** `uv run ruff check .`; propose commit `feat(test-lifecycle): delete a feature with optional Jira cleanup`.

### Task 4: Delete a scope, OpenAPI, full suite

**Files:**
- Modify: `app/services/test_lifecycle/deletion.py`, `app/api/v1/test_lifecycle.py` (also replace the comment at line 75 "Nessun endpoint di cancellazione per gli ambiti..." with an accurate one)
- Test: `tests/test_tl_delete.py`
- Regenerate OpenAPI file produced by the script in the BE `CLAUDE.md`.

**Interfaces:**
- Consumes: Task 3 exceptions and helpers.
- Produces: `async def delete_scope(db, scope_id: uuid.UUID, data: TlDeleteRequest) -> TlDeleteResult` (`deleted_features` = number of features removed); `ScopeNotDeletableError(blockers)`; endpoint `DELETE /scopes/{scope_id}` with body `TlDeleteRequest`.

- [ ] **Step 1: Write failing tests**: `test_delete_empty_scope`; `test_delete_scope_with_features_removes_all_and_deletes_selected_jira` (two features, two revisions each, one scenario each; selects both design keys and not the epic: epic still in `fake.issues`, `deleted_features == 2`); `test_delete_scope_blocked_when_any_feature_published` (409 `scope_not_deletable`, blockers name the feature, all features still present); `test_delete_scope_can_delete_epic_when_selected`; `test_delete_scope_rejects_feature_level_override_epic_key` (422); `test_delete_scope_unknown_returns_404`.
- [ ] **Step 2: Run** `uv run pytest tests/test_tl_delete.py -k delete_scope -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `delete_scope` mirroring `delete_feature` (lock scope `FOR UPDATE`, blockers across its features, Core `delete(TlScope)`).
- [ ] **Step 4: Run** `uv run pytest tests/test_tl_delete.py -v`, then regenerate OpenAPI, then `uv run pytest -q` and `uv run ruff check .`. Expected: all green (report any pre-existing failures separately).
- [ ] **Step 5:** Propose commit `feat(test-lifecycle): delete a scope with optional Jira cleanup`, and report which OpenAPI file changed.
