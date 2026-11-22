# QA Messaging Hub

Date: 2026-11-22
Repos: `pagopa-qa-centralhub-backend` (core), `pagopa-qa-centralhub-frontend` (bell and inbox),
`pagopa-platform-integration-test` (optional producer, later phase)
Status: approved by the owner on 2026-11-22 with the routing, deduplication, bell and page changes
included below; next step: implementation plan

## 1. Intent

QA events are scattered today. A Quality Gate run finishes and only someone polling the Hub notices;
the status page detects a problem and shows it only on its own page; application alerting lives in
other tools. There is no single place where "something happened that a QA person should know" is
recorded, routed to the right people and acted on.

The QA Messaging Hub is that place: a small, event-driven, extensible messaging backbone owned by the
QA Hub. Producers deposit **events** describing *what happened*. The hub decides, with routing rules,
*who must be told and how*. Independent **consumers** act on the events: the first one shows them in
the Hub frontend (a bell with an unread counter, filtered by the user's role); later ones can open a
Jira ticket or post on Slack.

First use case: let the frontend show messages and alerts to users. The design must let the scope grow
without redoing the foundations.

## 2. Goals, non-goals, anti-goals

Goals
- A producer-facing contract that is a standard: **CloudEvents 1.0** (JSON), published through a single
  `publish()` API in the backend and a single HTTP ingest endpoint for external producers.
- Producers say what happened (type, severity, product, data). They never name an audience.
- Routing rules live **in code** (a reviewed registry), mapping event types and filters to consumers
  and, for the inbox, to the roles that see the message.
- Durable events, with per-consumer delivery state, retry with backoff and a dead state.
- Phase 1 consumer: **inbox**, per-user read/unread state, exposed by API and shown in the frontend
  bell and in a dedicated, polished "Messaggi" page.
- **No repeated messages for the same condition.** A condition that persists across scans (for example
  "there are NRT runs pending review") is reported once; a new message is sent only when something new
  is added to the condition. This holds for every event type by construction (section 5.4b).
- The bell shows three clearly different states: no messages at all, messages that are all read, and
  messages with some unread (with the unread counter).
- Messages carry a link to the section that needs attention when there is one.
- Consumers are independent: one failing (for example Slack down) never blocks or delays the others.
- Near-zero cost and no new Azure resource at the start.
- **The transport is replaceable.** The design is built so that Azure Service Bus can replace the
  Postgres-based transport later without touching producers, rules or consumer handlers (section 8).

Non-goals
- A UI to edit routing rules (rules stay in code; a UI is a possible later step).
- Jira, Slack and email consumers (the design makes them easy to add; they are not built in phase 1).
- Adapters for Azure Monitor, Application Insights or other alert sources (future; they would be
  small translators into CloudEvents posted to the ingest endpoint).
- Real-time push to the browser (phase 1 polls).
- Guaranteed exactly-once or ordered delivery (see section 5.7: at-least-once, no ordering).
- Replacing the Postgres transport with Service Bus now.

Anti-goals (a failure even if it technically works)
- An event is lost because the business transaction committed and the publish did not (or the
  reverse).
- The same message appears twice to a user, or a consumer acts twice, after a retry.
- A periodic scan that keeps finding the same pending items fills the user's inbox with identical
  messages.
- A producer needs to know roles or channels.
- Code outside the messaging module reads the hub's tables directly, which would make the transport
  impossible to swap.
- A slow or failing consumer stalls the status page sync, the test-run ingestion or the other
  consumers.

## 3. Current state (verified in code and repo)

- Backend: FastAPI, Postgres (SQLAlchemy, Alembic, last migration `0035`), Redis, Celery worker and
  beat (`app/tasks/schedule.py`), deployed with Helm behind Azure APIM. New task modules must be
  imported in `app/tasks/__init__.py` or the worker never registers them. Advisory locks 670, 671,
  672 are taken by flaky and status page tasks.
- Users and roles: `users.role` references `roles.key` (`qa_manager`, `qa_analyst`, `qa_engineer`,
  `guest`); the permission matrix is `ACTION_CATALOG` in `app/core/permissions.py`.
- **No server-side identity yet.** `verify_jwt` raises `NotImplementedError`; most endpoints have no
  authentication and the frontend sends a Bearer token that the backend does not check. A per-user
  inbox needs an identity (see 5.6 and open question 1).
- `app/models/notification_rule.py` defines a `NotificationRule` model that has no migration and is not
  used anywhere; the page `settings/notifications` in the frontend is an empty placeholder with a TODO.
- Quality Gate: the backend launches the TAS workflow and reads the live state from GitHub Actions.
  When the run ends, results arrive through `POST /api/v1/test-metrics/test-runs`, called by the TAS
  ingestion (`populate_tables.py`); test runs carry `test_origin` (`TAS`, `NRT`, `QG`).
- Status page: a Celery sync every 900 s records deploy events and sync status
  (`app/services/status_page_sync.py`).
- All API changes in this backend are additive so that frontend and backend can deploy in any order;
  after schema changes the OpenAPI is regenerated for APIM re-import.

## 4. Constraints

- Azure only; low cost; very low volume (tens of events per day at the start).
- No new infrastructure in phase 1: Postgres, Redis and the Celery worker and beat already exist.
- Everything in the repository (code, comments, docs, commit messages) in English; user-facing UI text
  follows the existing frontend convention (Italian).
- API changes additive; new Alembic migration after `0035`.
- Follow existing conventions: Celery task modules, advisory-lock or row-lock discipline, audit log for
  administrative actions, tests that hit the local Postgres (`tests/_db.py`).

## 5. Decided design

### 5.1 Approaches considered

| | Approach | Cost | Trade-off |
|---|---|---|---|
| A | **Postgres log + Celery, behind a transport seam** | ~0, nothing new | Not a real bus; retry and dead state are our code; scales to this volume, not to high throughput |
| B | Azure Service Bus topic and subscriptions | order of ten euro per month (Standard, needed for topics; verify current price) | New resource, secrets and deploy; native retry, dead-letter, AMQP standard; oversized for now |
| C | Azure Event Grid | near zero | Push delivery to public HTTP endpoints; per-user state still needs Postgres; consumers must be exposed |

**Chosen: A, with a transport seam.** Almost every initial producer already reaches the backend over
HTTP or lives inside it, and the first consumer is a Postgres table, so a managed bus would add cost
and moving parts without adding capability today. The seam keeps B open at low cost: section 8.

### 5.2 Architecture overview

```
 producers                         messaging module (app/messaging)                    consumers
 ---------                         ---------------------------------                   ---------
 backend code ──publish()──┐
 (QG ingest, scanners)     │   validate envelope ─► evaluate rules (code) ─► persist
 TAS / external ──HTTP─────┘   (CloudEvents)         rules.py                 msg_events
   POST /messaging/events                                                      msg_deliveries (1 per consumer)
                                                                                    │
                                                              dispatcher (Celery) ──┤  claims deliveries
                                                                                    ▼
                                                                         handler(event, ctx)
                                                                           ├─ inbox  ─► msg_inbox ─► API ─► frontend bell
                                                                           ├─ jira   (future)
                                                                           └─ slack  (future)
```

The module is `app/messaging/`. Nothing outside it touches the `msg_*` tables; producers use `publish()`
and the ingest route, the frontend uses the inbox routes.

### 5.3 Event envelope (the contract)

CloudEvents 1.0, JSON structured mode.

| Attribute | Rule |
|---|---|
| `specversion` | `"1.0"` |
| `id` | unique per event; producer-supplied or generated by the hub (UUID). Also the idempotency key |
| `source` | URI-reference of the producer, for example `/qa-hub/status-page`, `/tas/quality-gate` |
| `type` | reverse-DNS style, `it.pagopa.qa.<domain>.<entity>.<verb>`, for example `it.pagopa.qa.quality_gate.run.completed` |
| `subject` | the entity concerned, for example the run id or application key |
| `time` | RFC 3339; defaults to receipt time |
| `datacontenttype` | `application/json` |
| `data` | JSON object with the event facts; free-form per type but documented in the event catalog |
| extension `severity` | `info` \| `warning` \| `critical` (required; the hub rejects events without it) |
| extension `product` | optional product key, aligned with `sp_products` keys, used by rule filters |
| extension `environment` | optional `dev` \| `uat` \| `prod` |

An **event catalog** (`docs/messaging/event-catalog.md`) lists each event type, its producer, severity
rule and the shape of `data`. Unknown types are accepted by the ingest route only if a catalog entry
exists in code (a `EVENT_TYPES` registry with a pydantic schema for `data`); this keeps typos from
silently creating dead events.

### 5.4 Routing rules (in code)

`app/messaging/rules.py` holds an explicit list of rules, validated at import time (unknown consumer or
role raises at startup, in tests too).

```python
Rule(
    id="qg-failed-to-qa",
    match=Match(type="it.pagopa.qa.quality_gate.run.completed", min_severity="warning"),
    deliveries=[Delivery(consumer="inbox", params={"roles": ["qa_manager", "qa_engineer"]})],
)
```

- `Match` supports exact or glob `type`, `min_severity`, optional `product` and `environment` lists, and
  an optional predicate on `data` for cases a declarative filter cannot express.
- An event may match several rules; each produces a delivery for its (consumer, params). Each consumer
  declares a `merge(params_a, params_b)` hook. Deliveries of the same consumer whose params the hook can
  merge become one delivery: the inbox merges by **union of roles**, so a manager named by two rules
  gets one message, rendered once. Consumers without a hook keep distinct params as separate deliveries
  (for example a future Jira consumer posting to two projects). Identical params always collapse to one.
  `params_hash` is the SHA-256 of the canonical JSON of the params (sorted keys, sorted role lists).
- **Rules are evaluated once, at acceptance time**, and the result is materialised as delivery rows. A
  consumer never re-evaluates rules; it receives its own `params`. This is what keeps rules independent
  from the transport (section 8).
- An event that matches no rule is still stored, with zero deliveries (visible for audit, no effect).

### 5.4b Deduplication (all event types)

Every event type in the catalog **must declare** one of two policies explicitly (startup validation
fails otherwise, so the guarantee holds for all rules by construction). Deduplication happens at
acceptance, before routing, so it does not depend on the transport.

- **`occurrence`**: each event is a distinct fact, identified by its `id` (deterministic where the
  source has a natural key). Example: a Quality Gate run completed. The same `id` is never accepted
  twice.
- **`condition`**: the event describes the *current state of something that persists* and is published
  by periodic scans. The type declares `key(event)` (the identity of the condition) and
  `members(event)` (the set of items currently in it, as strings). The hub keeps the last reported
  members per key in `msg_dedupe` and applies this rule inside the publish transaction, under a row
  lock on the key:
  - `new_members = members(event) - stored_members`; if it is **not empty**, the event is accepted,
    routed as usual, and the stored members become the full current set;
  - if it is empty (same set, or the set only shrank) the event is **suppressed**: not stored, no
    deliveries, only the stored members are refreshed and a debug log line is written;
  - when a scan finds the condition empty the producer calls `clear_condition(session, key)`, which
    removes the stored state, so the next occurrence of the condition is announced again.

Consequences, using NRT as the example: 5 runs pending are announced once; the next scans with the same
or fewer pending runs stay silent; a 6th run turns pending and a new message ("6 runs pending") is
sent; when all are reviewed the condition is cleared and a later pending run is announced again.

**Superseding.** When a new message for a condition is delivered, the inbox handler automatically marks
the same user's older messages with the same dedupe key as read, so the unread counter reflects only
the latest state and the inbox does not fill with outdated counts. They stay visible in the history.

### 5.5 Storage (new migration after `0035`)

Names are prefixed `msg_` like the `sp_` and `flaky_` families.

- `msg_events`: `id uuid PK` (the CloudEvent id), `source`, `type`, `subject`, `time`, `severity`,
  `product`, `environment`, `data jsonb`, `received_at`. Unique on `id` (duplicate publish of the same
  id is a no-op and returns the stored event).
- `msg_deliveries`: `id uuid PK`, `event_id FK`, `consumer text`, `params jsonb`, `status`
  (`PENDING`, `DONE`, `DEAD`), `attempts int`, `next_attempt_at`, `last_error text`, `done_at`.
  Unique on `(event_id, consumer, params_hash)`.
- `msg_dedupe`: `key text PK`, `members jsonb` (sorted array of strings), `last_event_id`,
  `last_notified_at`, `updated_at`. Rows untouched for 90 days are purged with the retention task.
- `msg_inbox`: `id uuid PK`, `event_id FK`, `delivery_id FK`, `user_id FK users.id`, `title`, `body`,
  `link text null` (a frontend path such as `/nrts`, never an absolute URL), `severity`, `type`,
  `dedupe_key text null`, `created_at`, `read_at null`, `auto_read bool` (true when marked read by
  superseding). Unique on `(event_id, user_id)`.
  `title`, `body` and `link` are a **rendered snapshot** produced by the inbox handler at delivery
  time, so a later change to a template never rewrites history.
- Retention: a daily Celery task deletes events, deliveries and inbox rows older than 90 days
  (`MESSAGING_RETENTION_DAYS`); `DEAD` deliveries are kept for the same window.

Events are written in the **caller's transaction** when published from backend code (transactional
outbox): the business change and the event commit or roll back together.

