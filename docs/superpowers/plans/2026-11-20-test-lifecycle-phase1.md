# Test Lifecycle — Fase 1 (Fondamenta) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In QA Hub si possono creare Ambiti, Feature e Scenari, scrivere gli scenari nell'editor ibrido con validazione Gherkin, e far avanzare la Feature nel workflow DRAFT → IN REVIEW → DESIGNED con review, revisioni e storico. Ancora senza Git, Jira e NRT.

**Architecture:** Nuovo modulo backend `test_lifecycle` (modelli nello schema `qachub`, servizi in `app/services/test_lifecycle/`, router `/api/v1/test-lifecycle`) e nuova sezione frontend `/test-lifecycle`. Il contenuto progettuale vive in **revisioni**; l'identità di Feature e Scenario (codice, numero, tag) vive fuori dalle revisioni ed è immutabile. Il backend calcola le transizioni possibili e i motivi di blocco; il frontend si limita a mostrarli.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Postgres, `gherkin-official` (parser Gherkin); Next.js 16, React 19, TanStack Query, Vitest + Testing Library.

**Spec:** `qa-hub-backend/docs/superpowers/specs/2026-11-20-test-lifecycle-design.md` (§3 decisioni, §5 dominio, §6 workflow, §10 permessi, §11 UX). Gli esecutori leggono spec e piano.

**Repo:** `qa-hub-backend` (Task 1–7) e `qa-hub-frontend` (Task 8–12), entrambi sul branch `feature/test-lifecycle`.

## Perimetro della fase 1

Dentro: Ambiti (con area), Feature, revisioni, Scenari con identità immutabile, editor ibrido con validazione, anteprima del `.feature` generato, review con commenti, transizioni DRAFT → IN REVIEW → DESIGNED, riapertura come nuova revisione, storico (eventi + snapshot), permessi e feature flag.

