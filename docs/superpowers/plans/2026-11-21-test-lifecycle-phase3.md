# Test Lifecycle — Fase 3 (Jira) Implementation Plan

> **Esecuzione:** inline in sessione (superpowers:executing-plans), senza subagent — richiesta dell'utente per i costi. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** L'hub crea e collega gli elementi Jira del processo QA: l'**Epic QA** dell'Ambito su richiesta, il task **Progettazione** su richiesta in bozza, il task **Implementazione suite** in automatico all'approvazione, entrambi nel progetto PQ, figli dell'Epic QA dell'Ambito, con componente, label `testing`, assegnatario e link "relates to" all'Epic/Story di prodotto. Verifica che le chiavi inserite esistano e mostra lo stato aggiornato dei task collegati.

**Architecture:** Nuovo client Jira in scrittura `app/services/test_lifecycle/jira_client.py`, servizio `jira_tasks.py` (creazione Epic QA e task, verifica, link, aggiornamento stati), cache dello stato dei task sulla Feature, job Celery di aggiornamento. Frontend: sezione Jira nel pannello metadati, campi Epic QA e componente negli Ambiti con pulsante "Crea Epic QA", guida aggiornata.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Celery, httpx, Jira Cloud REST API v3 (`/rest/api/3/issue`, `/issueLink`, `/user/search`, `/search/jql`); Next.js 16, React 19, TanStack Query.

**Spec:** `qa-hub-backend/docs/superpowers/specs/2026-11-20-test-lifecycle-design.md` §8 (Integrazione Jira). Codice esistente: fasi 1 e 2 (`2026-11-20-test-lifecycle-phase1.md`, `-phase2.md`).

**Repo e branch:** `qa-hub-backend` (Task 1–7) e `qa-hub-frontend` (Task 8–10), su `feature/test-lifecycle`, **solo in locale, nessun push**.

## Decisioni già prese