### 5.6 Publishing

- In-process: `await publish(session, event)` adds the event, evaluates the rules and inserts the
  deliveries in the caller's session, with no commit of its own. The **caller commits**, then may call
  `wake_dispatcher()` (best effort, swallows errors). There is no SQLAlchemy event hook. If the wake-up
  is skipped or fails, the beat-driven dispatcher picks the deliveries up within 30 s, so nothing is
  lost. The same code path serves FastAPI routes and Celery tasks (they already run async sessions
  through `run_async_task`).
- External: `POST /api/v1/messaging/events` takes one CloudEvent (structured JSON) and returns 202 with
  the event id, 200 when the same id was already accepted, 422 for an invalid envelope, unknown type or
  invalid `data`. It is protected with a shared secret header `X-Hub-Ingest-Key`
  (`MESSAGING_INGEST_KEY`, 401 when wrong or missing, 503 when unset) and is meant to be exposed through
  APIM like the other routes. This is an interim guard consistent with the rest of the backend until
  `verify_jwt` exists; the secret is never logged.
- Every accepted event writes an audit-log row only for the HTTP route (`messaging.event.ingest`), not
  for in-process publishes, to keep the audit table free of noise.

### 5.7 Dispatcher and delivery semantics

- Celery task `app.messaging.dispatch` (module imported in `app/tasks/__init__.py`, async body run with
  `run_async_task` like the other tasks), run by beat every 30 s and woken after publish.
