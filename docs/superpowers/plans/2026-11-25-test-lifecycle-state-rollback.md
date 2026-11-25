# Test Lifecycle state rollback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a feature go back from "Progettata" to "In review" (reviewer) and from "In review" to "Bozza" (design owner, no note), leaving the open PR untouched and warning about it in the UI.

**Architecture:** Two new transition actions (`withdraw_approval`, `withdraw`) in the existing transitions endpoint of `app/services/test_lifecycle/workflow.py`, with guards, history events and a 403 for the wrong actor. The frontend lists them in `FeatureHeader` (only when relevant), shows an open-PR warning, and updates the guide diagram. Two repos, two PRs: backend first.

**Tech Stack:** Backend: FastAPI, SQLAlchemy async, Postgres, pytest (`uv run pytest`), ruff. Frontend: Next.js 16, React 19, Tailwind 4, Vitest, pnpm.

**Spec:** `docs/superpowers/specs/2026-11-25-test-lifecycle-state-rollback-design.md` (in the backend repo)

## Global Constraints

- Backend repo `/Users/andrea.ferracci/projects/pagopa-qa-centralhub-backend`, frontend repo `/Users/andrea.ferracci/projects/pagopa-qa-centralhub-frontend`; each part on its own branch `feature/test-lifecycle-state-rollback` cut from an up-to-date default branch (`git checkout main && git pull --ff-only`). Never commit, push or open PRs: the user does it.
- Action names are exactly `withdraw_approval` ("Riporta in review") and `withdraw` ("Ritira in bozza"); they are appended after `submit`, `approve`, `request_changes`, `reopen`.
- `withdraw_approval`: only the assigned reviewer (403 `not_reviewer`); allowed only when the revision is `approved` and `pr_state != "merged"` (reason "La PR è già stata mergiata: usa Riapri" when merged; reason "La revisione non è approvata" otherwise).
- `withdraw`: only the design owner (403 `not_owner`, reason "Solo l'owner di progettazione può eseguire questa azione"); allowed only when the revision is `in_review` (reason "La revisione non è in revisione").
- Revision `approved_by`/`approved_at` are cleared by `withdraw_approval`; PR fields, Jira keys and existing snapshot versions are never touched; feature status changes only on first publication (`feature.published_revision_id is None`), like the existing transitions.
- History events: `tl.revision.approval_withdrawn` and `tl.revision.withdrawn`, payload `{revision_id, revision_number}`.
- No database migration. Code, comments, docs and commit text in English; UI copy Italian.
- Backend: ruff clean on touched files (baseline has about 1063 pre-existing errors elsewhere), OpenAPI regenerated with `uv run --extra dev python -m scripts.generate_openapi --apim-compatible`; DB tests need the local Postgres (`docker-compose`, run `uv run alembic upgrade head` if the DB is empty). Frontend: `pnpm test`, `pnpm lint`, `npx tsc --noEmit`; read `node_modules/next/dist/docs/` before Next code.

## Review Focus

- Withdrawing an approval on a feature that already has a published revision (reopened flow): the feature status must not change (`test_feature_status_unchanged_when_already_published` is the pattern).
- Re-approval after a withdrawal: a second snapshot version, same PR number, no second Jira task, no second PR.
- Withdrawing when the PR creation had failed or was never configured (`pr_state` is `None` or `closed`): allowed, nothing breaks.
- Double click / concurrent calls: the second call gets 409 `transition_not_allowed` (revision no longer in the required state), never a 500.
- UI: the new buttons never appear for statuses where they cannot apply; the open-PR warning never appears for an ordinary draft without PR or for a merged revision.

---

## Part A — Backend

### Task 1: Transition actions, guards and effects

**Files:**
- Modify: `app/schemas/test_lifecycle.py` (`TlTransitionAction`)
- Modify: `app/services/test_lifecycle/workflow.py` (`available_transitions`, `apply_transition`, new reasons, `NotOwnerError`)
- Modify: `app/api/v1/test_lifecycle.py` (`_translate`)
- Test: `tests/test_tl_workflow.py`