Fuori (fasi successive): PR su Git, import da Git, revisione "pubblicata" via merge, anomalie, base superata (fase 2); creazione e verifica Jira (fase 3: qui le chiavi Jira sono campi validati solo nel formato); NRT, RUNNING automatico, alias (fase 4); migrazione (fase 5); Studio di contesto (fase 6); autocompletamento degli step (fase 2, perché richiede l'indice degli step dal repo).

Conseguenza: in fase 1 una revisione approvata resta APPROVED (la PR arriva in fase 2), quindi una Feature DESIGNED non si può ancora riaprire. Il feature flag `test_lifecycle` resta **disattivato di default** fino alla fase 2.

## Global Constraints

- Tabelle nuove nello schema `qachub`, prefisso `tl_`. Migrazione Alembic `0036`, `down_revision = "0035"`.
- Le enum Postgres seguono il pattern di `app/models/dq.py` (`ENUM(..., create_type=False, values_callable=...)`, tipo creato nella migrazione).
- Pattern di layering: `models/` → `schemas/` → `services/` → `api/v1/`. Il backend non verifica il JWT sugli endpoint (pattern esistente); l'attore arriva nel body come `actor_email` e viene risolto su `users.email`. Un `actor_email` sconosciuto o inattivo → 403.
- Codice Ambito: regex `^[A-Za-z][A-Za-z0-9]{1,19}$`, unico case-insensitive. È immutabile se l'Ambito ha almeno una Feature (409).
- Codice Feature: `FEAT_{numero:03d}_{codice_ambito}` (es. `FEAT_001_FdR3`). Numero progressivo per Ambito, assegnato dal sistema, immutabile, mai riusato.
- Tag Feature: `@{CODICE_AMBITO_MAIUSCOLO}_{numero:03d}` (es. `@FDR3_001`). Tag identità Scenario: `@{CODICE_AMBITO_MAIUSCOLO}_{feat:03d}_{scen:02d}` (es. `@FDR3_001_02`). Numero scenario progressivo per Feature, mai riusato.
- Header del file generato: prima riga `# qa-hub: <codice feature>`, seconda `# language: it`. Keyword italiane (`Funzionalità`, `Contesto`, `Scenario`, `Schema dello scenario`, `Esempi`, `Dato/Data/Dati/Date`, `Quando`, `Allora`, `E`, `Ma`).
- Tag aggiuntivi ammessi (lista controllata, costante `ALLOWED_EXTRA_TAGS`): `runnable`, `e2e`, `ui`, `skip`. Polarità resa come `@positivo` / `@negativo`.
- Chiavi Jira in fase 1: Progettazione `^PQ-\d+$`; Epic/Story prodotto `^[A-Z][A-Z0-9]+-\d+$`.
- Stati Feature (`tl_feature_status`): `draft`, `in_review`, `designed`, `implemented`, `running`. Stati revisione (`tl_revision_status`): `draft`, `in_review`, `approved`, `merged`, `superseded`. In fase 1 si usano solo `draft`/`in_review`/`designed` e `draft`/`in_review`/`approved`; gli altri valori esistono già nell'enum.
- Eventi di storico in `audit_logs` con `resource_type = "tl_feature"`, `resource_id = str(feature.id)`, `action` con prefisso `tl.` (es. `tl.revision.submitted`), `detail` in JSON.
- Permessi (catalogo `app/core/permissions.py`): `view:test_lifecycle` (tutti tranne guest), `edit:tl_feature` (qa_manager, qa_analyst, qa_engineer), `review:tl_feature` (qa_manager, qa_analyst, qa_engineer), `manage:tl_scopes` (solo qa_manager). Feature catalog: chiave `test_lifecycle`, route `/test-lifecycle`, action `view:test_lifecycle`.
- Testi UI in italiano, come il resto del portale. Il modulo BDD non va né usato né importato.
- Frontend: prima di scrivere route dinamiche leggere `node_modules/next/dist/docs/` (regola di `AGENTS.md`); seguire il pattern `useParams()` di `src/app/(portal)/bdd/[id]/page.tsx`.

## Review Focus

1. **Due persone modificano la stessa bozza**: il secondo salvataggio non deve sovrascrivere in silenzio il primo. Ogni revisione ha `lock_version`; PATCH con versione vecchia → 409 e la UI mostra "modificata da un altro utente, ricarica". Test: Task 4 `test_update_revision_with_stale_lock_version_returns_409`, Task 10 test sul messaggio.
2. **Chi approva non è il reviewer, o il reviewer è l'owner**: approvazione da parte di un utente diverso dal reviewer → 403. Reviewer uguale all'owner design → l'invio in review è bloccato con motivo esplicito. Test: Task 5 `test_approve_by_non_reviewer_is_forbidden`, `test_submit_blocked_when_reviewer_is_owner`.
3. **Creazione concorrente di scenari**: due richieste simultanee non devono produrre lo stesso numero. Allocazione con `SELECT … FOR UPDATE` sulla riga Feature e vincolo unico `(feature_id, number)`. Test: Task 4 `test_concurrent_scenario_creation_allocates_distinct_numbers`.
4. **Step che rompono il file generato**: righe che non iniziano con una keyword, righe che iniziano con `#`, `@` o una keyword di sezione (`Scenario:`, `Funzionalità:`), outline senza Esempi, segnaposto `<x>` senza colonna corrispondente, step vuoti. Vanno rifiutati dalla validazione con riga e messaggio in italiano, mai salvati come "validi". Test: Task 2 `test_validate_steps_*`.
5. **Rinominare il codice Ambito quando esistono Feature** cambierebbe codici e tag già emessi: deve restituire 409. Test: Task 3 `test_scope_code_is_immutable_once_features_exist`.

---

### Task 1: Modelli e migrazione

**Files:**
- Create: `app/models/test_lifecycle.py`
- Modify: `app/models/__init__.py` (export dei nuovi modelli)
- Create: `alembic/versions/0036_create_test_lifecycle.py`
- Test: `tests/test_tl_models.py`

**Interfaces:**
- Produces (tutti in `app/models/test_lifecycle.py`, schema `qachub`):
  - enum `TlFeatureStatus`, `TlRevisionStatus`, `TlScenarioKind` (`scenario`, `outline`), `TlPolarity` (`positivo`, `negativo`);
  - `TlScope`: `id`, `code` (String(20), indice unico su `lower(code)`), `name`, `area: str|None`, `repo_folder: str|None`, `owner_team: str|None`, `next_feature_number: int = 1`, `created_at`, `updated_at`;
  - `TlFeature`: `id`, `scope_id` FK, `number: int`, `code` (unico), `status: TlFeatureStatus`, `design_owner_id`, `reviewer_id`, `impl_owner_id` (FK `users.id`, nullable), `jira_design_key`, `jira_impl_key` (nullable), `working_revision_id`, `published_revision_id` (FK `tl_feature_revisions.id`, nullable, `use_alter=True`), `next_scenario_number: int = 1`, `created_by: str`, `created_at`, `updated_at`; vincolo unico `(scope_id, number)`;
  - `TlFeatureRevision`: `id`, `feature_id` FK, `number: int` (unico per feature), `status: TlRevisionStatus`, `base_revision_id` (nullable), `lock_version: int = 0`, `title`, `description: str|None`, `background_steps: str|None`, `requirements_url: str|None`, `product_issue_key: str|None`, `reverse_engineering: bool = False`, `design_estimate_days`, `impl_estimate_days` (Numeric(5,1), nullable), `created_by`, `approved_by: str|None`, `approved_at`, `created_at`, `updated_at`;
  - `TlScenario` (identità): `id`, `feature_id` FK, `number: int`, `tag: str`, `created_at`; vincolo unico `(feature_id, number)`;
  - `TlScenarioContent`: `id`, `revision_id` FK (cascade), `scenario_id` FK, `position: int`, `title`, `kind: TlScenarioKind`, `polarity: TlPolarity`, `extra_tags: list[str]` (JSONB, default `[]`), `steps: str`, `examples: dict|None` (JSONB `{"headers": [...], "rows": [[...]]}`), `detail_link: str|None`; vincolo unico `(revision_id, scenario_id)`;
  - `TlFeatureVersion`: `id`, `feature_id`, `revision_id`, `origin` (String: `hub`|`git`), `snapshot: dict` (JSONB), `commit_sha: str|None`, `actor: str`, `created_at`;
  - `TlReviewComment`: `id`, `revision_id` FK, `scenario_id` (nullable), `author: str`, `body: str`, `resolved: bool = False`, `created_at`.

- [ ] **Step 1: Scrivere i test sui vincoli** (DB reale con `tests._db.TestSession`, fixture che pulisce le righe `tl_*` create con codici ambito `T` + uuid corto): `test_feature_number_unique_per_scope` (seconda Feature con stesso `(scope_id, number)` → `IntegrityError`), `test_scenario_number_unique_per_feature`, `test_scenario_content_unique_per_revision`, `test_scope_code_unique_case_insensitive` (`FdR3` e `fdr3` → `IntegrityError`).
- [ ] **Step 2: Eseguire** `uv run --extra dev pytest tests/test_tl_models.py -v`. Atteso: FAIL (import error).
- [ ] **Step 3: Implementare i modelli e la migrazione `0036`** (crea i 4 tipi enum in `qachub`, le 7 tabelle, gli indici: `tl_features(scope_id, status)`, `tl_scenario_contents(revision_id, position)`; la FK circolare feature→revision aggiunta dopo con `op.create_foreign_key`). `downgrade` elimina tabelle e tipi.
- [ ] **Step 4: Eseguire** `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`, poi i test. Atteso: migrazione reversibile, test PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): add models and migration`.

### Task 2: Modulo Gherkin (codici, validazione, rendering)

**Files:**
- Modify: `pyproject.toml` (dipendenza `gherkin-official`), `uv.lock`
- Create: `app/services/test_lifecycle/__init__.py`, `app/services/test_lifecycle/gherkin.py`
- Test: `tests/test_tl_gherkin.py`

**Interfaces:**
- Produces (`app/services/test_lifecycle/gherkin.py`, funzioni pure, nessun accesso DB):
  - `ALLOWED_EXTRA_TAGS: tuple[str, ...]`
  - `feature_code(scope_code: str, number: int) -> str`
  - `feature_tag(scope_code: str, number: int) -> str`
  - `scenario_tag(scope_code: str, feature_number: int, scenario_number: int) -> str`
  - `@dataclass(frozen=True) GherkinError(line: int, message: str)`
  - `validate_steps(steps: str, *, kind: TlScenarioKind, examples: dict | None, background: bool = False) -> list[GherkinError]`: le righe sono relative al blocco step (da 1)
  - `@dataclass RenderScenario(tag, title, kind, polarity, extra_tags, steps, examples, detail_link)` e `@dataclass RenderFeature(code, feature_tag, title, description, background_steps, scenarios: list[RenderScenario])`
  - `render_feature_file(feature: RenderFeature) -> str`: formato esatto di spec §7 (indentazione 2 spazi per livello, una riga vuota tra blocchi, tag aggiuntivi e polarità su una riga, poi il tag identità su una riga propria, `#link:{detail_link}` prima di `Scenario:` se presente, tabella Esempi con colonne allineate, newline finale).