- **Claim, commit, run, record.** Handlers may call the network (Jira, Slack), so no row lock or
  transaction is held while a handler runs:
  1. In a short transaction the dispatcher selects due deliveries (`status = PENDING`,
     `next_attempt_at <= now`, and `lease_until IS NULL OR lease_until < now`) with
     `FOR UPDATE SKIP LOCKED` (batch of 50), sets `lease_until = now + 5 min` and commits. Several workers
     can run the dispatcher at once; no advisory lock is needed.
  2. Outside any transaction it resolves the handler by `consumer` name and calls
     `handler(event, DeliveryContext(params, delivery_id, attempt))` under `asyncio.wait_for` with a
     60 s timeout (always shorter than the lease).
  3. In a new short transaction it records the result: `DONE` (clears the lease), or on exception
     increments `attempts`, stores `last_error` (truncated), clears the lease and sets `next_attempt_at`
     with exponential backoff (1 min, 5 min, 30 min, 2 h, 6 h). After the sixth failure the status
     becomes `DEAD` and an error log line is emitted.
- **Crash recovery:** if a worker dies mid-handler nothing is recorded; the delivery becomes claimable
  again when its lease expires and is retried. This is one more reason handlers must be idempotent.
- `msg_deliveries` therefore has a `lease_until timestamptz null` column (no extra status value).
- Each handler call is isolated: one consumer's failure cannot roll back or delay another's work.
- **Delivery is at-least-once with no ordering guarantee.** Handlers must be idempotent. The inbox
  handler relies on the unique `(event_id, user_id)` constraint (insert with `ON CONFLICT DO NOTHING`).
  Future handlers use `event.id` as the idempotency key against the external system (for example a Jira
  label or a Slack message key) wherever the target allows it.
