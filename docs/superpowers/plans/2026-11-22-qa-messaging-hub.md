# QA Messaging Hub (backend) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the backend of the QA Messaging Hub: CloudEvents events, code-defined routing, deduplication, a Postgres-backed transport behind a swappable seam, the inbox consumer and API, an ingest route, and the first producers (Quality Gate, NRT pending, SANP Health).

**Architecture:** New package `app/messaging/` (envelope, catalog, rules, consumers, bus, transport, dispatcher, inbox, scanners). Producers call `publish()` (or `clear_condition()`); events and per-consumer deliveries are written in the caller's transaction; a Celery task claims due deliveries with a lease and calls idempotent handlers. The inbox is the only consumer in phase 1. Nothing outside `app/messaging` touches the `msg_*` tables.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2 async + asyncpg, Alembic, Celery (beat), pydantic v2, pytest-asyncio (`asyncio_mode = auto`), ruff, mypy strict.

**Spec:** `docs/superpowers/specs/2026-11-22-qa-messaging-hub-design.md` (read it first; section numbers below refer to it). The frontend is a separate plan: `pagopa-qa-centralhub-frontend/docs/superpowers/plans/2026-11-22-qa-messaging-hub-frontend.md`. **The backend deploys first** (all changes are additive).

## Global Constraints

- Everything in the repository (code, comments, docs, logs, commit messages) is English; user-visible message text produced by templates is Italian.
- API changes are additive; new migration is `0036` (down_revision `"0035"`), written in the style of `0033`.
- `msg_*` tables live only behind `app/messaging`; other modules use `publish()`, `clear_condition()`, `emit_*` helpers and the inbox service.
- Roles are `qa_manager`, `qa_analyst`, `qa_engineer`, `guest` (`EDITABLE_ROLES`) plus `superadmin`; `rules.py` may only name roles from `EDITABLE_ROLES`.
- Severity values: `info`, `warning`, `critical`. Event type ids are `it.pagopa.qa.<domain>.<entity>.<verb>`.
- Handlers are idempotent and keyed by `event.id`; nothing relies on delivery order.
- Backoff steps (seconds): `60, 300, 1800, 7200, 21600`; `MAX_ATTEMPTS = 6`; lease 300 s; handler timeout 60 s; dispatcher batch 50; retention 90 days; scan interval 900 s; dispatch interval 30 s.
- Do not modify Helm or other infra without the owner's explicit confirmation (Task 13 only proposes the change).
- Do not commit or push without the owner's explicit confirmation; each task ends with a proposed commit that the executor shows and asks about.
- Before each commit run `uv run ruff check .`, `uv run mypy .` and the relevant `uv run pytest` selection; report results.
- Tests that touch the database use the local Postgres through `tests/_db.py` (`TestSession`), like `tests/test_flaky_*.py`.

## Review Focus

- A producer publishes while the business transaction rolls back: no event or delivery may remain (Task 4).
- A scan publishes the same pending set every 15 minutes: exactly one message is delivered in total (Task 4, Task 10).
- A set shrinks and later regrows with an item seen before: that item is announced again (Task 4).
- A worker dies mid-handler: the delivery is retried after the lease expires, and the inbox shows one message (Tasks 5, 6).
- A user with an unknown or inactive account, or no `X-User-Email`: bell shows empty or 401, never a 500 (Task 7).
- A failing publish inside the Quality Gate ingestion must not make `POST /test-runs` fail (Task 9).
- A rule naming a role or consumer that does not exist fails at startup, not at 3 a.m. (Task 3).

## File Structure

```
app/messaging/
  __init__.py          # public API re-exports: publish, clear_condition, wake_dispatcher, CloudEvent
  envelope.py          # CloudEvent model, new_event, deterministic_id
  catalog.py           # EventType registry (policy, data models, key/members, render)
  rules.py             # Match, DeliverySpec, Rule, RULES, route(), validate_registry()
  consumers.py         # Consumer registry, DeliveryContext, Handler type
  bus.py               # publish(), clear_condition(), PublishResult, wake_dispatcher()
  transport.py         # Transport protocol, PostgresTransport (enqueue, drain_due)
  dispatcher.py        # claim_due, record_success, record_failure, backoff constants
  inbox.py             # inbox consumer handler
  inbox_service.py     # list_inbox, inbox_summary, mark_read, mark_all_read
  producers/quality_gate.py   # build/emit Quality Gate completed
  scanners/__init__.py        # Scanner registry + run_all()
  scanners/nrt_pending.py
  scanners/sanp_failing.py
app/models/messaging.py        # MsgEvent, MsgDelivery, MsgInbox, MsgDedupe
app/schemas/messaging.py       # API schemas
app/api/v1/messaging.py        # routes + identity and ingest-key dependencies
app/tasks/messaging.py         # dispatch_messages, scan_conditions, purge_messages
alembic/versions/0036_messaging_hub.py
docs/messaging/event-catalog.md
tests/messaging/               # one test module per task (names given below)
```

Delete `app/models/notification_rule.py` and its two references in `app/models/__init__.py` (Task 2). Leave the empty `app/api/v1/notifications.py` stub alone.

---

### Task 1: Settings, envelope and event catalog