**Interfaces:**
- Consumes: existing helpers in `tests/test_tl_workflow.py` (`_ready_feature`, `_do`, `_get`, `_transition`, `reviewer`, `actor`, `scope`) and `TestSession` for direct DB setup.
- Produces:
  - `TlTransitionAction.WITHDRAW_APPROVAL = "withdraw_approval"`, `TlTransitionAction.WITHDRAW = "withdraw"` (appended).
  - `workflow.NOT_OWNER_REASON: str`, `workflow.PR_ALREADY_MERGED_REASON: str`, `workflow.NotOwnerError(Exception)`.
  - `available_transitions(...)` returns six entries; the first four keep their order; entries 5 and 6 are `withdraw_approval` and `withdraw` (with `revision is None`: both not allowed, reason "Nessuna revisione di lavoro aperta").
  - API: 403 `{"code": "not_owner", "message": "Solo l'owner di progettazione può ritirare la revisione"}` for `NotOwnerError`.

- [ ] **Step 1: Write failing tests in `tests/test_tl_workflow.py`** (each uses the existing fixtures; to simulate the open PR set `pr_number=7, pr_state="open", pr_branch="tl/x"` on the revision through `TestSession`)
  - `test_transitions_append_the_two_withdraw_actions`: action order is `[submit, approve, request_changes, reopen, withdraw_approval, withdraw]`.
  - `test_withdraw_approval_returns_revision_to_review`: after submit+approve (+ simulated open PR), the reviewer calls `withdraw_approval` → 200, feature `status == "in_review"`, revision `status == "in_review"`, `approved_by is None`, `approved_at is None`, `pr_number == 7`, `pr_state == "open"`, `jira_impl_key` unchanged, exactly one `tl_feature_versions` row, history contains `tl.revision.approval_withdrawn`.
  - `test_withdraw_approval_by_non_reviewer_is_forbidden`: the design owner gets 403 `not_reviewer`; `_transition(view_for_owner, "withdraw_approval")["allowed"]` is `False` with `["Solo il reviewer assegnato può eseguire questa azione"]`.
  - `test_withdraw_approval_blocked_when_pr_merged`: revision `pr_state="merged"` (and status `merged`/approved as in the merge flow) → 409 `transition_not_allowed`, reasons contain "La PR è già stata mergiata: usa Riapri".
  - `test_withdraw_approval_requires_approved_revision`: on a revision `in_review` → 409 with reason "La revisione non è approvata".
  - `test_withdraw_approval_keeps_feature_status_when_already_published`: pattern of `test_feature_status_unchanged_when_already_published`; feature status unchanged.
  - `test_withdraw_returns_in_review_to_draft_by_owner_without_note`: owner calls `withdraw` → 200, revision and feature `draft`, no `TlReviewComment` created, history contains `tl.revision.withdrawn`.
  - `test_withdraw_by_non_owner_is_forbidden`: reviewer gets 403 `not_owner`; unknown actor 403 as the other transitions; revision still `in_review`.
  - `test_withdraw_requires_in_review_revision`: on `draft` and on `approved` → 409 with reason "La revisione non è in revisione".
  - `test_second_withdraw_call_returns_409`: calling `withdraw` twice → first 200, second 409 `transition_not_allowed`.
  - `test_reapproval_after_withdrawal_creates_second_snapshot`: submit, approve, `withdraw_approval`, approve again → two version rows, `jira_impl_key` not recreated (use `FakeJiraClient` from `tests/tl_fakes.py` and assert `create_issue` call count did not increase on the second approval).