- Dead deliveries can be re-queued by an operator with a documented SQL statement or a small
  management script (a UI is out of scope).

### 5.8 Consumers and handlers

A consumer is a registered name plus an async handler:

```python
async def handle(event: CloudEvent, ctx: DeliveryContext) -> None: ...
```

Contract: pure with respect to the transport (it receives everything it needs in its arguments and does
not read the `msg_*` tables), idempotent, raises on failure and returns on success.

Phase 1 consumer, **inbox**:
1. Reads `ctx.params["roles"]`, selects active users whose role is in that list. If no active user
   matches, the delivery ends `DONE` with no inbox rows and a warning log line (it is not a failure; the
   event and its delivery stay visible for audit).
2. Renders `title`, `body` and optional `link` from a **message template registered per event type** in
   code (Italian UI text), using `event.data`.
3. Inserts one `msg_inbox` row per user (idempotent as above).

A user who gets the role after the event was delivered does not see the old message (fan-out on write).

### 5.9 Inbox API (additive)

Under `/api/v1/messaging`:

- `GET /inbox?unread_only=&severity=&type=&limit=&cursor=` – messages of the current user, newest
  first.
- `GET /inbox/summary` – `{unread, total, max_unread_severity}` for the bell: `total` counts the user's
  messages still within retention (so the bell can tell "no messages" from "all read"), and
  `max_unread_severity` is `info`, `warning`, `critical` or null.