**Files:**
- Modify: `app/config.py` (new settings)
- Create: `app/messaging/__init__.py`, `app/messaging/envelope.py`, `app/messaging/catalog.py`
- Test: `tests/messaging/test_envelope.py`, `tests/messaging/test_catalog.py` (create `tests/messaging/__init__.py`)

**Interfaces:**
- Produces:
  - Settings: `messaging_ingest_key: str = ""`, `messaging_retention_days: int = 90`, `messaging_scan_interval_s: float = 900.0`, `messaging_dispatch_interval_s: float = 30.0`.
  - `Severity = Literal["info","warning","critical"]`, `SEVERITY_ORDER: dict[str,int] = {"info":0,"warning":1,"critical":2}`.
  - `class CloudEvent(BaseModel)`: `specversion: Literal["1.0"]="1.0"`, `id: str` (UUID string), `source: str`, `type: str`, `subject: str | None`, `time: datetime` (tz-aware, default now UTC), `datacontenttype: Literal["application/json"]="application/json"`, `data: dict[str, Any]`, `severity: Severity`, `product: str | None`, `environment: Literal["dev","uat","prod"] | None`. Extra attributes are rejected.
  - `new_event(type: str, source: str, severity: Severity, data: dict[str, Any], *, subject: str | None = None, id: str | None = None, product: str | None = None, environment: str | None = None) -> CloudEvent`
  - `deterministic_id(event_type: str, key: str) -> str` (UUIDv5 over a fixed project namespace, lower-case string).
  - Catalog: `class RenderedMessage(NamedTuple): title: str; body: str; link: str | None`; `@dataclass(frozen=True) class EventType: type: str; policy: Literal["occurrence","condition"]; data_model: type[BaseModel]; render: Callable[[CloudEvent], RenderedMessage]; key: Callable[[CloudEvent], str] | None = None; members: Callable[[CloudEvent], frozenset[str]] | None = None`; `EVENT_TYPES: dict[str, EventType]`; `get_event_type(type: str) -> EventType` raising `UnknownEventType(LookupError)`; constants `QG_RUN_COMPLETED`, `NRT_REVIEWS_PENDING`, `SANP_CHECKS_FAILING` (the three type ids of section 5.11); `validate_catalog() -> None` (policy `condition` requires `key` and `members`; `occurrence` forbids both).
  - Data models (pydantic): `QgRunData(run_id: str, test_object: str, test_type: str, environment: str | None, scenario_qty: int, passed: int, failed: int, broken: int, skipped: int)`, `NrtPendingData(count: int, run_ids: list[str])`, `SanpFailingData(ko: int, warning: int, items: list[str])`.

- [ ] **Step 1: Write failing tests**
  - `test_new_event_defaults`: `new_event(QG_RUN_COMPLETED, "/qa-hub/quality-gate", "info", {...})` has a UUID `id`, `specversion == "1.0"`, tz-aware `time`.
  - `test_cloudevent_rejects_unknown_severity_and_extra_fields` (`pytest.raises(ValidationError)`).
  - `test_deterministic_id_is_stable`: same inputs give equal ids; different key gives a different id.
  - `test_every_event_type_declares_policy`: `validate_catalog()` passes; a hand-built `EventType(policy="condition", key=None, ...)` makes it raise `ValueError`.
  - `test_unknown_type_raises`: `get_event_type("nope")` raises `UnknownEventType`.
  - Render tests with exact strings (the Italian copy the owner approved):
    - QG failed (`scenario_qty=148, failed=8, broken=3, skipped=1`, test_object `pagopa-fdr`, type `integration`): title `"Quality Gate non superato · pagopa-fdr / integration"`, body `"12 scenari su 148 non superati: 8 failed, 3 broken, 1 skipped."`, link `"/quality-gate/report/<run_id>"`.
    - QG passed (`scenario_qty=86`, no failures): title `"Quality Gate superato · pagopa-gpd / e2e"`, body `"86 scenari, tutti passati."`.
    - NRT with 6 ids: title `"6 run NRT in attesa di analisi"`, link `"/nrts"`; with 1 id: `"1 run NRT in attesa di analisi"`.
    - SANP `ko=2, warning=3`: title `"SANP Health: 2 controlli KO e 3 in warning"`; `ko=0, warning=3`: `"SANP Health: 3 controlli in warning"`; `ko=2, warning=0`: `"SANP Health: 2 controlli KO"`; link `"/sanp-health"`.
  - Condition helpers: NRT `key == "nrt.reviews.pending"`, `members == frozenset(run_ids)`; SANP `key == "sanp_health.checks.failing"`, `members == frozenset(items)`.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_envelope.py tests/messaging/test_catalog.py -v` and confirm they fail (import errors).
- [ ] **Step 3: Implement** the modules and settings above. The QG body for a passing run is the "tutti passati" form; severity is chosen by the producer, not by `render`.
- [ ] **Step 4: Run** the same command; expect PASS. Run `uv run mypy app/messaging`.
- [ ] **Step 5: Propose commit** `feat: add messaging envelope, settings and event catalog`.

---

### Task 2: Migration and ORM models

**Files:**
- Create: `alembic/versions/0036_messaging_hub.py`, `app/models/messaging.py`
- Modify: `app/models/__init__.py` (register new models; remove `NotificationRule` import and `__all__` entry)
- Delete: `app/models/notification_rule.py`
- Test: `tests/messaging/test_models.py`

**Interfaces:**
- Produces ORM classes (default schema, like `sp_*`), all with `Mapped[...]` style:
  - `MsgEvent`: `id: UUID PK`, `source: str`, `type: str`, `subject: str | None`, `time: datetime`, `severity: str`, `product: str | None`, `environment: str | None`, `data: dict` (JSONB), `received_at: datetime` (server default now). Index on `(type, received_at)`.
  - `MsgDelivery`: `id: UUID PK default uuid4`, `event_id: FK msg_events.id ON DELETE CASCADE`, `consumer: str`, `params: dict` (JSONB), `params_hash: str`, `status: str` (check in `PENDING|DONE|DEAD`, default `PENDING`), `attempts: int = 0`, `next_attempt_at: datetime` (default now), `lease_until: datetime | None`, `last_error: str | None`, `done_at: datetime | None`. Unique `(event_id, consumer, params_hash)`. Index on `(status, next_attempt_at)`.
  - `MsgInbox`: `id: UUID PK default uuid4`, `event_id: FK msg_events.id ON DELETE CASCADE`, `delivery_id: FK msg_deliveries.id ON DELETE SET NULL`, `user_id: FK users.id ON DELETE CASCADE`, `title: str`, `body: str`, `link: str | None`, `severity: str`, `type: str`, `dedupe_key: str | None`, `created_at` (server default now), `read_at: datetime | None`, `auto_read: bool = False`. Unique `(event_id, user_id)`. Index on `(user_id, created_at)`.
  - `MsgDedupe`: `key: str PK`, `members: list[str]` (JSONB), `last_event_id: UUID | None`, `last_notified_at: datetime | None`, `updated_at` (server default now, onupdate now).

- [ ] **Step 1: Write failing tests** (DB): insert an event then two deliveries with the same `(event_id, consumer, params_hash)` raises `IntegrityError`; two inbox rows with the same `(event_id, user_id)` raise; a delivery with status `"X"` raises; deleting the event cascades to deliveries and inbox rows. Use `TestSession` and clean up created rows.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_models.py -v`; expect FAIL.
- [ ] **Step 3: Implement** models and the migration (`upgrade` creates the four tables and indexes; `downgrade` drops them in reverse order). Apply with `uv run alembic upgrade head` on the local DB.
- [ ] **Step 4: Run** the tests; expect PASS. Also run `uv run pytest tests/test_permissions_catalog.py tests/test_flaky_models.py` to confirm nothing else broke from the model-registry change.
- [ ] **Step 5: Propose commit** `feat: add messaging tables and remove unused NotificationRule model`.

