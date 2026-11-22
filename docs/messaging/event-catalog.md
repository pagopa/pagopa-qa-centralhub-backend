# QA Messaging Hub: event catalog and routing rules

Source of truth in code: `app/messaging/catalog.py` (types, policies, templates) and
`app/messaging/rules.py` (routing). Design: `docs/superpowers/specs/2026-11-22-qa-messaging-hub-design.md`.

Every event is a CloudEvents 1.0 JSON object. The hub's extension attributes are `severity`
(required: `info`, `warning`, `critical`), `product` and `environment` (`dev`, `uat`, `prod`).
Event types are named `it.pagopa.qa.<domain>.<entity>.<verb>`.

## Deduplication policies

| Policy | Meaning |
|---|---|
| `occurrence` | A distinct fact identified by its event `id` (deterministic where the source has a natural key). The same `id` is never accepted twice. |
| `condition` | The current state of something that persists, reported by periodic scans. The hub keeps the members already reported and accepts an event only if it adds a member that was not in the last reported set. When the condition is empty the scanner clears it, so the next occurrence is announced again. |

## Event types

| Type | Policy | Producer | Severity | `data` | Message link |
|---|---|---|---|---|---|
| `it.pagopa.qa.quality_gate.run.completed` | `occurrence` (id = UUIDv5 of the run id) | Backend, when `POST /api/v1/test-metrics/test-runs` stores a run with `test_origin = QG` | `warning` when `failed + broken + skipped > 0`, otherwise `info` | `run_id`, `test_object`, `test_type`, `environment`, `scenario_qty`, `passed`, `failed`, `broken`, `skipped` | `/quality-gate/report/{run_id}` |
| `it.pagopa.qa.nrt.reviews.pending` | `condition`, key `nrt.reviews.pending`, members = run ids | NRT scanner (every `MESSAGING_SCAN_INTERVAL_S`) | `info` | `count`, `run_ids` (full list) | `/nrts` |
| `it.pagopa.qa.sanp_health.checks.failing` | `condition`, key `sanp_health.checks.failing`, members = `<environment>:<spec_name>` | SANP Health scanner | `warning` | `ko`, `warning`, `items` (full list) | `/sanp-health` |

The NRT set is the one the NRT list shows: `revision_result = PENDING`, origin neither `TAS` nor `QG`,
finished within the list window (`NRT_LIST_WINDOW_DAYS`). The SANP members omit the status, so a check
flipping between `WARNING` and `KO` does not notify again.

The status page sync failure is intentionally **not** published in phase 1, to avoid flooding inboxes.

## Routing rules (inbox consumer)

| Rule id | Match | Audience |
|---|---|---|
| `qg-completed` | Quality Gate run completed, any severity | `qa_analyst`, `qa_engineer`, `qa_manager` |
| `nrt-pending-review` | NRT reviews pending | `qa_analyst`, `qa_engineer`, `qa_manager` |
| `sanp-failing` | SANP Health checks failing | `qa_analyst`, `qa_engineer`, `qa_manager` |

## Publishing from outside the backend

`POST /api/v1/messaging/events` with the header `X-Hub-Ingest-Key` and a CloudEvent body:

```bash
curl -X POST "$HUB/api/v1/messaging/events" \
  -H "Content-Type: application/json" -H "X-Hub-Ingest-Key: $MESSAGING_INGEST_KEY" \
  -d '{"specversion":"1.0","id":"'"$(uuidgen | tr A-Z a-z)"'","source":"/example","severity":"info",
       "type":"it.pagopa.qa.nrt.reviews.pending","data":{"count":1,"run_ids":["r-1"]}}'
```

`202` accepted, `200` duplicate id or suppressed condition, `401` wrong key, `422` invalid envelope, unknown
type or `data` that does not match the catalog, `503` when `MESSAGING_INGEST_KEY` is not set.