- `POST /inbox/{id}/read` and `POST /inbox/read-all`.

Identity: a single dependency `get_inbox_user` returns the current user. **Until `verify_jwt` exists** its
interim implementation takes the user email from the `X-User-Email` header sent by the frontend,
matched case-insensitively against `users.email` (unique, not normalised). A missing header gives 401; an
unknown or inactive user gets an empty inbox and a zero count (200), so the bell stays quiet instead of
erroring. The current frontend client (`apiClient`) sends only the Bearer token, so the bell needs a small
frontend change: add the session email as `X-User-Email` on the inbox calls. It is as trustworthy as the
rest of the unauthenticated API today (message content is low sensitivity, no
write beyond the caller's own read flags). When server-side authentication lands, only this dependency
changes. This limitation is stated in the README section for the feature.

New permission `view:inbox` in `ACTION_CATALOG` (category "Generale"), default true for `qa_manager`,
`qa_analyst`, `qa_engineer`, false for `guest`; the frontend hides the bell without it. Role visibility
of individual messages comes from the rules, not from this permission.

### 5.10 Frontend

**Bell in `Topbar`** (between the theme toggle and the role badge), polling `inbox/summary` every 60 s
and when the tab regains focus. Three visually distinct states:

| State | Condition | Look |
|---|---|---|
| No messages | `total = 0` | Outline bell in the muted text colour, no decoration |
| All read | `total > 0`, `unread = 0` | Outline bell in the normal text colour with a small green check mark at its corner |
| Unread | `unread > 0` | Bell on a soft tinted circle with a counter badge ("9+" above nine); the tint and badge colour follow the most severe unread message (info = accent, warning = amber, critical = red) |

Accessible name and tooltip state the situation ("3 messaggi non letti", "Nessun messaggio", "Tutti i
messaggi sono stati letti"). Colours come from the existing tokens (`--accent`, `--warning`, `--danger`,
`--success` and their `-soft` variants) so light and dark themes both work.

**Quick panel** (click on the bell): the latest messages, each with severity icon, title, one-line body,
relative time and, when present, a link; "Segna tutto come letto"; a footer link "Vedi tutti i
messaggi" to the page below. Empty state with a friendly one-liner.

**Page "Messaggi"** (`/messages`, sidebar entry gated by `view:inbox`, with the unread count next to the
label). It is a designed section, not a plain list:
- header with the title and summary chips (unread, and counts per severity);
- segmented control "Tutti / Non letti" and severity filter chips;
- messages grouped by day ("Oggi", "Ieri", then the date);
- each message is a card: coloured severity rail and an icon in a soft circle, bold title with an unread
  dot, body, a type tag (Quality Gate, NRT, SANP Health), relative time (absolute on hover), a primary
  link button to the section to look at (for example "Apri report", "Vai agli NRT", "Apri SANP Health")
  and a quiet "Segna come letto" action;
- loading skeletons, a friendly empty state, responsive layout, same spacing, radii and shadows as the
  other pages.
Built from the existing primitives (`Chip`, `ui/button`, `ui/tabs`, `ui/tooltip`). A mockup is reviewed
with the owner before the frontend task starts.

The `settings/notifications` placeholder stays as the future home of rule management and is not
touched in phase 1.

### 5.11 Phase 1 producers and event catalog

1. `it.pagopa.qa.quality_gate.run.completed`: published **in-process** by the backend when
   `POST /test-metrics/test-runs` stores a run with `test_origin = QG`. That endpoint is a plain insert
   called once per run, and TAS already fills the run's aggregates (`scenario_qty`, `passed_scenario`,
   `failed_scenario`, `broken_scenario`, `skipped_scenario`, `total_failures`, `test_object`) before
   posting, so the totals are available without waiting for the scenario rows. No change in the TAS
   repository is needed to start. The event `id` is deterministic (UUIDv5 of the run id and event
   type), so a repeated ingestion of the same run cannot produce a second event. "Failed" means
   `failed + broken + skipped > 0` (the sum TAS calls `total_failures`; that column is not in the backend
   `TestRun` model, so the backend computes it). The test object and type come from the run's suite
   (`suite_id`). Severity `warning` when failed,
   `info` when the run passes. Policy `occurrence`. `data`: run id, test object, test type,
   environment, totals. Message link: the run report page, `/quality-gate/report/{run_id}`.