---

### Task 3: Rules and consumer registry

**Files:**
- Create: `app/messaging/consumers.py`, `app/messaging/rules.py`
- Test: `tests/messaging/test_rules.py`

**Interfaces:**
- Consumes: `CloudEvent`, `SEVERITY_ORDER`, `QG_RUN_COMPLETED`, `NRT_REVIEWS_PENDING`, `SANP_CHECKS_FAILING`, `get_event_type`.
- Produces:
  - `consumers.py`: `@dataclass(frozen=True) class DeliveryContext: params: dict[str, Any]; delivery_id: uuid.UUID; attempt: int; session: AsyncSession`; `Handler = Callable[[CloudEvent, DeliveryContext], Awaitable[None]]`; `@dataclass(frozen=True) class Consumer: name: str; handler: Handler; merge: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None`; `CONSUMERS: dict[str, Consumer]` with `register_consumer(consumer: Consumer) -> None`. (`session` is the handler's own DB session, used by the inbox to write its store; handlers still never read `msg_deliveries`.)
  - `rules.py`: `Match(type: str, min_severity: Severity = "info", products: tuple[str, ...] = (), environments: tuple[str, ...] = (), predicate: Callable[[CloudEvent], bool] | None = None)` (frozen dataclass; `type` is an exact id or an `fnmatch` glob); `DeliverySpec(consumer: str, params: dict[str, Any])`; `Rule(id: str, match: Match, deliveries: tuple[DeliverySpec, ...])`; `RULES: tuple[Rule, ...]`; `route(event: CloudEvent, rules: Sequence[Rule] = RULES) -> list[DeliverySpec]`; `params_hash(params: dict[str, Any]) -> str` (SHA-256 of canonical JSON: sorted keys, sorted lists of strings); `validate_registry(rules: Sequence[Rule] = RULES) -> None`.
  - The three phase 1 rules of section 5.11, all to consumer `inbox` with `params={"roles": ["qa_analyst", "qa_engineer", "qa_manager"]}`: `qg-completed` (QG type, any severity, so both passed and failed runs notify), `nrt-pending-review` (NRT type), `sanp-failing` (SANP type). The inbox consumer is registered by Task 6; `validate_registry` is called by `app/messaging/__init__.py` after consumers are registered.

- [ ] **Step 1: Write failing tests**
  - `test_route_matches_type_and_min_severity`: a QG `warning` event and a QG `info` event both route to inbox through `qg-completed`; a test rule with `min_severity="warning"` skips an `info` event; an event of an unmapped type returns `[]`.
  - `test_same_consumer_params_merge`: with a test consumer whose `merge` unions roles, two rules matching one event yield one `DeliverySpec` with the union of roles; with no merge hook and different params, two specs are returned; identical params collapse to one.
  - `test_params_hash_canonical`: `params_hash({"roles": ["b","a"]}) == params_hash({"roles": ["a","b"]})`.
  - `test_validate_registry_rejects_unknown_consumer_and_role`: both raise `ValueError` with the offending name in the message.
  - `test_glob_and_product_filters`.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_rules.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** Roles are checked against `app.core.permissions.EDITABLE_ROLES` for any param key `roles`. Register a stub consumer in tests only; the production registry is filled in Task 6.
- [ ] **Step 4: Run** the tests; expect PASS.
- [ ] **Step 5: Propose commit** `feat: add messaging routing rules and consumer registry`.

---

### Task 4: Transport seam, `publish()` and deduplication

**Files:**
- Create: `app/messaging/transport.py`, `app/messaging/bus.py`
- Modify: `app/messaging/__init__.py` (re-export `publish`, `clear_condition`, `wake_dispatcher`, `CloudEvent`, `new_event`)
- Test: `tests/messaging/test_bus.py`, `tests/messaging/test_dedupe.py`

**Interfaces:**
- Consumes: ORM models (Task 2), `route`, `params_hash`, `get_event_type`, `CloudEvent`.
- Produces:
  - `transport.py`: `class Transport(Protocol): async def enqueue(self, session: AsyncSession, event_id: uuid.UUID, deliveries: Sequence[DeliverySpec]) -> None; async def drain_due(self, *, limit: int = 50) -> DrainStats`; `@dataclass class DrainStats: claimed: int = 0; done: int = 0; failed: int = 0; dead: int = 0`; `class PostgresTransport` implementing it (`enqueue` inserts `msg_deliveries` rows with `ON CONFLICT DO NOTHING` on the unique key; `drain_due` is implemented in Task 5 and may `raise NotImplementedError` until then); `get_transport() -> Transport` returning a module-level `PostgresTransport` instance.
  - `bus.py`: `class PublishStatus(StrEnum): ACCEPTED="accepted"; DUPLICATE="duplicate"; SUPPRESSED="suppressed"`; `@dataclass(frozen=True) class PublishResult: status: PublishStatus; event_id: str; deliveries: int`; `async def publish(session: AsyncSession, event: CloudEvent) -> PublishResult` (no commit); `async def clear_condition(session: AsyncSession, key: str) -> None` (deletes the `msg_dedupe` row, no commit); `def wake_dispatcher() -> None` (best effort `dispatch_messages.apply_async` with `expires`; swallows and logs every exception; import the task lazily to avoid a cycle).
  - Behaviour of `publish` (section 5.4b and 5.6): validate `event.data` with the catalog `data_model` (invalid data raises `pydantic.ValidationError`, unknown type raises `UnknownEventType`); for `condition` types take a row lock on the `msg_dedupe` key (`INSERT ... ON CONFLICT DO NOTHING`, then `SELECT ... FOR UPDATE`), compute `new = members - stored`; empty `new` refreshes the stored set and returns `SUPPRESSED` without writing an event; otherwise insert the event (`ON CONFLICT (id) DO NOTHING`; a conflict returns `DUPLICATE`), route, enqueue deliveries, and store the full current members with `last_event_id` and `last_notified_at`. `occurrence` types skip the dedupe table.

- [ ] **Step 1: Write failing tests** (DB, each rolls back or cleans up):
  - `test_publish_stores_event_and_deliveries`: a warning QG event creates one `msg_events` row and one `msg_deliveries` row (`consumer="inbox"`, status `PENDING`) with the union of the three roles in `params`.
  - `test_publish_rolls_back_with_caller_transaction`: after `await session.rollback()` neither row exists.
  - `test_publish_duplicate_id_is_noop`: second publish of the same id returns `DUPLICATE`, one row only.
  - `test_publish_unknown_type_and_invalid_data_raise`.
  - `test_event_without_matching_rule_has_zero_deliveries`: stored, zero deliveries (use a catalogued type whose rules are monkeypatched to `()`).
  - `test_condition_first_report_accepted`, `test_condition_same_set_suppressed`, `test_condition_shrunk_set_suppressed_and_stored`, `test_condition_one_new_member_accepted_with_full_set_stored`, `test_condition_clear_then_reappear_accepted`, `test_condition_regrow_with_previously_seen_member_accepted` (set `{a,b}` → `{a}` suppressed → `{a,b}` accepted).
  - `test_condition_concurrent_publishes_serialise`: two sessions publish the same new set concurrently with `asyncio.gather`; exactly one is `ACCEPTED`.
  - `test_wake_dispatcher_swallows_errors`: patch the task's `apply_async` to raise; no exception escapes.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_bus.py tests/messaging/test_dedupe.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** Use SQLAlchemy `postgresql.insert(...).on_conflict_do_nothing(...)`. Log accepted, suppressed and duplicate outcomes with structlog (`event_id`, `type`, `status`).
- [ ] **Step 4: Run** the tests; expect PASS; run mypy.
- [ ] **Step 5: Propose commit** `feat: add messaging publish with transactional outbox and condition deduplication`.

---

### Task 5: Dispatcher (claim, lease, retry, dead) and Celery tasks

**Files:**
- Create: `app/messaging/dispatcher.py`, `app/tasks/messaging.py`
- Modify: `app/messaging/transport.py` (`PostgresTransport.drain_due`), `app/tasks/__init__.py` (import `messaging`), `app/tasks/schedule.py` (beat entries `dispatch-messages` every `settings.messaging_dispatch_interval_s` with `expires` equal to the interval, and `scan-conditions` every `settings.messaging_scan_interval_s`, and `purge-messages` daily at 03:30 UTC)
- Test: `tests/messaging/test_dispatcher.py`, `tests/messaging/test_transport_contract.py`, extend `tests/test_celery_queue.py` only if it enumerates tasks

**Interfaces:**
- Consumes: models, `CONSUMERS`, `DeliveryContext`, `Transport`, `run_async_task`.
- Produces:
  - `dispatcher.py`: `BACKOFF_S = (60, 300, 1800, 7200, 21600)`, `MAX_ATTEMPTS = 6`, `LEASE_S = 300`, `HANDLER_TIMEOUT_S = 60`; `@dataclass(frozen=True) class Claimed: delivery_id: uuid.UUID; event_id: uuid.UUID; consumer: str; params: dict[str, Any]; attempt: int`; `async def claim_due(session: AsyncSession, *, limit: int, now: datetime) -> list[Claimed]` (select `PENDING` with `next_attempt_at <= now` and `lease_until` null or expired, `FOR UPDATE SKIP LOCKED`, set `lease_until = now + LEASE_S`, commit; `attempt` is `attempts + 1`); `async def record_success(session, delivery_id, now) -> None`; `async def record_failure(session, delivery_id, error: str, now: datetime) -> str` (returns the new status, `PENDING` or `DEAD`; increments `attempts`, truncates `last_error` to 1000 chars, clears the lease, sets `next_attempt_at = now + BACKOFF_S[attempts-1]`, `DEAD` once `attempts >= MAX_ATTEMPTS`).
  - `PostgresTransport.drain_due(limit=50) -> DrainStats`: loop of claim (own session), load the event into a `CloudEvent`, run the handler under `asyncio.wait_for(..., HANDLER_TIMEOUT_S)` in its own session, then record the result in a new session. A missing consumer is recorded as a failure.
  - `app/tasks/messaging.py`: `dispatch_messages()` task named `app.tasks.messaging.dispatch_messages` (body via `run_async_task`, returns the `DrainStats` as dict); `scan_conditions()` named `app.tasks.messaging.scan_conditions` (calls `app.messaging.scanners.run_all()` once Task 10 exists; until then returns `{}`); `purge_messages()` named `app.tasks.messaging.purge_messages` deleting events, `msg_dedupe` rows and (via cascade) deliveries and inbox rows older than `settings.messaging_retention_days`.

- [ ] **Step 1: Write failing tests** (DB, with a registered test consumer):
  - `test_claim_sets_lease_and_skips_leased`: a claimed delivery is not returned by a second `claim_due` until `now > lease_until`.
  - `test_claim_skips_future_next_attempt`.
  - `test_success_marks_done`; `test_failure_backoff_schedule`: failures 1 to 5 set `next_attempt_at` to `now + 60, 300, 1800, 7200, 21600` seconds and keep `PENDING`; the sixth gives `DEAD`.
  - `test_failure_isolation`: two deliveries of one event, handler A raises, handler B succeeds; B is `DONE`, A is retried.
  - `test_expired_lease_is_retried_after_crash`: simulate a claim with no recorded result, advance `now` beyond the lease, and the delivery is claimed again with `attempt == 1` (`attempts` only increments when a failure is recorded).
  - `test_handler_timeout_counts_as_failure` (patch `HANDLER_TIMEOUT_S` to 0.05 and a sleeping handler).
  - `test_redelivery_of_done_event_is_harmless`: setting a `DONE` delivery back to `PENDING` re-runs an idempotent handler without a second effect.
  - **Transport contract test** (`test_transport_contract.py`): a function `run_transport_contract(transport: Transport)` asserting enqueue-then-drain calls the handler once with the event and params, retries after failure, and goes `DEAD` after six failures; parametrised with `PostgresTransport`. This is the suite a future Service Bus transport must pass.
  - `test_purge_deletes_old_rows_only`.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_dispatcher.py tests/messaging/test_transport_contract.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** No transaction is held while the handler runs (claim commits first). Import the new task module in `app/tasks/__init__.py` (a module that is not imported is never registered by the worker).
- [ ] **Step 4: Run** the tests and `uv run pytest tests/test_celery_queue.py`; expect PASS.
- [ ] **Step 5: Propose commit** `feat: add messaging dispatcher with lease, retry and dead state`.

---

### Task 6: Inbox consumer

**Files:**
- Create: `app/messaging/inbox.py`
- Modify: `app/messaging/__init__.py` (register the `inbox` consumer with its role-union `merge`, then call `validate_catalog()` and `validate_registry()`)
- Test: `tests/messaging/test_inbox_consumer.py`

**Interfaces:**
- Consumes: `DeliveryContext`, `CloudEvent`, `get_event_type(...).render`, `User`, `MsgInbox`.
- Produces: `async def inbox_handler(event: CloudEvent, ctx: DeliveryContext) -> None`; `def merge_roles(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]` (union, sorted); `INBOX_CONSUMER = Consumer(name="inbox", handler=inbox_handler, merge=merge_roles)`.
- Behaviour: select active users whose `role` is in `ctx.params["roles"]`; render title, body, link with the catalog; insert one `msg_inbox` row per user (`ON CONFLICT (event_id, user_id) DO NOTHING`) with `severity`, `type`, `dedupe_key` (`key(event)` for `condition` types, else null), `delivery_id`; then, for each such user and non-null `dedupe_key`, mark that user's older rows with the same `dedupe_key` and `read_at IS NULL` as read (`read_at = now()`, `auto_read = True`), never the new row. Zero matching users: log a warning and return (delivery ends `DONE`). The handler never commits: the dispatcher (Task 5) commits the handler's session after the handler returns, and rolls it back on error.

- [ ] **Step 1: Write failing tests** (DB, create users with roles through `User(...)` and clean up): fan-out by role (manager and engineer receive, guest does not); inactive user skipped; no matching users returns without rows and without raising; running the handler twice for the same event creates one row per user (idempotent); snapshot stored (changing the template function afterwards does not alter the stored title); superseding: delivering a second NRT condition event marks the first message `read_at` set and `auto_read` true for each user while the new one stays unread; a different dedupe key is untouched; `merge_roles({"roles":["a"]},{"roles":["b","a"]}) == {"roles":["a","b"]}`.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_inbox_consumer.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** Also confirm `import app.messaging` raises nothing with the production `RULES` (registry validation passes).
- [ ] **Step 4: Run** the tests plus `tests/messaging/test_rules.py`; expect PASS.
- [ ] **Step 5: Propose commit** `feat: add inbox consumer with per-user delivery and superseding`.

---

### Task 7: Inbox service, schemas, identity and API

**Files:**
- Create: `app/messaging/inbox_service.py`, `app/schemas/messaging.py`, `app/api/v1/messaging.py`
- Modify: `app/api/v1/router.py` (include router with prefix `/messaging`, tag `messaging`), `app/core/permissions.py` (new `view:inbox` entry), `tests/test_permissions_catalog.py` (count 30 to 31)
- Test: `tests/messaging/test_inbox_service.py`, `tests/messaging/test_inbox_api.py`

**Interfaces:**
- Produces:
  - Schemas: `InboxItemOut(id: uuid.UUID, title: str, body: str, link: str | None, severity: Severity, type: str, created_at: datetime, read_at: datetime | None, auto_read: bool)`; `InboxPageOut(items: list[InboxItemOut], next_cursor: str | None)`; `InboxSummaryOut(unread: int, total: int, max_unread_severity: Severity | None)`; `MarkReadOut(updated: int)`.
  - Service: `async def list_inbox(db, user_id, *, unread_only: bool = False, severity: str | None = None, type: str | None = None, limit: int = 50, cursor: str | None = None) -> InboxPageOut` (newest first; opaque base64 cursor over `(created_at, id)`; `limit` 1..100); `async def inbox_summary(db, user_id) -> InboxSummaryOut` (`total` counts the user's rows within `messaging_retention_days`); `async def mark_read(db, user_id, inbox_id) -> bool` (False when the row is not the user's); `async def mark_all_read(db, user_id) -> int`.
  - Identity dependency in `app/api/v1/messaging.py`: `async def get_inbox_user(db: DbDep, x_user_email: Annotated[str | None, Header()] = None) -> User | None`: missing header raises 401; matches `users.email` case-insensitively; unknown or inactive returns `None`.
  - Routes (prefix `/api/v1/messaging`): `GET /inbox`, `GET /inbox/summary`, `POST /inbox/{inbox_id}/read` (404 when not found or no user), `POST /inbox/read-all`. For `user is None`: list returns an empty page, summary `{unread:0,total:0,max_unread_severity:null}`, read-all `{updated:0}`.
  - Permission: `{"key": "view:inbox", "label": "Messaggi", "category": "Generale", "defaults": {"qa_manager": True, "qa_analyst": True, "qa_engineer": True, "guest": False}}`.

- [ ] **Step 1: Write failing tests.** Service (DB): ordering and pagination with a cursor across 3 pages; `unread_only`, severity and type filters; `inbox_summary` distinguishes `total=0` from `unread=0,total>0` and reports the max unread severity (`critical` over `warning` over `info`); rows older than the retention window are excluded from `total`; `mark_read` refuses another user's row; `mark_all_read` returns the count and is idempotent. API (`client` fixture, DB users): missing header gives 401; unknown email and inactive user give 200 with empty data; case-insensitive email match; `POST .../read` then summary decrements `unread`; read-all; `view:inbox` is in `ACTION_KEYS` with `guest` false.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_inbox_service.py tests/messaging/test_inbox_api.py tests/test_permissions_catalog.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** Follow the style of `app/api/v1/flaky.py` for the router, response models and error handling.
- [ ] **Step 4: Run** the tests; expect PASS.
- [ ] **Step 5: Propose commit** `feat: add messaging inbox API and view:inbox permission`.

---

### Task 8: Ingest route

**Files:**
- Modify: `app/api/v1/messaging.py` (add `POST /events` and the key dependency), `app/schemas/messaging.py` (`EventIngestOut`)
- Test: `tests/messaging/test_ingest_api.py`

**Interfaces:**
- Consumes: `publish`, `wake_dispatcher`, `CloudEvent`, `AuditLog`, `settings.messaging_ingest_key`.
- Produces: `EventIngestOut(id: str, status: Literal["accepted","duplicate","suppressed"])`; `require_ingest_key(x_hub_ingest_key: Annotated[str | None, Header()] = None) -> None` (503 when the setting is empty, 401 when the header is missing or differs, compared with `hmac.compare_digest`, never logged); `POST /api/v1/messaging/events`: body is a `CloudEvent`; status 202 for `accepted`, 200 for `duplicate` or `suppressed`, 422 for an invalid envelope, unknown type or invalid `data`; commits, calls `wake_dispatcher()`, and writes an `audit_logs` row `messaging.event.ingest` (`resource_type="msg_event"`, `resource_id=<event id>`, detail JSON with `type` and `status`) in the same transaction, using the same `AuditLog(...)` pattern as `app/services/quality_gate.py`.

- [ ] **Step 1: Write failing tests:** 503 with empty key setting; 401 without header and with a wrong header; 202 for a valid NRT condition event, then 200 `suppressed` for the same set; 200 `duplicate` for a repeated QG event id; 422 for unknown type, bad severity and `data` not matching the catalog model; an audit row exists after acceptance; the key value never appears in captured logs (use `caplog`).
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_ingest_api.py -v`; expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests; expect PASS.
- [ ] **Step 5: Propose commit** `feat: add messaging event ingest route`.

---

### Task 9: Quality Gate producer

**Files:**
- Create: `app/messaging/producers/__init__.py`, `app/messaging/producers/quality_gate.py`
- Modify: `app/api/v1/test_metrics.py` (`create_test_run`: after the run is stored, when `test_run_out.test_origin == "QG"` call the emitter)
- Test: `tests/messaging/test_producer_quality_gate.py`

**Interfaces:**
- Consumes: `TestRun`, `TestSuite` (`app/models/test_metrics.py`), `new_event`, `deterministic_id`, `publish`, `wake_dispatcher`.
- Produces: `def build_quality_gate_event(run: TestRun, suite: TestSuite) -> CloudEvent` (pure: type `QG_RUN_COMPLETED`, source `/qa-hub/quality-gate`, subject the run id, `id = deterministic_id(QG_RUN_COMPLETED, str(run.id))`, severity `warning` when `failed + broken + skipped > 0` (treat `None` as 0) else `info`, `environment` the lower-cased `run.env.value` when set, `data` as `QgRunData`); `async def emit_quality_gate_completed(db: AsyncSession, run: TestRun) -> None`: loads the suite by `run.suite_id`, publishes, commits, calls `wake_dispatcher()`; on any exception it logs, rolls back and returns (never raises).

- [ ] **Step 1: Write failing tests:** `build_quality_gate_event` severity for failures/none, `None` counters, deterministic id equal across two builds; DB: emitting for a QG run creates one event and one delivery; emitting twice for the same run creates one event (second is `DUPLICATE`); an exception inside `publish` (patched) is swallowed and the run row still exists; route test: `POST /api/v1/test-metrics/test-runs` with `test_origin="QG"` creates an event, with `test_origin="NRT"` and `"TAS"` creates none, and the response is unchanged when the emitter fails.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_producer_quality_gate.py -v`; expect FAIL.
- [ ] **Step 3: Implement.** Keep the route change to a few lines; the emitter does the work.
- [ ] **Step 4: Run** the tests plus `uv run pytest tests -k "test_metrics or quality_gate" -q`; expect PASS.
- [ ] **Step 5: Propose commit** `feat: publish a message when a Quality Gate run completes`.

---

### Task 10: Scanners (NRT pending, SANP failing checks)

**Files:**
- Create: `app/messaging/scanners/__init__.py`, `nrt_pending.py`, `sanp_failing.py`
- Modify: `app/services/test_metrics.py` (extract the pending predicate and add `list_pending_nrt_run_ids`; `get_pending_runs` keeps its behaviour), `app/tasks/messaging.py` (`scan_conditions` calls `run_all`)
- Test: `tests/messaging/test_scanners.py`; existing `get_pending_runs` tests must still pass

**Interfaces:**
- Produces:
  - `test_metrics.py`: `async def list_pending_nrt_run_ids(db: AsyncSession) -> list[uuid.UUID]` (runs with `revision_result == PENDING`, `test_origin` not null and not in `("TAS","QG")`, `timestamp_end > _list_window_start()`), built on a shared helper so `get_pending_runs` and this function use one predicate.
  - `scanners/__init__.py`: `@dataclass(frozen=True) class Scanner: name: str; run: Callable[[AsyncSession], Awaitable[None]]`; `SCANNERS: tuple[Scanner, ...]`; `async def run_all() -> dict[str, str]` (each scanner in its own session and transaction; exceptions caught and logged; returns `{name: "ok" | "error"}`).
  - `nrt_pending.scan(db)`: ids empty gives `clear_condition(db, "nrt.reviews.pending")`; otherwise `publish` of a `NRT_REVIEWS_PENDING` event (source `/qa-hub/scanners/nrt`, severity `info`, `data = NrtPendingData(count, run_ids=sorted str ids)`); then commit and `wake_dispatcher()`.
  - `sanp_failing.scan(db)`: take the latest `SanpHealthRun` with `import_status == COMPLETE` (by `completed_at`); items are `f"{environment.value}:{spec_name}"` for its results with status `KO` or `WARNING` (sorted); no completed run or no items gives `clear_condition(db, "sanp_health.checks.failing")`; otherwise publish `SANP_CHECKS_FAILING` (source `/qa-hub/scanners/sanp`, severity `warning`, `data = SanpFailingData(ko, warning, items)`).

- [ ] **Step 1: Write failing tests** (DB, create suites, runs, SANP rows and clean up): NRT list excludes TAS and QG origins, non-PENDING runs and runs older than the window, and matches `get_pending_runs().nrt`; first scan accepted, second scan suppressed (one message per user in total after dispatch), a new pending run produces a second message and supersedes the first, all reviewed then `clear_condition` and a later pending run is announced again; SANP scan counts `KO` and `WARNING` only (not `INFO`, `OK`, `NOT_CONFIGURED`), uses only the latest completed run, a spec flipping `WARNING` to `KO` does not re-notify, empty clears; `run_all` continues when one scanner raises and reports `error` for it.
- [ ] **Step 2: Run** `uv run pytest tests/messaging/test_scanners.py -v`; expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests and `uv run pytest tests -k "pending or sanp" -q`; expect PASS.
- [ ] **Step 5: Propose commit** `feat: add NRT pending and SANP Health scanners for the messaging hub`.

---

### Task 11: End-to-end test, documentation and OpenAPI

**Files:**
- Create: `docs/messaging/event-catalog.md`, `tests/messaging/test_end_to_end.py`
- Modify: `README.md` (new section "QA Messaging Hub"), `CLAUDE.md` (durable rules, section 9 of the spec), `openapi/qa-hub-be.json` (regenerated)

- [ ] **Step 1: Write the end-to-end test:** a QG failure ingested through `POST /test-runs` then one `drain_due()` leaves an unread `warning` message with link `/quality-gate/report/<id>` for each of the three roles' users and none for a guest; a scanner pass twice then `drain_due()` leaves exactly one NRT message per user. Run it; expect PASS only after Tasks 1-10 (it should pass at once; fix wiring if not).
- [ ] **Step 2: Write `docs/messaging/event-catalog.md`:** a table of the three event types (type id, producer, policy, severity rule, `data` shape, audience, link) and the routing rules table of section 5.11.
- [ ] **Step 3: README section** (English): purpose, event flow diagram from section 5.2, the routes, settings, the interim identity header and ingest key, how to re-queue a `DEAD` delivery (SQL: `UPDATE msg_deliveries SET status='PENDING', attempts=0, next_attempt_at=now(), lease_until=NULL WHERE id=...`), and a subsection **"Replacing Postgres with Azure Service Bus"** that reproduces section 8 of the spec: what stays stable, what changes (outbox plus relay, subscription per consumer filtered on `consumer`, receiver calling the same handler), the six rules, and when to switch.
- [ ] **Step 4: `CLAUDE.md`:** add a "Messaging Hub" section with the durable rules from section 9 of the spec.
- [ ] **Step 5: Regenerate OpenAPI** with `uv run --extra dev python -m scripts.generate_openapi --apim-compatible` and check the diff only adds the `/messaging` operations.
- [ ] **Step 6: Run** the full quality pass: `uv run ruff check .`, `uv run mypy .`, `uv run pytest -q`. Report results.
- [ ] **Step 7: Propose commit** `docs: document the QA Messaging Hub and regenerate OpenAPI`.

---

### Task 12: README maintenance gate and PR text (no automatic action)

- [ ] Apply the repository's documentation gate if one exists for this repo (the integration-test repo's `ppa-readme` skill is for that repo only; here the README section in Task 11 is the gate). Prepare, but do not run, a PR title and description (context, summary, how tested, follow-ups: frontend plan, Helm secret, TAS publishing via ingest, Jira and Slack consumers, Service Bus transport) and show it to the owner.