- [ ] **Step 1: Test** su codici e tag: `feature_code("FdR3", 1) == "FEAT_001_FdR3"`, `feature_tag("FdR3", 1) == "@FDR3_001"`, `scenario_tag("FdR3", 1, 2) == "@FDR3_001_02"`.
- [ ] **Step 2: Test di validazione** (Review Focus 4), ciascuno con l'asserzione sul numero di riga:
  - `test_validate_steps_accepts_italian_keywords` (`Dato…\nE…\nQuando…\nAllora…` → `[]`);
  - `test_validate_steps_rejects_line_without_keyword` (riga 2 `il sistema risponde` → errore riga 2);
  - `test_validate_steps_rejects_section_keyword_inside_steps` (`Scenario: x` → errore);
  - `test_validate_steps_rejects_comment_and_tag_lines` (`# nota`, `@tag` → errore);
  - `test_validate_steps_rejects_empty_steps`;
  - `test_validate_steps_outline_requires_examples`;
  - `test_validate_steps_outline_placeholder_without_column` (`<flowid>` senza colonna `flowid` → errore con il nome del segnaposto);
  - `test_validate_steps_background_allows_only_given_and_and` (in `background=True`, `Quando` → errore; regola del sync attuale).
  Messaggi in italiano.
- [ ] **Step 3: Test di rendering**: `test_render_feature_file_matches_golden`, con un golden atteso inline (Feature con Contesto, uno Scenario positivo `@runnable` con `detail_link`, uno Schema dello scenario negativo con 2 righe di Esempi), e `test_rendered_file_parses_with_gherkin_parser` (l'output passa da `gherkin.parser.Parser` senza errori).
- [ ] **Step 4: Eseguire i test.** Atteso: FAIL.
- [ ] **Step 5: Implementare.** La validazione compone un mini-documento (`# language: it` + `Funzionalità: x` + `Scenario:`/`Schema dello scenario:` + step indentati + eventuale `Esempi:`), lo passa a `gherkin.parser.Parser`, rimappa le righe di `CompositeParserException` sul blocco step e aggiunge i controlli che il parser non fa (righe `#`/`@`, segnaposto, Contesto solo `Dat*`/`E`).
- [ ] **Step 6: Eseguire i test.** Atteso: PASS. Poi `uv run ruff check app tests`.
- [ ] **Step 7: Commit** `feat(test-lifecycle): gherkin codes, validation and rendering`.

### Task 3: Ambiti — servizio e API

**Files:**
- Create: `app/schemas/test_lifecycle.py` (sezione Ambiti; le altre sezioni le aggiungono i task successivi)
- Create: `app/services/test_lifecycle/scopes.py`
- Create: `app/api/v1/test_lifecycle.py`; Modify: `app/api/v1/router.py` (`prefix="/test-lifecycle"`, `tags=["test-lifecycle"]`)
- Test: `tests/test_tl_scopes.py`

**Interfaces:**
- Produces: schemi `TlScopeCreate(code, name, area?, repo_folder?, owner_team?)`, `TlScopeUpdate` (tutti opzionali), `TlScopeOut` (+ `feature_count: int`, `status_counts: dict[str, int]`); servizi `list_scopes(db) -> list[TlScopeOut]`, `create_scope(db, data) -> TlScope`, `update_scope(db, scope_id, data) -> TlScope` (solleva `ScopeCodeLockedError`, `ScopeCodeConflictError`); endpoint `GET/POST /scopes`, `PATCH /scopes/{scope_id}`.
- Errori: codice non valido → 422; codice duplicato → 409; cambio codice con Feature esistenti → 409 con `detail` "Il codice dell'ambito non può cambiare: esistono già feature".

- [ ] **Step 1: Test API** (client `httpx` + DB reale): `test_create_scope_returns_201`, `test_create_scope_rejects_invalid_code` (`"1abc"`, `"FdR-3"` → 422), `test_create_scope_duplicate_code_case_insensitive_returns_409`, `test_scope_code_is_immutable_once_features_exist` (Review Focus 5), `test_scope_name_editable_with_features`, `test_list_scopes_includes_status_counts`.
- [ ] **Step 2: Eseguire.** Atteso: FAIL.
- [ ] **Step 3: Implementare** schemi, servizio e router.
- [ ] **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): scopes API`.

### Task 4: Feature, revisioni e scenari — servizio e API

**Files:**
- Modify: `app/schemas/test_lifecycle.py`
- Create: `app/services/test_lifecycle/actors.py`, `app/services/test_lifecycle/features.py`
- Modify: `app/api/v1/test_lifecycle.py`
- Test: `tests/test_tl_features.py`

**Interfaces:**
- Consumes: Task 1 (modelli), Task 2 (`feature_code`, `feature_tag`, `scenario_tag`, `validate_steps`).
- Produces:
  - `actors.resolve_actor(db, email: str) -> User` (solleva `UnknownActorError` → 403);
  - schemi `TlFeatureCreate(scope_id, title, design_owner_id, actor_email)`, `TlFeatureUpdate(design_owner_id?, reviewer_id?, impl_owner_id?, jira_design_key?, jira_impl_key?, actor_email)`, `TlRevisionUpdate(lock_version, actor_email, title?, description?, background_steps?, requirements_url?, product_issue_key?, reverse_engineering?, design_estimate_days?, impl_estimate_days?)`, `TlScenarioCreate(actor_email, title, kind, polarity)`, `TlScenarioUpdate(actor_email, lock_version, title?, kind?, polarity?, extra_tags?, steps?, examples?, detail_link?, position?)`, `TlScenarioOut` (identità + contenuto + `errors: list[{line, message}]`), `TlRevisionOut` (+ `scenarios`, `background_errors`), `TlFeatureListItem` (codice, titolo dalla revisione di lavoro o, se assente, dalla pubblicata, stato, owner, reviewer, ambito, `scenario_count`, `updated_at`), `TlFeatureDetail` (feature + `working_revision: TlRevisionOut|None` + `published_revision: TlRevisionOut|None` + `transitions` — campo vuoto finché il Task 5 non lo popola);
  - servizi `create_feature`, `update_feature`, `get_feature_detail`, `list_features(db, scope_id?, status?, owner_id?)`, `update_revision`, `add_scenario`, `update_scenario`, `remove_scenario`. Ogni scrittura su revisione incrementa `lock_version` e accetta modifiche solo se la revisione è `draft` (altrimenti 409 "La revisione non è modificabile nello stato …");
  - endpoint: `GET /features`, `POST /features`, `GET /features/{id}`, `PATCH /features/{id}`, `PATCH /features/{id}/revisions/{rev_id}`, `POST /features/{id}/revisions/{rev_id}/scenarios`, `PATCH /features/{id}/revisions/{rev_id}/scenarios/{scenario_id}`, `DELETE /features/{id}/revisions/{rev_id}/scenarios/{scenario_id}` (toglie il contenuto dalla revisione, l'identità e il numero restano consumati).
- Regole: `create_feature` blocca la riga Ambito (`FOR UPDATE`), assegna `next_feature_number`, crea la revisione 1 `draft` e la imposta come `working_revision_id`, scrive l'evento `tl.feature.created`. `add_scenario` blocca la riga Feature, assegna `next_scenario_number` e crea identità e contenuto in coda (`position` = max + 1). Gli errori di validazione **non bloccano il salvataggio** (si salva la bozza) ma vengono restituiti in `errors`.

- [ ] **Step 1: Test**:
  - `test_create_feature_assigns_code_and_draft_revision` (`FEAT_001_<code>`, stato `draft`, revisione 1);
  - `test_second_feature_gets_next_number`;
  - `test_unknown_actor_returns_403`;
  - `test_update_revision_with_stale_lock_version_returns_409` (Review Focus 1);
  - `test_add_scenario_assigns_identity_tag` (`@<CODE>_001_01`);
  - `test_removed_scenario_number_is_never_reused` (aggiungi 01 e 02, rimuovi 02, aggiungi → 03);
  - `test_concurrent_scenario_creation_allocates_distinct_numbers` (Review Focus 3: due `add_scenario` lanciati con `asyncio.gather` su due sessioni → numeri diversi);
  - `test_invalid_steps_are_saved_with_errors`;
  - `test_jira_keys_format_validated` (`jira_design_key="ABC-1"` → 422, `"PQ-12"` → OK);
  - `test_list_features_filters_by_scope_and_status`.
- [ ] **Step 2: Eseguire.** Atteso: FAIL.
- [ ] **Step 3: Implementare.**
- [ ] **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): features, revisions and scenarios API`.