2. `it.pagopa.qa.nrt.reviews.pending`: runs waiting for review. Policy `condition`, key
   `nrt.reviews.pending`, members the pending run ids. Produced by a periodic **scan**
   (section 5.11b). The scan reuses the definition already used by the NRT list and by
   `get_pending_runs` (runs with `revision_result = PENDING`, origin neither TAS nor QG, finished within
   `NRT_LIST_WINDOW_DAYS`); the builder extracts that query into a shared function instead of copying
   it. A run that leaves the window counts as resolved. Severity `info`. `data`: count and the full list
   of run ids (never truncated, because the members for deduplication are read from it). Message link:
   `/nrts`.
3. `it.pagopa.qa.sanp_health.checks.failing`: SANP Health checks currently `KO` or `WARNING` in the
   latest completed import. Policy `condition`, key `sanp_health.checks.failing`, members
   `<environment>:<spec_name>` of each failing check (the status is deliberately not part of the member,
   so a check flipping between `WARNING` and `KO` does not re-notify; the message shows the current
   counts). Severity `warning` for both statuses. `data`: counts of `KO` and `WARNING` and the full list
   of failing items (never truncated, same reason). Message link: `/sanp-health`.
4. **Starting routing rules** (decided by the owner):

   | Event | Severity | Inbox audience |
   |---|---|---|
   | Quality Gate run completed, with failures | `warning` | `qa_manager`, `qa_analyst`, `qa_engineer` |
   | Quality Gate run completed, no failures | `info` | `qa_manager`, `qa_analyst`, `qa_engineer` |
   | NRT runs pending review | `info` | `qa_manager`, `qa_analyst`, `qa_engineer` |
   | SANP Health checks `KO` or `WARNING` | `warning` | `qa_manager`, `qa_analyst`, `qa_engineer` |

   The status page sync failure is **intentionally not published** in phase 1, to avoid flooding users'
   inboxes; it can be added later as a catalogued type with its own rule.
5. The ingest route exists from day one, so TAS or any external system can publish a catalogued event
   without backend changes.

### 5.11b Scanners (periodic condition producers)