- [ ] **Step 2: Run** `uv run pytest tests/test_tl_workflow.py -q` — Expected: new tests FAIL (unknown action / missing behaviour), existing ones PASS.
- [ ] **Step 3: Implement `TlTransitionAction` members and the two checks in `available_transitions`** in `workflow.py`: `_withdraw_approval_reasons(feature, revision, actor)` (reviewer identity like `_review_reasons`, then `revision.status == APPROVED`, then `revision.pr_state != "merged"`) and `_withdraw_reasons(feature, revision, actor)` (status `IN_REVIEW`, actor is `feature.design_owner_id`). Distinguish actor reasons for 403 by extending `_ACTOR_REASONS` with `NOT_OWNER_REASON`.
- [ ] **Step 4: Implement the effects in `apply_transition`** as two new `elif` branches following the existing pattern (status change, clear approval fields, `first_publication` feature status, `history.record_event`), and raise `NotOwnerError` (new class) when the actor reason is `NOT_OWNER_REASON`. Map it in `_translate` in `app/api/v1/test_lifecycle.py`.
- [ ] **Step 5: Run** `uv run pytest tests/test_tl_workflow.py -q` — Expected: all PASS.
- [ ] **Step 6: Run** `uv run pytest tests -q -k "tl_"` and `uv run ruff check app/services/test_lifecycle/workflow.py app/schemas/test_lifecycle.py app/api/v1/test_lifecycle.py tests/test_tl_workflow.py` — Expected: only the 2 known pre-existing failures in `tests/test_tl_jira_tasks.py` (`test_create_scope_epic_payload_and_saves_key`, `test_scope_epic_reuses_existing_by_summary`); no new ruff errors in these files.

### Task 2: OpenAPI and local end-to-end check

**Files:**
- Modify: `openapi/qa-hub-be.json` (generated)

- [ ] **Step 1: Regenerate** with `uv run --extra dev python -m scripts.generate_openapi --apim-compatible`; `git diff openapi/qa-hub-be.json` shows only the two new enum values and the 403 text.
- [ ] **Step 2: Run** `uv run pytest -q -k "openapi or tl_"` — Expected: same result as Task 1 Step 6.
- [ ] **Step 3: Summarise for the PR** (title, description with tests, the merge risk and the unchanged sync). No commit.

## Part B — Frontend

### Task 3: Types, labels, permissions and header buttons

**Files:**
- Modify: `src/types/test-lifecycle.ts` (`TlTransitionAction`)
- Modify: `src/lib/test-lifecycle.ts` (`TRANSITION_LABEL`)
- Modify: `src/components/test-lifecycle/FeatureHeader.tsx` (`TRANSITION_PERMISSION`, visibility rule, confirmation dialog)
- Test: `src/components/test-lifecycle/FeatureHeader.test.tsx`

**Interfaces:**
- Produces: `TlTransitionAction = "submit" | "approve" | "request_changes" | "reopen" | "withdraw_approval" | "withdraw"`; `TRANSITION_LABEL.withdraw_approval = "Riporta in review"`, `TRANSITION_LABEL.withdraw = "Ritira in bozza"`; permissions `review:tl_feature` for `withdraw_approval` and `edit:tl_feature` for `withdraw`.