### Task 5: Workflow, transizioni e storico

**Files:**
- Create: `app/services/test_lifecycle/workflow.py`, `app/services/test_lifecycle/history.py`
- Modify: `app/services/test_lifecycle/features.py` (popola `transitions` nel dettaglio), `app/schemas/test_lifecycle.py`, `app/api/v1/test_lifecycle.py`
- Test: `tests/test_tl_workflow.py`

**Interfaces:**
- Produces:
  - `TlTransitionAction` = `submit` | `approve` | `request_changes` | `reopen`;
  - `@dataclass TransitionCheck(action: str, allowed: bool, reasons: list[str])`;
  - `workflow.available_transitions(feature, revision, actor: User|None) -> list[TransitionCheck]`: sempre tutte e 4 le azioni, con `allowed` e motivi in italiano;
  - `workflow.apply_transition(db, feature_id, revision_id, action, actor_email, note: str|None) -> TlFeatureDetail` (solleva `TransitionNotAllowedError(reasons)` → 409 con `{"reasons": [...]}`, `NotReviewerError` → 403);
  - `history.record_event(db, feature_id, action: str, actor: User, detail: dict) -> None`;
  - `history.snapshot(db, feature, revision, origin="hub", actor) -> TlFeatureVersion` (snapshot JSON = metadati della revisione + scenari con tag e contenuto + output di `render_feature_file`);
  - `history.list_history(db, feature_id) -> list[TlHistoryItem]` (eventi + versioni ordinati per data, desc);
  - schemi `TlTransitionRequest(action, actor_email, note?)`, `TlTransitionOut`, `TlHistoryItem(kind: "event"|"version", at, actor, action?, detail?, version_id?)`;
  - endpoint `POST /features/{id}/revisions/{rev_id}/transitions`, `GET /features/{id}/history`, `GET /features/{id}/versions/{version_id}`.