A **scanner** is a small function that reads the current state from the database and either publishes a
`condition` event or clears the condition: `if items: publish(event) else: clear_condition(key)`. One
Celery task `app.messaging.scan` runs all registered scanners every `MESSAGING_SCAN_INTERVAL_S` (default
900 s) through beat. Each scanner runs in its own transaction and its exceptions are caught and logged,
so a broken scanner never stops the others, and a publish error never breaks the job that owns the data
(for example the SANP sync). Phase 1 scanners: NRT pending reviews, SANP Health failing checks. Adding a
source later means adding a scanner and a catalog entry.

### 5.12 Observability

Structured log lines for accepted event, matched rules, delivery attempts and dead transitions, with
`event_id` and `delivery_id`. A Grafana or SQL view on `msg_deliveries` (pending age, dead count) is
suggested in the README, not built here.

### 5.13 Housekeeping

Remove the unused `NotificationRule` model (no migration ever created its table) in this change, so there
is only one notification concept in the code base.

## 6. Testing

- Rules: table-driven tests on matching and validation (unknown consumer or role fails at startup).
- Publish: transactional behaviour (rollback removes event and deliveries), duplicate id is a no-op.
- Dispatcher: claim and `SKIP LOCKED` behaviour, backoff schedule, move to `DEAD`, failure isolation
  between two deliveries of the same event, redelivery of an already-`DONE` event is harmless.
- Inbox handler: idempotency, fan-out by role, inactive users skipped, template snapshot stored.
- Deduplication: every catalog type declares a policy (startup validation); `condition` scenarios
  table-driven: first report accepted, same set suppressed, shrunk set suppressed, one new member
  accepted, clear then reappear accepted again, concurrent publishes of the same key serialise on the
  row lock; superseding marks older same-key messages read for each user.
- Scanners: NRT pending (same result as `get_pending_runs`, TAS and QG excluded), SANP failing checks
  (latest completed import only, `KO` and `WARNING`), empty result clears the condition, a failing scanner
  does not stop the others.
- API: ingest (200/202/401/422/503), inbox list and filters, summary (`total` distinguishes "no messages"
  from "all read"), read, read-all, `view:inbox` behaviour.
- A **transport contract test** that runs the same handler suite through the Postgres transport; it is
  the test a future Service Bus transport must also pass.
- Frontend: the three bell states (no messages, all read, unread with counter and severity colour),
  quick panel, `/messages` page filters and grouping, links, mark read, hidden without permission.

## 7. Rollout

1. Migration, `app/messaging` module, rules, inbox consumer, API, OpenAPI regeneration for APIM
   re-import. Backend deploys first (additive).
2. Mockup review of the bell and the `/messages` page with the owner, then the frontend: bell, quick
   panel, page, sidebar entry.
3. Wire the producers (Quality Gate ingestion hook, NRT and SANP scanners with their beat entry). Update
   the backend README and `CLAUDE.md` with the durable rules (section 9).
4. Later, independently: TAS publishes through the ingest route, Jira and Slack consumers, Azure Monitor
   adapter, rule management UI, Service Bus transport.

## 8. Replacing Postgres with Azure Service Bus later

This section is part of the documentation of the solution: the architecture is built so that adopting
Service Bus is a small, local change, not a redesign.

**What is stable across the swap (and therefore protected by the design)**
- The producer contract: `publish()`, the ingest route and the CloudEvents envelope. Producers do not
  know the transport.
- Routing rules in code and their output (deliveries with `consumer` and `params`).
- Deduplication (`occurrence` and `condition` policies, `msg_dedupe`): it runs at acceptance, before
  anything reaches a transport, so a Service Bus transport inherits it unchanged.
- The scanners, which only call `publish()` and `clear_condition()`.
- Consumer handlers: `handle(event, ctx)` receives all it needs as arguments.
- The inbox table, API and frontend.

