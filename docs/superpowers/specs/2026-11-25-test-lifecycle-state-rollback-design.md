# Test Lifecycle: going back from "Progettata" and "In review"

Date: 2026-11-25
Repos: `pagopa-qa-centralhub-backend` (workflow service and API) and `pagopa-qa-centralhub-frontend`
(header buttons, copy, state diagram). No database migration, no new dependency.
Size: small project (two PRs, backend first).

## Intent and why

Today a revision only moves forward. The reviewer can send an "In review" revision back to "Bozza" with
"Richiedi modifiche" (a note is mandatory), but the author cannot withdraw their own submission, and once
the reviewer approves ("Progettata") there is no way back: the PR is already open and the Jira
implementation task exists. When a mistake is noticed right after submitting or approving, the only options
are to wait for the merge and use "Riapri", or to work around the tool.

## Goals

1. A feature whose revision is "Progettata" (approved, PR not merged yet) can go back to "In review".
2. A revision "In review" can go back to "Bozza" by the design owner, without a note
   (the reviewer's "Richiedi modifiche" with a note stays as it is).
3. Every step is recorded in the history, like the other transitions.
4. The UI shows the two actions only to the people allowed to use them, with a clear explanation of what
   happens to the open PR.

## Non-goals

Closing or deleting the GitHub PR or branch (the Git client has no close operation); removing the Jira
implementation task; undoing a merged revision (that stays "Riapri"); changing validation rules,
reviewer rules or the optimistic-locking protocol; any change to the sync job.

## Anti-goals

Losing the history snapshot created at approval; letting a merged revision be moved back; letting someone
who is not the reviewer withdraw an approval, or someone who is not the design owner withdraw a submission;
a second Jira task or a second PR created by the new approval.

## Decisions

Confirmed by the person:
- The open PR is left open when an approval is withdrawn.
- The merge risk that follows is handled with a warning in the UI, not with a change to the sync.

My calls (to confirm when reading):
- Two new transition actions, appended after the existing four so the order of the current ones does not change:
  - `withdraw_approval` ("Riporta in review"): revision `approved` to `in_review`.
  - `withdraw` ("Ritira in bozza"): revision `in_review` to `draft`.
- Who can use them (backend rule, same style as the reviewer check): `withdraw_approval` only the assigned
  reviewer (403 otherwise, reason `NOT_REVIEWER_REASON`); `withdraw` only the design owner (403 otherwise,
  new reason "Solo l'owner di progettazione può eseguire questa azione"). Frontend permissions:
  `review:tl_feature` and `edit:tl_feature`, as for approve and submit.
- Guards: `withdraw_approval` is refused when the revision is not `approved` or its PR is `merged`
  (reason "La PR è già stata mergiata: usa Riapri"); `withdraw` is refused when the revision is not `in_review`.
- Effects of `withdraw_approval`: revision status `in_review`, `approved_by` and `approved_at` cleared,
  feature status back to `in_review` only on first publication (same rule as the other transitions),
  history event `tl.revision.approval_withdrawn`. The snapshot version created at approval is kept (history is
  append-only); a new one is created at the next approval. The PR fields (`pr_number`, `pr_state`, `pr_branch`)
  and the Jira keys are untouched.
- Effects of `withdraw`: revision status `draft`, feature status back to `draft` only on first publication,
  history event `tl.revision.withdrawn` with no note.
- Re-approval after a withdrawal reuses the existing PR: `publish_revision(..., allow_open=True)` already
  rewrites the file on the same branch, and the Jira task is not created again (`jira_impl_key` is checked).
- Both actions appear in `transitions` after the existing four; the UI maps them by `action`, so nothing
  depends on the list length.
- The state diagram in the feature page (guide) gets the two backward arrows, with the open-PR note.
- The two new buttons are shown only when relevant (`withdraw_approval` while the revision is `approved`,
  `withdraw` while it is `in_review`), unlike the existing four which are always listed; "Riporta in review"
  asks for a confirmation that explains what happens to the PR.

## Known risk and how it is handled

While a revision is back in "In review" or "Bozza" its PR is still open on GitHub. If someone merges that PR,
the sync marks the revision `merged` and imports the merged file, whatever the revision status is
(`mark_revision_merged` only checks `pr_state == "open"`). The merged content is the text approved earlier,
and any edit made in the hub after the withdrawal is overwritten, and the working revision is cleared.

Decision (confirmed by the person): accept the risk and warn in the UI. A working revision that is `draft` or
`in_review` and still has `pr_state == "open"` (this only happens after an approval was withdrawn, because the
PR is opened at approval) shows a warning in the feature header: "La PR #n è ancora aperta con la versione
approvata in precedenza: non mergiarla finché la revisione non è di nuovo approvata". The PR description
records the risk. The sync job does not change. Making the sync safe (keeping the hub draft as a new revision
when a withdrawn revision is merged) is a possible follow-up, not part of this work.

## Approaches considered

A. Two new transition actions in the existing transitions endpoint (chosen): fits the current API, pre-check
   (`transitions` with `allowed` and reasons), history events and tests.
B. Generic "set status" endpoint: flexible but bypasses the guards and the history conventions; rejected.
C. Reuse `request_changes` for "In review to Bozza": cannot be used by the author and demands a note;
   rejected.

## Constraints

Backend: `uv run pytest` (needs the local Postgres), ruff on the touched files (the repo has a large
pre-existing baseline, so no new errors), OpenAPI regenerated with
`uv run --extra dev python -m scripts.generate_openapi --apim-compatible`. Frontend: pnpm, `pnpm test`,
`pnpm lint`, `npx tsc --noEmit`, Italian UI copy, hub design tokens. Read `node_modules/next/dist/docs/`
before Next code (frontend AGENTS.md). Code, docs, commits and PR text in English. No commits or PRs by the
assistant.

## Left to the builder

Exact reason strings beyond those above, file/test layout, the confirmation dialog wording, and where the
open-PR warning sits in the header.

## Delivery

Backend branch cut from an up-to-date `main`, then the frontend branch, both merged in that order (the frontend
needs the new actions in the OpenAPI types). Check the flow end to end against a local backend before opening
the frontend PR.