- **Epic QA e componente**: predefiniti nell'**Ambito** (`jira_qa_epic_key`, `jira_component`); la Feature può sovrascrivere l'Epic (`jira_qa_epic_key` sulla feature). Risposta dell'utente.
- **Link al prodotto**: i task PQ creati sono collegati all'Epic/Story di prodotto con link Jira **"Relates"**. Risposta dell'utente.
- **Tipi di issue** (verificati su PQ-834/PQ-835): Progettazione → `Progettazione Scenari e casi di test`; Implementazione → `Implementazione suite di TA`. Label `testing`.
- **Lo stato Jira non guida lo stato della Feature** (spec §8): è solo mostrato.
- **Epic QA dell'Ambito: esistente o nuova** (richiesta dell'utente). Nella gestione Ambiti si può (a) **scegliere un'Epic QA già esistente** su Jira, cercandola per nome tra le Epic di PQ o inserendo la chiave, oppure (b) **crearne una nuova** con il pulsante "Crea Epic QA". In entrambi i casi la chiave diventa la `jira_qa_epic_key` dell'Ambito. Una chiave inserita a mano viene verificata: deve esistere, essere del progetto PQ ed essere di tipo Epic.
- **Nessuna modalità Jira locale** (richiesta dell'utente): anche in locale si usa il progetto Jira **PQ** reale; le issue create durante le prove le cancella l'utente. Questo è l'unico permesso di scrittura su Jira dato per le prove: solo progetto PQ.
- Credenziali: si riusano `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` già presenti.

## Global Constraints

- Migrazione `0038`, `down_revision = "0037"`; schema `qachub`, enum come nelle fasi precedenti.
- Settings nuovi: `tl_jira_project: str = "PQ"`, `tl_jira_design_issue_type: str = "Progettazione Scenari e casi di test"`, `tl_jira_impl_issue_type: str = "Implementazione suite di TA"`, `tl_jira_label: str = "testing"`, `tl_jira_link_type: str = "Relates"`, `tl_jira_refresh_interval_s: float = 900.0`, `tl_jira_epic_issue_type: str = "Epic"`.
- Titoli: task `[<codice feature>] Progettazione — <titolo>` e `[<codice feature>] Implementazione suite — <titolo>` (trattino lungo `—`); Epic QA `[QA Epic] <nome Ambito>` (stesso formato delle Epic esistenti, es. PQ-495). Troncati a 255 caratteri.
- Descrizione (ADF): una riga "Feature progettata su QA Hub: <link>" con `{tl_hub_base_url}/test-lifecycle/<id>` se configurato, più l'elenco dei tag degli scenari.
- Assegnatario: accountId Jira trovato cercando l'email dell'owner della fase (`/rest/api/3/user/search?query=<email>`, primo utente attivo con email uguale). Non trovato → task non assegnato e avviso nella risposta (`jira_warning`), mai un errore.
- Formati chiave: Epic QA `^PQ-\d+$` (progetto `tl_jira_project`), Epic/Story prodotto `^[A-Z][A-Z0-9]+-\d+$` (come fase 1), componente testo libero ≤100 caratteri.
- Errori HTTP nuovi (`detail: {code, message}`): `jira_not_an_epic` (422), `jira_not_configured` (503), `jira_error` (502, messaggio di Jira), `jira_issue_not_found` (422, con la chiave), `jira_task_exists` (409), `jira_task_not_allowed` (409), `jira_in_progress` (409).
- Tipo di anomalia nuovo `jira_creation_failed` (aggiunto a `tl_issue_kind` nella migrazione 0038), usato quando la creazione automatica del task Implementazione fallisce.
- Eventi nuovi: `tl.jira.task_created`, `tl.jira.task_linked`, `tl.jira.refreshed` (solo se cambia qualcosa). La creazione dell'Epic QA di un Ambito si registra nei log applicativi (gli Ambiti non hanno storico).
- Nessuna chiamata Jira con lock di riga tenuti: stesso schema della fase 2 (lock advisory per feature `hashtext('tl_jira:'||feature_id)`, lock di riga brevi prima e dopo la rete).
- Testi UI in italiano; link Jira resi cliccabili solo se http(s).

## Review Focus

1. **Task doppi dopo un timeout**: Jira crea il task ma la risposta va persa. Prima di creare si cerca via JQL un task del progetto con lo stesso titolo `[<codice>] Progettazione …` (o Implementazione) e lo si riusa. Test: Task 4 `test_create_reuses_existing_task_found_by_jql`.
2. **Jira giù mentre si salva una chiave**: il salvataggio non si blocca; la chiave resta "non verificata" e viene verificata al prossimo aggiornamento. Solo un 404 certo rifiuta la chiave. Test: Task 3 `test_save_key_when_jira_down_is_accepted_unverified`.
3. **Owner senza account Jira** (email diversa): task creato senza assegnatario, con avviso. Test: Task 4 `test_unknown_assignee_creates_unassigned_with_warning`.
4. **Epic QA sbagliata** (non esiste o non è un'Epic): Jira risponde 400; l'errore arriva all'utente con il messaggio di Jira, la feature non resta in stato incoerente. Test: Task 4 `test_jira_400_surfaces_message_and_keeps_feature_unchanged`.
5. **Doppio clic su "Crea task Progettazione" o "Crea Epic QA"**: un solo elemento. Test: Task 4 `test_concurrent_create_second_gets_409`, Task 4 `test_concurrent_epic_create_second_gets_409`.

---

### Task 1: Client Jira in scrittura

**Files:**
- Modify: `app/config.py` (settings dei Global Constraints)
- Create: `app/services/test_lifecycle/jira_client.py`
- Modify: `tests/tl_fakes.py` (`FakeJiraClient`)
- Test: `tests/test_tl_jira_client.py`

**Interfaces:**
- Produces (`jira_client.py`):
  - `TlJiraError(status: int, message: str)`, `TlJiraNotConfigured`
  - `@dataclass(frozen=True) JiraIssue(key, summary, status, status_category: "new"|"indeterminate"|"done", issue_type, url)`
  - `class TlJiraClient(base_url, email, token, transport=None)` con metodi async: `get_issue(key) -> JiraIssue | None` (404 → None), `find_account_id(email) -> str | None`, `search_summary(project, prefix) -> JiraIssue | None` (JQL `project = X AND summary ~ "\"<prefix>\""`, primo risultato il cui summary inizia con il prefisso), `create_issue(*, project, issue_type, summary, description_adf, labels, parent_key, component, assignee_id) -> JiraIssue`, `create_link(link_type, inward_key, outward_key) -> None`.
  - `create_issue` accetta `parent_key=None` (per l'Epic QA) e `issue_type` per nome (`Epic` per l'Epic QA).
  - `search_epics(project, text, limit=20) -> list[JiraIssue]`: JQL `project = X AND issuetype = Epic AND summary ~ "<text>*" ORDER BY updated DESC` (testo vuoto: le Epic più recenti); il testo viene ripulito dai caratteri speciali JQL.
  - `get_jira_client()`: richiede `jira_base_url`, `jira_email`, `jira_api_token` (vuoti → `TlJiraNotConfigured`).
  - `issue_url(key) -> str`: `{jira_base_url}/browse/{key}`.

- [ ] **Step 1: Test** (`httpx.MockTransport`): `test_get_issue_maps_status_category`, `test_get_issue_404_returns_none`, `test_find_account_id_matches_email_exactly`, `test_create_issue_payload` (progetto, tipo per nome, summary, label, parent, componente, assegnatario, descrizione ADF), `test_create_link_payload`, `test_search_summary_prefix`, `test_error_carries_jira_message`, `test_create_epic_without_parent`, `test_search_epics_jql_and_escaping`, `test_not_configured_raises`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): jira write client`.

### Task 2: Modelli e migrazione 0038

**Files:**
- Modify: `app/models/test_lifecycle.py`; Create: `alembic/versions/0038_test_lifecycle_jira.py`
- Modify: `app/schemas/test_lifecycle.py` (scope create/update/out con `jira_qa_epic_key`, `jira_component`; feature update con `jira_qa_epic_key`)
- Test: `tests/test_tl_models_jira.py`, estensione di `tests/test_tl_scopes.py`

**Interfaces:**
- `TlScope` + `jira_qa_epic_key: str|None` (String(32)), `jira_component: str|None` (String(100)).
- `TlFeature` + `jira_qa_epic_key: str|None` (override), `jira_cache: dict` (JSONB, default `{}`: `{"<chiave>": {"summary", "status", "category", "issue_type", "url", "verified": bool, "checked_at"}}`), `jira_error: str|None` (Text).
- Enum `tl_issue_kind` + valore `jira_creation_failed` (`ALTER TYPE … ADD VALUE`; il downgrade non può togliere il valore: documentarlo e lasciarlo).
- Validazione schema: Epic QA `^PQ-\d+$` (prefisso da `tl_jira_project`), componente ≤100 caratteri trimmed.
- Salvataggio dell'Epic QA di un Ambito (create/update) e dell'override sulla feature: verifica su Jira prima di salvare; inesistente → 422 `jira_issue_not_found`; esistente ma non Epic o di un altro progetto → 422 `jira_not_an_epic`; Jira giù → salvata come non verificata (come le altre chiavi).

- [ ] **Step 1: Test**: default dei nuovi campi; scope API accetta e restituisce Epic QA e componente; formato Epic QA errato → 422.
- [ ] **Step 2–4:** FAIL → implementare (migrazione reversibile tranne il valore enum) → PASS (`alembic upgrade head && downgrade -1 && upgrade head`).
- [ ] **Step 5: Commit** `feat(test-lifecycle): jira fields and migration`.

### Task 3: Verifica delle chiavi e cache degli stati

**Files:**
- Create: `app/services/test_lifecycle/jira_tasks.py` (prima parte)
- Modify: `app/services/test_lifecycle/features.py` (`update_feature`, `update_revision`), `app/api/v1/test_lifecycle.py`, `app/schemas/test_lifecycle.py`
- Test: `tests/test_tl_jira_tasks.py`

**Interfaces:**
- `async verify_keys(client, keys: list[str]) -> dict[str, dict]`: per ogni chiave `get_issue`; `None` → solleva `JiraIssueNotFound(key)`; errore di rete/5xx o client non configurato → voce `{"verified": False}` senza errore.
- Salvataggio di `jira_design_key`, `jira_impl_key`, `jira_qa_epic_key` (feature) e `product_issue_key` (revisione): la verifica avviene **prima** di prendere i lock di riga; chiave inesistente → 422 `jira_issue_not_found`; le voci verificate entrano in `jira_cache`.
- `async refresh_feature(db, feature_id, client) -> TlFeatureDetail`: rilegge tutte le chiavi collegate (design, impl, Epic QA effettiva, prodotto della revisione di lavoro o pubblicata), aggiorna `jira_cache` ed evento `tl.jira.refreshed` solo se cambia qualcosa; endpoint `POST /features/{id}/jira/refresh` (`{actor_email}`).
- `TlFeatureDetail` + `jira: list[TlJiraLink]` con `TlJiraLink(role: "design"|"impl"|"qa_epic"|"product", key, summary, status, category, url, verified)` costruita da chiavi + cache, + `jira_error`, + `jira_qa_epic_effective` (feature o Ambito).

- [ ] **Step 1: Test**: `test_save_existing_key_caches_status`, `test_save_missing_key_returns_422`, `test_save_key_when_jira_down_is_accepted_unverified` (Review Focus 2), `test_product_key_verified_on_revision_update`, `test_refresh_updates_cache_and_event_only_on_change`, `test_detail_exposes_jira_links_with_effective_epic`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): verify jira keys and cache statuses`.

### Task 4: Creazione del task Progettazione e dell'Epic QA

**Files:**
- Modify: `app/services/test_lifecycle/jira_tasks.py`, `app/api/v1/test_lifecycle.py`
- Test: `tests/test_tl_jira_tasks.py`

**Interfaces:**
- `async create_task(db, feature_id, kind: "design"|"impl", actor: User, client) -> TlFeatureDetail`:
  1. lock advisory `tl_jira:<feature_id>` (occupato → 409 `jira_in_progress`);
  2. lock di riga breve: verifica che la chiave del ruolo sia vuota (altrimenti 409 `jira_task_exists`) e che lo stato lo consenta (design: revisione di lavoro `draft`; impl: revisione `approved` o feature `designed`/`implemented`; altrimenti 409 `jira_task_not_allowed`); legge titolo, codice, tag, owner della fase (design → `design_owner`, impl → `impl_owner`), Epic effettiva, componente, chiave prodotto; commit;
  3. senza lock: `search_summary` (Review Focus 1) → se trovato lo riusa; altrimenti `find_account_id` e `create_issue`; poi `create_link("Relates", <task>, <prodotto>)` se c'è una chiave prodotto (un errore sul link non annulla il task: va in `jira_warning`);
  4. rilock: salva la chiave, la cache, `jira_error=None`, evento `tl.jira.task_created` (o `tl.jira.task_linked` se riusato); commit.
  - Errore Jira al passo 3 → `jira_error` salvato, eccezione `TlJiraError` → 502 (Review Focus 4: nessuna chiave salvata).
- Endpoint `POST /features/{id}/jira/design-task` (`{actor_email}`) → `TlFeatureDetail` + `jira_warning: str|None`.
- `async create_scope_epic(db, scope_id, actor: User, client) -> TlScopeOut`: stesso schema (lock advisory `tl_jira_scope:<scope_id>`, lock breve, rete senza lock, rilock): richiede `jira_qa_epic_key` vuota (altrimenti 409 `jira_task_exists`); cerca prima un'Epic con summary `[QA Epic] <nome>` nel progetto (riuso); altrimenti crea l'issue di tipo `tl_jira_epic_issue_type`, label `testing`, componente dell'Ambito, senza assegnatario; salva la chiave sull'Ambito. Endpoint `POST /scopes/{id}/jira/epic` (`{actor_email}`; permesso UI `manage:tl_scopes`) → `TlScopeOut`.
- `GET /jira/epics?q=` → `[TlJiraLink]` (chiave, titolo, stato, url) per il selettore delle Epic esistenti; Jira non configurato → 503.

- [ ] **Step 1: Test** (con `FakeJiraClient`): `test_create_design_task_payload` (tipo, titolo, label, parent = Epic dell'Ambito, componente, assegnatario, descrizione con link e tag, link Relates al prodotto), `test_feature_epic_overrides_scope`, `test_create_reuses_existing_task_found_by_jql`, `test_unknown_assignee_creates_unassigned_with_warning`, `test_jira_400_surfaces_message_and_keeps_feature_unchanged`, `test_concurrent_create_second_gets_409`, `test_design_task_not_allowed_outside_draft`, `test_design_task_exists_409`, `test_without_jira_config_503`, `test_create_scope_epic_payload_and_saves_key`, `test_scope_epic_reuses_existing_by_summary`, `test_scope_epic_exists_409`, `test_concurrent_epic_create_second_gets_409`, `test_epics_search_endpoint`, `test_scope_existing_epic_verified_on_save`, `test_scope_epic_not_an_epic_422`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): create design task and scope qa epic on jira`.

### Task 5: Task Implementazione automatico all'approvazione

**Files:**
- Modify: `app/services/test_lifecycle/workflow.py` (dopo l'approvazione, accanto alla PR), `app/services/test_lifecycle/jira_tasks.py`, `app/api/v1/test_lifecycle.py`
- Test: `tests/test_tl_jira_tasks.py`, `tests/test_tl_workflow.py`

**Interfaces:**
- Dopo il commit dell'approvazione, se `jira_impl_key` è vuota: `create_task(kind="impl")` best effort (dopo la PR). Jira non configurato → nessun errore, `jira_error = "Jira non configurato"`. Errore Jira → `jira_error` + anomalia `jira_creation_failed` (una sola aperta per feature, `detail.key = "impl"`), risolta in automatico quando il task viene creato.
- Endpoint `POST /features/{id}/jira/impl-task` (`{actor_email}`) per riprovare.
- Assegnatario: `impl_owner`; se manca, non assegnato (con avviso).

- [ ] **Step 1: Test**: `test_approve_creates_impl_task_and_link`, `test_approve_jira_failure_keeps_approval_and_opens_issue`, `test_retry_impl_task_resolves_issue`, `test_approve_without_jira_config_sets_error_only`, `test_reapproval_does_not_create_second_impl_task`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): create implementation task on approval`.

### Task 6: Aggiornamento periodico degli stati

**Files:**
- Create: `app/tasks/sync_test_lifecycle_jira.py`; Modify: `app/tasks/schedule.py`, `app/services/test_lifecycle/jira_tasks.py`
- Test: `tests/test_tl_jira_refresh.py`

**Interfaces:**
- `async refresh_all(db, client, limit=200) -> int`: feature con almeno una chiave collegata, ordinate per `jira_cache` più vecchia; una richiesta per chiave distinta (chiavi uguali tra feature lette una volta sola); errori per singola chiave ignorati (la voce resta com'è); ritorna le feature aggiornate.
- Task Celery `app.tasks.sync_test_lifecycle_jira.refresh_jira` (beat ogni `tl_jira_refresh_interval_s`, `expires` uguale); non configurato → log e uscita; lock advisory `tl_jira_refresh` per non sovrapporsi.

- [ ] **Step 1: Test**: `test_refresh_all_dedups_keys`, `test_refresh_all_ignores_single_key_errors`, `test_task_not_configured_returns_status`, `test_beat_entry`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): periodic jira status refresh`.

### Task 7: OpenAPI e verifica backend

- [ ] **Step 1:** `uv run --extra dev python -m scripts.generate_openapi --apim-compatible` (solo aggiunte), `uv run --extra dev pytest -q` (solo gli 11 fallimenti preesistenti), `ruff` sui file toccati, `.env.example` con le variabili `TL_JIRA_*`.
- [ ] **Step 2: Commit** `feat(test-lifecycle): jira openapi`.

### Task 8 (frontend): Tipi e hook

**Files:** `src/types/test-lifecycle.ts`, `src/hooks/useTestLifecycle.ts`, test relativi.
- Tipi: `TlJiraLink`, campi `jira`, `jira_error`, `jira_qa_epic_effective` sul dettaglio, `jira_qa_epic_key`/`jira_component` su Ambito e feature; codici errore nuovi.
- Hook: `useCreateTlJiraTask()` (`{featureId, kind}` → POST design-task/impl-task, restituisce anche `jira_warning`), `useRefreshTlJira()`, `useCreateTlScopeEpic()` (`{scopeId}` → POST scopes/{id}/jira/epic, invalida gli ambiti), `useTlJiraEpics(q)` (attivo con ricerca debounced, `staleTime` 60 s); invalidano feature, issues, history.
- [ ] TDD come nelle fasi precedenti → **Commit** `feat(test-lifecycle): jira types and hooks`.

### Task 9 (frontend): Sezione Jira, Ambiti e guida

**Files:** `src/components/test-lifecycle/JiraLinks.tsx` (+ test), `FeatureMetaPanel.tsx`, `ScopeManagerDialog.tsx`, `src/app/(portal)/test-lifecycle/guida/page.tsx`, `LifecycleDiagram.tsx`.
- `JiraLinks`: per ogni ruolo (Progettazione, Implementazione, Epic QA, Prodotto) chiave come link (solo http/https), titolo, badge di stato colorato per categoria (Da fare / In corso / Fatto; "Non verificata" se `verified=false`).
- Pulsante **Crea task Progettazione** (revisione in bozza, chiave vuota, permesso `edit:tl_feature`); pulsante **Riprova task Implementazione** (approvata, chiave vuota, `jira_error` presente); **Aggiorna da Jira**; `jira_warning` e `jira_error` mostrati inline.
- Ambiti: campo **Epic QA** con selettore che cerca per nome tra le Epic esistenti di PQ (mostra chiave e titolo; si può anche scrivere la chiave), più il pulsante **Crea Epic QA** quando il campo è vuoto (crea l'Epic su PQ e compila il campo); campo componente Jira; pannello feature: campo Epic QA (sovrascrive quella dell'Ambito, placeholder con quella effettiva).
- Guida: nuova sezione "Jira" (cosa crea l'hub e quando, link al prodotto, stati) e nota nel diagramma ("task Jira" su Bozza e Progettata).
- [ ] TDD: link solo http(s); pulsanti visibili solo nei casi giusti; avviso assegnatario mostrato; campi Ambito inviati; "Crea Epic QA" chiama l'hook e mostra la chiave creata; scegliendo un'Epic dal selettore la sua chiave viene inviata al salvataggio; errore `jira_not_an_epic` mostrato sul campo → **Commit** `feat(test-lifecycle): jira section in feature and scopes`.

### Task 10: Verifica finale

- [ ] Backend `pytest -q` (11 preesistenti), frontend `pnpm test && pnpm lint && pnpm build`.
- [ ] Prova manuale **sul progetto PQ reale** (autorizzata dall'utente, che poi cancella le issue): Ambito → Crea Epic QA, crea task Progettazione, approva (task Implementazione automatico con link al prodotto), chiave inesistente (es. `PQ-999999`) rifiutata. Nessuna scrittura su progetti diversi da PQ; elencare all'utente tutte le chiavi create, per la pulizia.
- [ ] Nessun push.