**What changes**
- The piece that moves a delivery from "accepted" to "handler called". Today: the Celery dispatcher reads
  `msg_deliveries`. With Service Bus:
  - the `msg_events` and `msg_deliveries` tables stay and become the **outbox**: events are still written
    in the business transaction, and a relay task publishes one Service Bus message per delivery to a
    topic, with application properties `consumer` and the delivery `params`, `event.id` as `MessageId`;
  - one subscription per consumer, with a trivial filter on the property `consumer`. The routing
    decision is **not** moved into subscription filters: it stays in `rules.py`, so there is one place
    for routing logic;
  - a receiver process per consumer calls the same handler; Service Bus provides lock, retry and
    dead-letter, so the custom backoff code is retired for that transport;
  - the relay marks the outbox row as published; delivery state in `msg_deliveries` can then be reduced
    to an audit trail.
- A consumer that lives outside the backend (for example an Azure Function for Slack) reads its
  subscription directly. The CloudEvents envelope is its contract; it re-implements the handler in its
  own runtime.

**Rules that keep this true (enforced in review)**
1. Only `app/messaging` reads or writes `msg_*` tables; everyone else uses `publish()`.
2. Handlers are idempotent and keyed by `event.id` (Service Bus is at-least-once too).
3. No logic depends on delivery order across events.
4. Handlers do not query delivery state; everything they need is in the event and `ctx`.
5. Routing stays in `rules.py`.
6. The transport sits behind an interface with two operations: *enqueue accepted deliveries* and *drain
   due deliveries to handlers*. The Postgres implementation is the reference and the contract test in
   section 6 defines what another implementation must satisfy.

**When to make the change**: a consumer or producer that cannot reach the backend, throughput well beyond
tens of events per hour, or a need for native fan-out and dead-letter operations that the custom
dispatcher handles poorly. None of these holds today.

## 9. Durable rules to record in `CLAUDE.md` once built

The six rules of section 8, plus: every event type declares `occurrence` or `condition` explicitly;
scanners call `clear_condition` when their condition is empty; rules are evaluated at acceptance time only; the inbox stores a
rendered snapshot; `view:inbox` gates the bell, rules gate the audience; new task modules must be
imported in `app/tasks/__init__.py`.

## 10. Left to the builder

- Exact module layout inside `app/messaging/`, class names, and the SQL for claim and backoff.
- The cursor format for the inbox list.
- Message template wording (Italian) for the two phase 1 event types.
- Batch size, poll interval and backoff steps are defaults and may be tuned.
- Test data helpers and how the transport contract test is parameterised.

## 11. Calls made while designing (to be checked)

These were decided without an explicit answer from the owner; each can be reversed.

- Fan-out on write for the inbox (new role holders do not see past messages).
- 90-day retention for events, deliveries and inbox rows.
- The frontend polls every 60 s; no SSE or WebSocket in phase 1.
- Shared-secret header for the ingest route as the interim guard.
- `X-User-Email` as the interim identity for inbox routes.
- Quality Gate completion is published in-process at test-run ingestion instead of by TAS.
- `FOR UPDATE SKIP LOCKED` with a lease column instead of an advisory lock for the dispatcher.
- The ingest key `MESSAGING_INGEST_KEY` is provisioned like the other secrets: a Key Vault secret
  referenced by name in `envSecret` of the Helm values for dev, uat and prod; APIM re-import follows the
  existing OpenAPI regeneration step.
- A failed QG run is `warning`, a passing one `info`; `critical` is reserved for external alerts.
- Deduplication is a property of the event type (accept or suppress before routing), not of each routing
  rule; "something new" means a member not present in the last reported set.
- When a condition message is superseded, the user's older messages with the same key are marked read
  automatically instead of being deleted.
- SANP members omit the status so `WARNING`/`KO` flapping does not re-notify.
- The scan interval is 15 minutes; the SANP scan reads the latest imported data and is independent from
  the hourly SANP sync.
- Links are frontend paths (`/nrts`, `/sanp-health`, `/quality-gate/report/{run_id}`), not absolute URLs,
  so no base-URL setting is needed.
- The `/messages` page is part of phase 1 (it was a follow-up before), with a mockup review first.
- The summary endpoint's `total` counts messages within the 90-day retention.
- `view:inbox` is false for `guest`.
- Removal of the unused `NotificationRule` model.