- Guardie (spec §6, fase 1):
  - `submit` (revisione `draft`): owner design presente; reviewer presente e diverso dall'owner design; `jira_design_key` presente; `product_issue_key` presente **oppure** `reverse_engineering`; almeno uno scenario; nessun errore di validazione negli scenari e nel Contesto.
  - `approve` / `request_changes` (revisione `in_review`): l'attore è il reviewer. `request_changes` richiede `note`.
  - `reopen`: Feature senza revisione di lavoro aperta e con una revisione `merged` o pubblicata. In fase 1 è sempre non consentita, con motivo "Disponibile dopo il merge del feature file (fase Git)".
- Effetti:
  - `submit` → revisione `in_review`; se la Feature non ha `published_revision_id`, stato Feature `in_review`; evento `tl.revision.submitted`.
  - `request_changes` → revisione `draft`, Feature `draft` (stessa condizione); la nota diventa un commento generale; evento.
  - `approve` → revisione `approved` (`approved_by`, `approved_at`); Feature `designed` (stessa condizione); `snapshot(...)`; evento `tl.revision.approved`.
  - Ogni modifica a owner/reviewer/chiavi Jira (Task 4) scrive anche `tl.feature.updated` con i campi cambiati: va aggiunto qui, in `update_feature`.

- [ ] **Step 1: Test**:
  - `test_submit_blocked_reasons_listed` (Feature nuova senza reviewer, Jira, scenari → `allowed=False` e i 4 motivi attesi);
  - `test_submit_blocked_when_reviewer_is_owner` (Review Focus 2);
  - `test_submit_blocked_by_invalid_steps`;
  - `test_submit_moves_feature_to_in_review`;
  - `test_edit_revision_in_review_returns_409`;
  - `test_approve_by_non_reviewer_is_forbidden` (Review Focus 2);
  - `test_request_changes_requires_note_and_returns_to_draft`;
  - `test_approve_sets_designed_and_creates_snapshot` (lo snapshot contiene `file` uguale a `render_feature_file` della revisione);
  - `test_reopen_not_allowed_in_phase_1`;
  - `test_history_lists_events_and_versions_desc`.