---

### Task 13: Helm secret proposal (needs owner confirmation)

- [ ] Show the owner the exact change to add `MESSAGING_INGEST_KEY` to `envSecret` in `helm/values-dev.yaml`, `values-uat.yaml`, `values-prod.yaml` (value is the Key Vault secret name, e.g. `qa-centralhub-messaging-ingest-key`; follow how `TAS_GITHUB_TOKEN` is declared) and ask before editing. Note that the secret must exist in Key Vault before deploying, and that APIM must re-import the regenerated OpenAPI.

---

## Self-review (spec coverage)

- 5.3 envelope and catalog: Task 1. 5.4 rules: Task 3. 5.4b deduplication and superseding: Tasks 4 and 6. 5.5 storage and retention: Tasks 2 and 5. 5.6 publish and ingest: Tasks 4 and 8. 5.7 dispatcher: Task 5. 5.8 consumers: Tasks 3 and 6. 5.9 API, identity, permission: Task 7. 5.11 producers and rules: Tasks 3, 9, 10. 5.11b scanners: Task 10. 5.12 observability: structlog lines in Tasks 4 and 5. 5.13 housekeeping: Task 2. Section 6 tests: each task; the transport contract test is in Task 5. Section 8 documentation: Task 11. Frontend (5.10): separate plan.
- Deviations from the spec, both deliberate: task names live under `app.tasks.messaging.*` (project convention) rather than `app.messaging.*`; `DeliveryContext` carries the handler's DB `session`.
- Types used across tasks are defined once: `CloudEvent`, `EventType`, `DeliverySpec`, `Consumer`, `DeliveryContext`, `Transport`, `PublishResult`, `Claimed`.