- [ ] **Step 1: Write failing tests in `FeatureHeader.test.tsx`** (reuse the file's feature fixtures; add `withdraw_approval`/`withdraw` entries to the `transitions` list)
  - `shows 'Riporta in review' only while the revision is approved`: with `working_revision.status = "approved"` and the check allowed the button is enabled; with `draft` or `in_review` it is not rendered.
  - `shows 'Ritira in bozza' only while the revision is in review`: rendered and enabled for `in_review`; not rendered for `draft` and `approved`.
  - `exposes the reasons of a blocked withdraw_approval`: merged PR check not allowed → button disabled with the reason in `aria-describedby` text.
  - `asks for confirmation before withdrawing an approval`: clicking 'Riporta in review' opens a dialog mentioning that the PR stays open (text contains "resta aperta"); `onTransition` is not called until "Conferma" is clicked, then it is called with `"withdraw_approval"`.
  - `withdraws a submission without a note`: clicking 'Ritira in bozza' calls `onTransition("withdraw")` immediately (no dialog, no note).
- [ ] **Step 2: Run** `pnpm vitest run src/components/test-lifecycle/FeatureHeader.test.tsx` — Expected: new tests FAIL; existing PASS.
- [ ] **Step 3: Implement.** Extend the type and the `Record`s (`tsc` points at every place), add the visibility rule (`withdraw_approval` only for `approved`, `withdraw` only for `in_review`) in the `feature.transitions.map`, and a small confirmation dialog for `withdraw_approval` next to the existing note dialog.
- [ ] **Step 4: Run** `pnpm vitest run src/components/test-lifecycle` and `npx tsc --noEmit` — Expected: PASS, no type errors.

### Task 4: Open-PR warning

**Files:**
- Modify: `src/components/test-lifecycle/FeatureHeader.tsx`
- Test: `src/components/test-lifecycle/FeatureHeader.test.tsx`

**Interfaces:**
- Consumes: `feature.working_revision.pr_number`, `pr_state`, `status`.
- Produces: a `role="status"` notice shown when the working revision is `draft` or `in_review` and `pr_state === "open"`, with the text "La PR #{pr_number} è ancora aperta con la versione approvata in precedenza: non mergiarla finché la revisione non è di nuovo approvata".

- [ ] **Step 1: Write failing tests**
  - `warns when a withdrawn revision still has an open PR`: status `in_review`, `pr_state "open"`, `pr_number 7` → the notice with "#7" is visible; same for `draft`.
  - `does not warn for an ordinary draft`: `pr_state` null → no notice.
  - `does not warn when the PR is merged or the revision approved`: `pr_state "merged"`, or status `approved` with an open PR → no notice.
- [ ] **Step 2: Run** the file — Expected: new tests FAIL.
- [ ] **Step 3: Implement the notice** under the header row using the existing warning tokens (`--warning-soft` / `--warning-ink`).
- [ ] **Step 4: Run** `pnpm vitest run src/components/test-lifecycle/FeatureHeader.test.tsx` — Expected: PASS.

### Task 5: Guide diagram and text

**Files:**
- Modify: `src/components/test-lifecycle/LifecycleDiagram.tsx`
- Modify: `src/app/(portal)/test-lifecycle/guida/page.tsx`
- Test: `src/app/(portal)/test-lifecycle/guida/page.test.tsx`

**Interfaces:**
- Produces: two extra arrows in the diagram (Progettata → In review labelled "Riporta in review", In review → Bozza labelled "Ritira in bozza", next to the existing "Richiedi modifiche") and guide text explaining both actions and the open-PR note.

- [ ] **Step 1: Write failing tests in `guida/page.test.tsx`**: the page shows "Riporta in review" and "Ritira in bozza", and mentions that the PR resta aperta.
- [ ] **Step 2: Run** `pnpm vitest run "src/app/(portal)/test-lifecycle/guida"` — Expected: FAIL.
- [ ] **Step 3: Implement** the arrows in the SVG (keep the viewBox readable at the current width; do not overlap the "Riapri" arc) and the guide paragraph near the existing "Richiedi modifiche" text in section 4.
- [ ] **Step 4: Run** the guide tests — Expected: PASS.

### Task 6: Verification

- [ ] **Step 1: Frontend** `pnpm test`, `pnpm lint`, `npx tsc --noEmit` — Expected: all green, lint 0 errors.
- [ ] **Step 2: End to end against a local backend** (backend branch running): draft → submit → approve → "Riporta in review" (confirmation text, warning appears, PR untouched) → "Ritira in bozza" → edit → submit → approve again (same PR updated, one Jira task); check the buttons per role (reviewer, owner, other). Record what could not be checked.
- [ ] **Step 3: Summarise for the PR** (title, description with the merge risk and the follow-up "make the sync safe"). No commit.