- [ ] **Step 2: Eseguire.** Atteso: FAIL.
- [ ] **Step 3: Implementare.**
- [ ] **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): workflow transitions and history`.

### Task 6: Commenti di review, anteprima e validazione live

**Files:**
- Create: `app/services/test_lifecycle/reviews.py`
- Modify: `app/schemas/test_lifecycle.py`, `app/api/v1/test_lifecycle.py`
- Test: `tests/test_tl_reviews.py`

**Interfaces:**
- Produces:
  - `GET/POST /features/{id}/revisions/{rev_id}/comments` (`TlCommentCreate(actor_email, body, scenario_id?)`, `TlCommentOut`);
  - `PATCH /comments/{comment_id}` (`{actor_email, resolved: bool}`);
  - `GET /features/{id}/revisions/{rev_id}/preview` → `{file: str, errors: [{scenario_tag|null, line, message}]}`;
  - `POST /gherkin/validate` (`{steps, kind, examples?, background?}` → `{errors}`), senza scritture su DB.
- Regole: si può commentare su revisioni `draft` e `in_review`; su revisioni `approved` → 409. Ogni commento scrive l'evento `tl.comment.added`.

- [ ] **Step 1: Test**: `test_add_comment_on_scenario`, `test_comment_on_approved_revision_returns_409`, `test_resolve_comment`, `test_preview_returns_rendered_file_and_errors`, `test_validate_endpoint_returns_errors_without_db_writes`.
- [ ] **Step 2: Eseguire.** Atteso: FAIL.
- [ ] **Step 3: Implementare.**
- [ ] **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): review comments, preview and live validation`.

### Task 7: Permessi, feature flag e OpenAPI

**Files:**
- Modify: `app/core/permissions.py` (4 azioni dei Global Constraints, categoria `"Test Lifecycle"`), `app/core/features.py` (voce `test_lifecycle`), `tests/test_permissions_catalog.py` (aggiorna `test_action_catalog_size`, aggiungi `test_test_lifecycle_actions_defaults`), `tests/test_feature_visibility_service.py` se elenca le chiavi
- Modify: dove si inizializza la visibilità delle feature, per avere `test_lifecycle` **disabilitata di default** (verificare in `app/services/feature_visibility.py`)
- Modify: `openapi/qa-hub-be.json` (rigenerato)

