# Run detail: flakiness indicator and readability

Date: 2026-11-15
Repos: `pagopa-qa-centralhub-backend`, `pagopa-qa-centralhub-frontend`
Status: approved (mockup "B")
Builds on: `2026-11-14-flaky-scenarios-management-design.md`

## Intent

Reviewers analysing an NRT, TAS or Quality Gate run see, next to every non-passed scenario,
the flakiness index of the "Scenari flaky" section, so they can tell a flaky failure from a
regression without leaving the run. The detail panel is also made easier to read.

## Current state

- NRT, TAS and QG lists render the same `NrtDetailsPanel` (via `NrtsTable`).
- Every label and value is 12px; labels use `--text-muted` (about 2.6:1 contrast on the panel
  background).
- `GET /api/v1/flaky/scenarios` filters by `test_object`, not by scenario.

## Decisions

| Topic | Decision |
|---|---|
| Data | `GET /api/v1/flaky/scenarios` gains a repeatable `scenario_name` filter (exact match). The panel asks for the run's `env`, `test_object` and the names of its non-passed scenarios, `page_size=200`. The scenario's `test_type` comes from the suite. |
| Indicator | New column "Flakiness <env>" with the 5-segment `FlakinessLevel` (or "Rotto" / "Dati insuff." chips; "Dati insuff." also when the scenario has no score). Under it, "Da marcare" when the row hint is `to_mark` and the scenario is not flagged in the panel. Tooltip: score, flip rate, pass on retry, runs analysed. Visible for NRT, TAS and QG. |
| Summary | Above the table, chips counting the non-passed scenarios: "N da marcare flaky" (level ≥ 4, not flagged), "N già marcati" (flagged in the panel), "N stabili: probabile regressione" (state OK with level ≤ 2, or BROKEN, not flagged). Chips with 0 are hidden. |
| Layout | Three cards: Suite (test object, tipo, versione test object, team), Run (ambiente chip, versione test suite, inizio, fine, durata), Revisione (revisore, ticket prodotto, ticket QA — tickets only where they exist today). QG keeps its fields (ambiente, versione test suite, revisore, GitHub Actions run) in the Run and Revisione cards. |
| Typography | Values 14px medium; labels 11.5–12px in `--text-dim`; section titles 11px uppercase tracking in `--text-dim`; dates, versions and durations in mono; table text 13.5px, headers 11px uppercase. |
| Table | Status as coloured badge (FAILED red, BROKEN amber, others neutral); error in mono, clamped to 2 lines with the full text in `title`; duration in seconds with one decimal (`4,1 s`); flaky flag as a switch (NRT only, as today); note textarea 13px. |

Non-goals: changing which scenarios are listed, editing flags from TAS/QG, changing the list rows.
