# Test Lifecycle — Fase 5 (Migrazione da Confluence) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, scelto dall'utente per risparmiare crediti). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** una pagina "Migrazione" nell'hub che analizza le pagine FEAT di Confluence e i feature file su Git, fa confermare/correggere le proposte riga per riga ed esegue la migrazione per ambito: feature nell'hub, alias dei vecchi tag, avviso sulle pagine Confluence, PR di allineamento.

**Architecture:** un client Confluence dedicato (httpx, credenziali Atlassian già presenti) e un parser puro del formato storage delle pagine; un servizio di analisi che salva righe di migrazione (proposta + problemi + decisione dell'utente) e le ricostruisce senza perdere le decisioni; un servizio di esecuzione per ambito che crea le feature con i servizi esistenti, scrive i pannelli Confluence (solo se abilitato) e apre la PR di allineamento con il client Git esistente; il sync esistente chiude il giro al merge.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic 0040, httpx + `MockTransport`, `html.parser` e `difflib` della libreria standard; Next.js 16, TanStack Query, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-21-test-lifecycle-migration-design.md` (estende `2026-11-20-test-lifecycle-design.md` §12).

## Global Constraints

- Permesso nuovo `manage:tl_migration` (default: solo `qa_manager`).
- Radice Confluence `tl_confluence_root_page_id = "2815950851"`; pagine incluse: titolo che inizia con `FEAT_`.
- Scrittura su Confluence solo se `tl_migration_confluence_write = True` (default **False**: in locale e nei test non si scrive mai); solo sulle pagine migrate.
- Nessuna scrittura diretta su `main`: solo il branch `qa-hub/migration-<scope_code>` e una PR verso `tl_base_branch`. In locale vale la modalità Git locale.
- Mapping stati: `test suite implemented` → IMPLEMENTED; `approvato` → DESIGNED; tutto il resto → DRAFT (confronto case-insensitive, spazi normalizzati); file senza pagina → IMPLEMENTED.
- Tag di categoria iniziali: `runnable`, `e2e`, `ui`, `skip`; i tag non identità trovati in migrazione sono mantenuti per default.
- Nessuna chiamata reale a Confluence, GitHub o Jira nei test (fake e `MockTransport`).
- Ogni riga ricorda `page_id` e/o `file_path`: una nuova analisi conserva le decisioni delle righe con la stessa chiave; l'esecuzione salta le righe già eseguite.

## Review Focus

1. Pagina modificata dopo l'analisi (versione Confluence diversa) o file cambiato (blob diverso) → all'esecuzione la riga torna `pending` con il problema "cambiata dopo l'analisi", nulla creato.
2. Riesecuzione dello stesso ambito → nessuna feature doppia, nessuna seconda PR se quella aperta esiste.
3. Errore su Confluence (pannello) o Git (PR) a metà esecuzione → le feature create restano, l'errore è salvato sull'ambito e c'è "Riprova".
4. Tabelle Confluence irregolari (celle vuote, step senza prefisso, scenario senza tabella) → nota di migrazione, mai eccezione.
5. Due righe confermate con lo stesso numero nello stesso ambito → esecuzione bloccata con messaggio, nulla creato.

---

### Task 1: Modello dati, permesso, impostazioni

**Files:** `alembic/versions/0040_test_lifecycle_migration.py`, `app/models/test_lifecycle.py`, `app/core/permissions.py`, `app/config.py`, `tests/test_tl_migration_models.py`.
- `tl_migration_items`: `id`, `page_id` String(32) null, `page_version` int null, `page_title` String(500) null, `file_path` String(1024) null, `file_blob_sha` String(64) null, `scope_code` String(20), `number` int null, `title` String(500), `status` (`tl_feature_status`), `decision` String(16) (`pending|confirmed|excluded`, default `pending`), `problems` JSONB `[]` (lista `{code, message}`), `proposal` JSONB `{}` (owner/reviewer account e nomi, `owner_id`, `reviewer_id`, chiavi Jira, `requirements_url`, `description`, `impl_estimate_days`, `area`, `repo_folder`, scenari estratti dalla pagina, `note`), `overrides` JSONB `{}` (correzioni dell'utente, vincono sulla proposta), `feature_id` FK null, `executed_at` null, `created_at`, `updated_at`. Unici parziali su `page_id` e su `file_path` (non null).
- `tl_migration_scopes`: `scope_code` PK, `executed_at`, `pr_number`, `pr_url`, `pr_state`, `pr_error`, `confluence_errors` JSONB `[]`, `updated_at`.
- `tl_migration_tags`: `name` PK, `occurrences` int, `decision` String(8) (`keep|drop`, default `keep`).
- `tl_category_tags`: `name` PK, `created_at`; seed `runnable`, `e2e`, `ui`, `skip`.
- `tl_migration_state`: riga singola (`id=1`), `analyzed_at`, `status` (`idle|running|error`), `error`.
- `tl_features.migration_note` Text null, `tl_features.migrated_from` JSONB null (`{page_id, page_url, file_path, commit_sha}`), `tl_features.alignment_pending` bool default false.
- Settings: `tl_confluence_root_page_id: str = "2815950851"`, `tl_confluence_space: str = "QDP"`, `tl_migration_confluence_write: bool = False`.
- Permesso `manage:tl_migration` ("Test Lifecycle (migrazione da Confluence)", default solo `qa_manager`).
- [ ] Test: modelli scrivibili, unici su `page_id`/`file_path`, seed dei tag di categoria, permesso nel catalogo. → **Commit** `feat(test-lifecycle): migration data model`.

### Task 2: Tag di categoria estendibili

**Files:** `app/services/test_lifecycle/category_tags.py`, `gherkin.py`, `feature_parser.py`, `git_import.py`, `features.py`, `app/schemas/test_lifecycle.py`, `app/api/v1/test_lifecycle.py`, `tests/test_tl_category_tags.py`.
- `async category_tags(db) -> tuple[str, ...]` (ordinati; i 4 iniziali sempre presenti).
- `parse_feature_file(text, allowed_extra: Iterable[str] = ALLOWED_EXTRA_TAGS)`; l'import Git passa `await category_tags(db)`.
- Lo schema di `extra_tags` controlla solo il formato (`^[A-Za-z][A-Za-z0-9_-]{0,49}$`); l'appartenenza alla lista la verifica il servizio (`unknown_extra_tag` 422).
- `GET /test-lifecycle/category-tags` → `list[str]`.
- [ ] Test: tag aggiunto alla tabella accettato su uno scenario e letto dal parser; tag sconosciuto → 422; endpoint. → **Commit** `feat(test-lifecycle): extensible category tags`.

### Task 3: Client Confluence

**Files:** `app/services/test_lifecycle/confluence_client.py`, `tests/test_tl_confluence_client.py`.
- `TlConfluenceError(status, message)`, `TlConfluenceNotConfigured`; `get_confluence_client()` da `jira_base_url`/`jira_email`/`jira_api_token`.
- `TlConfluenceClient(base_url, email, token, transport=None)`:
  - `async list_feat_pages(root_id) -> list[PageRef(id, title, version)]` (`GET /wiki/rest/api/content/search` con CQL `ancestor = <root> AND type = page`, paginato; filtro titolo `FEAT_`);
  - `async get_page(id) -> PageContent(id, title, version, storage, url)` (`expand=body.storage,version`);
  - `async get_user(account_id) -> AtlassianUser(account_id, display_name, email|None)` (`GET /rest/api/3/user?accountId=`), con cache in memoria;
  - `async prepend_panel(page: PageContent, panel_html) -> int` (`PUT /wiki/rest/api/content/{id}` con `version.number = page.version + 1`; 409 → `TlConfluenceError(409)`); rifiuta se `settings.tl_migration_confluence_write` è falso (`TlConfluenceError(403, "Scrittura su Confluence disattivata")`).
- [ ] Test con `MockTransport`: paginazione e filtro titoli, parsing pagina, utente con email nascosta, PUT con versione+1, 409, scrittura disattivata, errore di rete → 502. → **Commit** `feat(test-lifecycle): confluence client`.

### Task 4: Parser delle pagine FEAT

**Files:** `app/services/test_lifecycle/confluence_page.py`, `tests/fixtures/confluence/feat_wip.xml`, `feat_implemented.xml`, `feat_irregular.xml`, `tests/test_tl_confluence_page.py`.
- `parse_feat_page(storage: str) -> FeatPage` (puro, `html.parser`): `code`, `doc_name`, `area`, `status_text`, `owner_accounts: list[str]`, `reviewer_accounts`, `reviewer_label` (testo/team), `requirements_url`, `jira_design`, `jira_impl`, `product_key` (chiavi `[A-Z][A-Z0-9]+-\d+`; "TBD" → None), `estimate_days` (numero da "Test Automatici"), `description` (primo titolo/paragrafo sotto "Dettagli"), `feature_file_url`, `background_steps`, `scenarios: list[PageScenario(number, title, steps)]`, `note` (testo non interpretato).
- Step degli scenari: celle Given/When/Then → righe; voci "- Dato …" / "- E …" separate; prima riga di Given senza prefisso → "Dato …", di When → "Quando …", di Then → "Allora …"; "E" mantenuto.
- `map_status(text) -> TlFeatureStatus` secondo i Global Constraints.
- Fixture costruite sulla struttura reale (tabelle "Informazioni generali" e "Dettagli", `ri:user ri:account-id`, titoli "Scenario NN: …"), con contenuti sintetici.
- [ ] Test: campi della pagina WIP; stato e Jira della pagina implemented; pagina irregolare → scenari parziali + nota, nessuna eccezione; `map_status`. → **Commit** `feat(test-lifecycle): confluence feat page parser`.

### Task 5: Analisi

**Files:** `app/services/test_lifecycle/migration.py`, `app/schemas/test_lifecycle.py`, `tests/tl_fakes.py` (`FakeConfluenceClient`), `tests/test_tl_migration_analysis.py`.
- Funzioni pure: `code_parts(code) -> (scope_code, number) | None` (`FEAT_(\d{3})_([A-Za-z][A-Za-z0-9]*)`), `title_score(a, b) -> float` (`difflib.SequenceMatcher` su testo normalizzato), `match_pages_files(pages, files) -> list[(page|None, file|None)]` (link "Feature File" della pagina → codice+ambito dal tag feature o dal nome file → miglior titolo ≥ 0.6, ognuno usato una volta).
- `async analyze(db, confluence, git) -> TlMigrationSummary`: lock advisory `tl_migration`; legge pagine (`list_feat_pages` + `get_page`) e `.feature` su `main` (`list_tree` + `get_file`, parse con `parse_feature_file`); risolve persone (`get_user` → utente hub per email, poi per nome normalizzato); conta i tag non identità in `tl_migration_tags`; crea/aggiorna le righe per chiave (`page_id`, altrimenti `file_path`), conservando `decision` e `overrides`; le righe non eseguite che non esistono più vengono rimosse; aggiorna `tl_migration_state`.
- Problemi (`code`): `number_conflict` (stesso ambito e numero su più righe o già nell'hub), `page_without_file`, `file_without_page`, `multiple_files`, `duplicate_identity_tag`, `parse_error`, `owner_not_found`, `reviewer_not_found`, `reviewer_is_team`, `likely_duplicate_page` (titolo che inizia con "Test "), `fixture_or_copy` (percorso con `/tas_` o `docs/use-cases/`), `no_code` (file senza tag/nome FEAT: ambito e numero da scegliere).
- [ ] Test con fake Confluence + `FakeGitClient`: abbinamento per link, per codice, per titolo; doppio FEAT_005 → `number_conflict` su entrambe; mapping stati; persone; seconda analisi conserva decisione e override; riga sparita rimossa; tag contati. → **Commit** `feat(test-lifecycle): migration analysis`.

### Task 6: Esecuzione per ambito

**Files:** `migration.py`, `app/services/test_lifecycle/migration_align.py`, `tests/test_tl_migration_execute.py`.
- `effective(item) -> dict` (proposta + override).
- `async execute_scope(db, scope_code, actor, *, git, confluence) -> TlMigrationScopeOut`: lock advisory `tl_migration`; righe `confirmed` non eseguite dell'ambito; blocchi (`MigrationBlocked(reasons)` → 409 `migration_blocked`): numeri doppi, numero già usato nell'hub, righe senza ambito/numero. Rilettura di pagine e file: versione/blob cambiati → riga `pending` + problema `changed_since_analysis`, saltata.
  - Chiavi Jira verificate prima di scrivere con `jira_tasks.verify_keys` (come in fase 3: Jira giù o non configurato → chiave salvata non verificata; chiave inesistente → problema `jira_key_not_found` e riga `pending`).
  - Per riga: ambito creato se manca (codice, nome = codice, area, cartella); feature con codice e numero dati, `next_feature_number = max + 1`; revisione numero 1 con metadati; scenari (da file: tag nuovi `@<SCOPE>_<NNN>_<NN>` nell'ordine del file, extra tag = tag `keep` non identità e non polarità; da pagina: scenari estratti); stato: IMPLEMENTED/DESIGNED con file → revisione pubblicata + versione storico origine `migration`, `git_path` = percorso, `alignment_pending = True`; DRAFT → revisione di lavoro; alias = tag identità vecchi (feature e scenario) senza `@`, ignorati se già usati; `migration_note`, `migrated_from`; evento `tl.feature.migrated`. Un commit per riga (`feature_id`, `executed_at`).
  - Poi pannello Confluence per le pagine eseguite (`prepend_panel` con link `tl_hub_base_url/test-lifecycle/<id>`), errori in `confluence_errors` (scrittura disattivata → voce "non scritta: disattivata").
  - Poi PR di allineamento (`migration_align.open_alignment_pr`): branch `qa-hub/migration-<scope>` da `main`, per ogni file eseguito `put_file` col contenuto rigenerato (`history.build_render` + `render_feature_file`) e `blob_sha` letto; PR "Allineamento QA Hub — <scope>" con tabella tag vecchio → nuovo e commenti persi per file (`lost_comments(original, rendered)`: righe `#` non header/lingua assenti nel nuovo); PR aperta già esistente → riusata; errori in `pr_error`.
- `async retry_alignment(db, scope_code, git)` e `async retry_confluence(db, scope_code, confluence)`.
- [ ] Test: esecuzione crea feature/scenari/alias/eventi e stati corretti; riesecuzione non duplica e riusa la PR; numero doppio → 409 nulla creato; pagina cambiata → riga pending; errore Git → feature create + `pr_error` + retry ok; scrittura Confluence disattivata → nessuna PUT, errore registrato; `lost_comments`; tag `drop` rimosso, `keep` aggiunto ai tag di categoria. → **Commit** `feat(test-lifecycle): migration execution per scope`.

### Task 7: Chiusura al merge

**Files:** `git_sync.py`, `git_import.py`, `tests/test_tl_migration_sync.py`.
- Nel sync: per gli ambiti con `pr_state = open`, `get_pull`; merged → `pr_state=merged`, `alignment_pending=False` per le feature dell'ambito, evento `tl.feature.aligned`; closed → `pr_state=closed`, `pr_error="PR chiusa senza merge"`.
- L'import del file mergiato (header ora presente) trova la feature e non apre anomalie strutturali (contenuto uguale alla pubblicata).
- Una feature migrata DESIGNED con file su `main` diventa IMPLEMENTED al merge dell'allineamento per l'unica regola di promozione esistente (nessuna anomalia bloccante).
- Finché `alignment_pending` è vero, un file senza header nello stesso percorso non genera anomalie.
- [ ] Test con `FakeGitClient`: merge della PR → feature non più in attesa, nessuna anomalia, versione `git` non duplicata; PR chiusa → errore. → **Commit** `feat(test-lifecycle): close migration on alignment merge`.

### Task 8: API e OpenAPI

**Files:** `app/api/v1/test_lifecycle.py`, `app/schemas/test_lifecycle.py`, `tests/test_tl_migration_api.py`, `openapi/qa-hub-be.json`, `.env.example`.
- Tutte con `actor_email` e permesso `manage:tl_migration` (403 altrimenti):
  - `POST /migration/analyze` → `TlMigrationSummary` (409 `migration_in_progress`; 503 se Confluence o Git non configurati);
  - `GET /migration` → `TlMigrationSummary` (stato, conteggi per ambito e per decisione, ambiti con stato PR/Confluence);
  - `GET /migration/items?scope_code=&decision=&problem=` → `list[TlMigrationItemOut]`;
  - `PATCH /migration/items/{id}` (`decision`, `overrides`: `scope_code`, `number`, `title`, `status`, `owner_id`, `reviewer_id`, `file_path`) → item (409 se già eseguita);
  - `GET/PATCH /migration/tags` (decisione keep/drop);
  - `POST /migration/scopes/{code}/execute`, `POST /migration/scopes/{code}/retry-pr`, `POST /migration/scopes/{code}/retry-confluence`.
- `TlFeatureDetail` + `migration_note`, `migrated_from`, `alignment_pending`.
- [ ] Test endpoint, permessi, errori; suite completa (11 preesistenti), ruff, OpenAPI solo aggiunte, `.env.example` (`TL_CONFLUENCE_*`, `TL_MIGRATION_CONFLUENCE_WRITE=false`). → **Commit** `feat(test-lifecycle): migration api`.

### Task 9 (frontend): pagina Migrazione

**Files:** `src/types/test-lifecycle.ts`, `src/hooks/useTestLifecycle.ts`, `src/app/(portal)/test-lifecycle/migrazione/page.tsx` (+ test), `src/components/test-lifecycle/MigrationItemsTable.tsx`, `MigrationItemDialog.tsx`, `MigrationTagsPanel.tsx`, `MigrationScopeCard.tsx` (+ test), link nella pagina Test Lifecycle (solo con `manage:tl_migration`), `ExtraTagsPicker.tsx` (lista da `GET /category-tags`).
- Pagina: "Analizza" (con data dell'ultima analisi), riepilogo per ambito (righe da decidere / confermate / escluse / eseguite, stato PR con link, errori Confluence, pulsanti "Esegui ambito", "Riprova PR", "Riprova Confluence"); tabella righe con filtri (ambito, decisione, problema), problemi come badge, link pagina/file (solo http(s)); dialog di correzione (ambito, numero, titolo, stato, owner, reviewer, file) con "Conferma" / "Escludi"; pannello tag (mantieni/rimuovi); "Conferma tutte senza problemi" per ambito; avviso quando la scrittura su Confluence è disattivata.
- [ ] TDD: analizza; filtri; dialog invia override e decisione; esecuzione bloccata mostra i motivi; stato PR e retry; tag; link solo http(s); pagina nascosta senza permesso. → **Commit** `feat(test-lifecycle): migration page`.

### Task 10 (frontend): feature migrate e guida

**Files:** `FeatureHeader.tsx` o pannello dedicato, `src/lib/test-lifecycle.ts`, guida, test.
- Dettaglio feature: badge "Migrata da Confluence" con link alla pagina (http(s)), "Allineamento Git in attesa" finché `alignment_pending`, nota di migrazione visibile e modificabile da chi può modificare.
- Etichette eventi `tl.feature.migrated`, `tl.feature.aligned`; guida: sezione "Migrazione da Confluence" (analisi, decisioni, esecuzione per ambito, PR di allineamento, pagine Confluence).
- [ ] TDD: badge e nota; etichette; sezione guida. → **Commit** `feat(test-lifecycle): migrated features and guide`.

### Task 11: Verifica finale

- [ ] Backend `pytest -q` (11 preesistenti), frontend test + lint + build.
- [ ] Prova locale: Git in modalità locale, Confluence reale **in sola lettura** (`TL_MIGRATION_CONFLUENCE_WRITE=false`); l'esecuzione la fa l'utente dalla pagina. Nessun push.