- [ ] **Step 1: Test** `test_test_lifecycle_actions_defaults` (default esatti dei Global Constraints) e `test_test_lifecycle_feature_disabled_by_default`.
- [ ] **Step 2: Eseguire.** Atteso: FAIL. **Step 3: Implementare.** **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5:** `uv run --extra dev python -m scripts.generate_openapi`, poi `uv run --extra dev pytest -q` (tutta la suite) e `uv run ruff check .`. Atteso: tutto verde, OpenAPI contiene i path `/api/v1/test-lifecycle/*`.
- [ ] **Step 6: Commit** `feat(test-lifecycle): permissions, feature flag, openapi`.

### Task 8 (frontend): Tipi, hook e helper

**Files (in `qa-hub-frontend`):**
- Create: `src/types/test-lifecycle.ts` (specchio degli schemi `Tl*` del backend; `TransitionCheck`, `GherkinError`)
- Create: `src/hooks/useTestLifecycle.ts`, `src/hooks/useTestLifecycle.test.tsx`
- Create: `src/lib/test-lifecycle.ts`, `src/lib/test-lifecycle.test.ts`

**Interfaces:**
- Produces:
  - hook (query key con radice `["tl", …]`): `useTlScopes`, `useCreateTlScope`, `useUpdateTlScope`, `useTlFeatures(filters)`, `useTlFeature(id)`, `useCreateTlFeature`, `useUpdateTlFeature`, `useUpdateTlRevision`, `useAddTlScenario`, `useUpdateTlScenario`, `useRemoveTlScenario`, `useTlTransition`, `useTlHistory(id)`, `useTlComments(featureId, revId)`, `useAddTlComment`, `useResolveTlComment`, `useTlPreview(featureId, revId)`, `useValidateSteps()`. Le mutation iniettano `actor_email` dalla sessione (`useSession`, come `useDq`) e invalidano `["tl","feature",id]` e `["tl","features"]`;
  - `lib/test-lifecycle.ts`: `FEATURE_STATUS_LABEL`, `FEATURE_STATUS_COLOR`, `REVISION_STATUS_LABEL` (etichette italiane: Bozza, In review, Progettata, Implementata, In esecuzione), `TRANSITION_LABEL` (Invia in review, Approva, Richiedi modifiche, Riapri), `isStaleLockError(err: unknown): boolean` (vero per `ApiError` 409 con lock), `transitionBlockReasons(checks, action): string[]`.

- [ ] **Step 1: Test** degli hook (pattern `useQualityGate.test.tsx`, mock di `apiClient` e `next-auth/react`): `useAddTlScenario` fa POST sull'URL corretto con `actor_email` della sessione; `useTlTransition` invalida feature e lista; test sugli helper `isStaleLockError` e `transitionBlockReasons`.
- [ ] **Step 2:** `pnpm vitest run src/hooks/useTestLifecycle.test.tsx src/lib/test-lifecycle.test.ts`. Atteso: FAIL. **Step 3: Implementare.** **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): types, hooks and helpers`.

### Task 9 (frontend): Panoramica, lista Feature, Ambiti e navigazione

**Files:**
- Create: `src/app/(portal)/test-lifecycle/page.tsx`
- Create: `src/components/test-lifecycle/ScopeOverview.tsx` (una card per Ambito, raggruppate per area: conteggi per stato e numero di feature), `FeatureTable.tsx` (filtri Ambito/stato/owner, link al dettaglio), `CreateFeatureDialog.tsx` (Ambito, titolo, owner design precompilato con l'utente corrente), `ScopeManagerDialog.tsx` (crea/modifica; campo codice disabilitato quando `feature_count > 0`, con tooltip del motivo)
- Modify: `src/components/shell/Sidebar.tsx` (nuovo gruppo "Test Design", voce "Test Lifecycle", icona `ListChecks`, `action: "view:test_lifecycle"`, `featureKey: "test_lifecycle"`), `src/lib/features.ts` (route `/test-lifecycle` → `test_lifecycle`), `src/lib/features.test.ts`
- Test: `src/components/test-lifecycle/FeatureTable.test.tsx`, `ScopeManagerDialog.test.tsx`

- [ ] **Step 1: Test**: `FeatureTable` mostra codice, titolo e badge stato e filtra per stato; `ScopeManagerDialog` disabilita il codice quando l'Ambito ha feature; `features.test.ts` mappa `/test-lifecycle/abc` su `test_lifecycle`.
- [ ] **Step 2: Eseguire.** Atteso: FAIL. **Step 3: Implementare** (stile e componenti `ui/` come le pagine DQ; `Gate action="edit:tl_feature"` sul pulsante "Nuova feature", `Gate action="manage:tl_scopes"` su "Gestisci ambiti"). **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): overview, feature list and scopes UI`.

### Task 10 (frontend): Dettaglio Feature ed editor ibrido

**Files:**
- Create: `src/app/(portal)/test-lifecycle/[id]/page.tsx`
- Create: `src/components/test-lifecycle/FeatureHeader.tsx` (codice, titolo, badge stato, badge revisione, pulsanti di transizione da `transitions`: quelli non consentiti sono disabilitati con i motivi in tooltip/popover; `request_changes` apre un dialog con nota obbligatoria), `FeatureMetaPanel.tsx` (owner, reviewer, owner implementazione, chiavi Jira, Epic/Story o "reverse engineering", requisiti, stime), `ScenarioCard.tsx`, `StepsEditor.tsx` (textarea monospazio con numeri di riga ed evidenziazione delle righe in errore; componente nuovo, senza importare `components/bdd`), `ExamplesGrid.tsx` (intestazioni e righe modificabili, aggiungi/rimuovi riga/colonna), `FilePreview.tsx` (tab "Anteprima file": mostra `preview.file`, sola lettura, pulsante copia)
- Test: `ScenarioCard.test.tsx`, `FeatureHeader.test.tsx`

**Comportamento:**
- La scheda scenario mostra tag identità e numero in sola lettura. Titolo, tipo, polarità e tag aggiuntivi (multiselect da `ALLOWED_EXTRA_TAGS`) sono modificabili; gli step usano `StepsEditor`; la griglia Esempi compare solo se il tipo è `outline`.
- Validazione live: dopo 400 ms di inattività si chiama `useValidateSteps` e si mostrano gli errori per riga. Il salvataggio (PATCH) parte al blur o con "Salva" e invia `lock_version`.
- Con un 409 di lock (`isStaleLockError`) compare un avviso "La bozza è stata modificata da un altro utente. Ricarica per vedere le modifiche." con pulsante Ricarica, e il testo locale non viene perso finché l'utente non ricarica.
- Con la revisione non in `draft` tutto è in sola lettura.

- [ ] **Step 1: Test**:
  - `ScenarioCard` rende il tag in sola lettura;
  - mostra gli errori alla riga indicata;
  - nasconde Esempi per `scenario` e li mostra per `outline`;
  - mostra l'avviso su errore 409 di lock (Review Focus 1);
  - `FeatureHeader` disabilita "Invia in review" ed espone i motivi ricevuti.
- [ ] **Step 2: Eseguire.** Atteso: FAIL. **Step 3: Implementare.** **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): feature detail and hybrid scenario editor`.

### Task 11 (frontend): Review e Storico

**Files:**
- Create: `src/components/test-lifecycle/ReviewPanel.tsx` (commenti generali e per scenario, con badge sul numero di commenti aperti nella scheda scenario; aggiunta e risoluzione), `HistoryPanel.tsx` (timeline di eventi e versioni; per una versione, link "Vedi file" che apre il `file` dello snapshot; diff fra due versioni selezionate, riga per riga, con un algoritmo LCS semplice in `lib/test-lifecycle.ts::lineDiff(a: string, b: string): {type: "same"|"add"|"del", text: string}[]`)
- Modify: `src/app/(portal)/test-lifecycle/[id]/page.tsx` (tab Scenari · Review · Storico · Anteprima file), `src/lib/test-lifecycle.ts`, `src/lib/test-lifecycle.test.ts`
- Test: `ReviewPanel.test.tsx`

- [ ] **Step 1: Test**: `lineDiff` su casi di aggiunta, rimozione e invariato; `ReviewPanel` aggiunge un commento legato allo scenario selezionato e lo segna come risolto.
- [ ] **Step 2: Eseguire.** Atteso: FAIL. **Step 3: Implementare.** **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): review and history panels`.

### Task 12: Verifica finale end-to-end

- [ ] **Step 1: Backend:** `uv run --extra dev pytest -q && uv run ruff check .`. Atteso: tutto verde.
- [ ] **Step 2: Frontend:** `pnpm test && pnpm lint && pnpm build`. Atteso: tutto verde.
- [ ] **Step 3: Prova manuale** con backend e frontend in locale e la feature flag attivata da Superadmin:
  1. crea l'Ambito `FdR3` (area FdR);
  2. crea una Feature;
  3. aggiungi due scenari, uno outline con Esempi;
  4. verifica che "Invia in review" elenchi i motivi;
  5. compila reviewer e `PQ-1`;
  6. invia;
  7. come reviewer richiedi modifiche, poi approva;
  8. controlla Storico e Anteprima file. L'anteprima deve corrispondere al formato di spec §7.
  Riportare screenshot e risultati.
- [ ] **Step 4:** nessun commit se non ci sono modifiche. Altrimenti `fix(test-lifecycle): …`.
