# Test Lifecycle — Fase 2 (Git) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collegare il Test Lifecycle al repo `pagopa/pagopa-platform-integration-test`. Una revisione approvata genera il `.feature` e apre una PR. Al merge l'hub importa il file, pubblica la revisione e porta la Feature in IMPLEMENTED. Le modifiche fatte direttamente su Git vengono versionate e classificate, e quelle strutturali aprono anomalie. Una bozza basata su una versione superata va riallineata con un confronto a tre. La CI del repo controlla le convenzioni e l'editor suggerisce gli step già esistenti.

**Architecture:** Nuovi servizi backend in `app/services/test_lifecycle/`: `git_client` (scrittura), `feature_parser` (inverso di `render_feature_file`), `git_publish` (PR), `git_import` (import e diff), `git_sync` (polling), `rebase`, `step_index`, `issues`. Un task Celery beat esegue il sync ogni `tl_sync_interval_s`, con un endpoint per lanciarlo a mano. Una nuova GitHub Action nel repo dei test controlla le convenzioni. Il frontend aggiunge tab Git, coda anomalie, dialog di riallineamento e autocompletamento.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic, Celery, httpx, `gherkin-official`; Next.js 16, React 19, TanStack Query; GitHub REST API v2022-11-28; Python stdlib + `unittest` per lo script CI.

**Spec:** `qa-hub-backend/docs/superpowers/specs/2026-11-20-test-lifecycle-design.md` §5–§8 (revisioni, workflow, integrazione Git). Il piano della fase 1 (`2026-11-20-test-lifecycle-phase1.md`) descrive il codice su cui si costruisce.

**Repo e branch:** tutti su `feature/test-lifecycle`, **solo in locale, nessun push** (decisione dell'utente: il push si fa alla fine dell'intero lavoro).
- `qa-hub-backend` (Task 1–10)
- `pagopa-platform-integration-test`: clonato in `/Users/cristiano.sticca/My Projects/pagopa-platform-integration-test` (Task 11)
- `qa-hub-frontend` (Task 12–16)

## Decisioni già prese

- **Sync via polling** (risposta dell'utente: non si sa se l'hub è raggiungibile da GitHub). Un job Celery gira ogni `tl_sync_interval_s` (default 300) e c'è il pulsante "Sincronizza ora". Niente webhook in questa fase.
- **Token: si riusa quello esistente.** Il backend ha già `TAS_GITHUB_TOKEN` (secret `pagopa-platform-tas-github-bot-pat`, il bot del repo dei test). Si aggiunge l'impostazione `tl_github_token`; se è vuota si usa `tas_github_token`. Il bot deve avere anche Contents RW e Pull requests RW sul repo: se oggi ha solo Actions, l'utente estende i permessi del PAT, senza modifiche al codice. Il sync gira nel **worker** Celery, i cui values helm oggi non montano `TAS_GITHUB_TOKEN`: il Task 8 lo aggiunge. Se non c'è nessun token, sync e PR sono disattivati con un messaggio esplicito.
- **File gestiti** = file `.feature` la cui prima riga è `# qa-hub: <codice>`. Gli altri file del repo sono ignorati fino alla migrazione (fase 5).
- **Il sync verso Confluence (`syncFeatureFile.yaml`) resta acceso.** Si spegne in fase 5, a migrazione conclusa (spec §12).
- **La PR non si mergia dall'hub.** Il merge resta su GitHub, con la review dei CODEOWNERS.

## Global Constraints

- Migrazione Alembic `0037`, `down_revision = "0036"`; tabelle nuove nello schema `qachub` con prefisso `tl_`; enum come nella fase 1 (`create_type=False`, tipo creato nella migrazione).
- Settings nuovi in `app/config.py`:
  - `tl_github_token: str = ""` (vuoto → si usa `tas_github_token`)
  - `tl_repo: str = "pagopa/pagopa-platform-integration-test"`
  - `tl_base_branch: str = "main"`
  - `tl_sync_interval_s: float = 300.0`
  - `tl_hub_base_url: str = ""` (URL pubblico del portale, usato nei link delle PR)
- Percorso del file: `{scope.repo_folder}/{feature.code}_{slug(title)}.feature`.
  - `slug` = titolo ASCII (accenti rimossi), caratteri non alfanumerici sostituiti da `_`, massimo 60 caratteri, senza `_` iniziali o finali.
  - Il percorso viene salvato in `feature.git_path` alla prima PR e **non cambia più** se cambia il titolo.
- Branch della PR: `qa-hub/{feature.code}-r{revision.number}`.
- Titolo della PR: `[{feature.code}] {title} (r{revision.number})`.
- Body della PR:
  - link `{tl_hub_base_url}/test-lifecycle/{feature.id}` se configurato;
  - autore e approvatore;
  - elenco dei tag degli scenari.
- Messaggio di commit: `test({scope.code}): {feature.code} r{revision.number}`.
- Nuovo stato del ciclo di vita di una PR, `pr_state`: `open` | `merged` | `closed` | `null` (nessuna PR).
- Classificazione delle differenze (spec §7):
  - **testuali**: step, titoli, esempi, descrizione, Contesto e tag aggiuntivi di scenari esistenti → nuova versione con origine `git`, nessuna anomalia;
  - **strutturali**: scenario senza tag identità, tag identità duplicato, tag con prefisso o numero di feature sbagliato, scenario rimosso, header assente o diverso → anomalia aperta;
  - **nuovo scenario** con tag identità valido e non noto → identità creata e anomalia `new_scenario_from_git` da riconoscere.
- Tipi di anomalia (`tl_issue_kind`): `structural_change`, `new_scenario_from_git`, `file_deleted`, `unknown_feature_code`, `parse_error`, `pr_closed_unmerged`, `pr_creation_failed`. Stati (`tl_issue_status`): `open`, `acknowledged`, `resolved`.
- Errori HTTP nuovi, stesso formato della fase 1 (`detail: {code, message}`):
  - `git_not_configured` (503);
  - `github_error` (502, con `message` dalla risposta GitHub);
  - `rebase_required` (409);
  - `sync_in_progress` (409).
- Il sync non gira mai due volte in parallelo: `pg_try_advisory_lock(hashtext('tl_git_sync'))`. Se non lo ottiene, l'endpoint risponde 409 `sync_in_progress` e il job salta il giro.
- Eventi di storico nuovi: `tl.pr.created`, `tl.pr.merged`, `tl.pr.closed`, `tl.git.imported`, `tl.revision.reopened`, `tl.revision.rebased`, `tl.issue.updated`.
- Permesso nuovo `manage:tl_git` (qa_manager, qa_engineer) per "Sincronizza ora", "Riprova PR" e la gestione delle anomalie. Riaprire una feature richiede `edit:tl_feature`.
- Testi UI e messaggi di errore in italiano. Il modulo BDD non va usato.

## Review Focus

1. **Creazione della PR interrotta a metà.** Il branch è creato ma il commit o la PR falliscono. "Riprova PR" deve essere idempotente: riusa il branch se esiste, aggiorna il file se c'è già, riusa la PR aperta con lo stesso head invece di crearne un'altra. Test: Task 4 `test_retry_reuses_existing_branch_and_pr`.
2. **Due sync in parallelo** (beat e pulsante manuale) non devono importare due volte né creare revisioni doppie. Test: Task 8 `test_concurrent_sync_second_gets_409`.
3. **File modificato su Git con CRLF, BOM o spazi finali.** Va normalizzato e non deve generare anomalie strutturali né versioni spurie se il contenuto è identico. Test: Task 3 `test_parse_normalizes_crlf_bom_trailing_spaces` e Task 6 `test_reimport_identical_content_creates_no_version`.
4. **File rinominato o spostato su Git** con lo stesso header. Si aggiorna `git_path` senza segnalare `file_deleted`. Test: Task 6 `test_moved_file_updates_git_path_without_issue`.
5. **PR modificata su GitHub prima del merge** (qualcuno ritocca il file sul branch). Si importa il contenuto mergiato, non il render approvato: le differenze testuali diventano una versione `git` e quelle strutturali un'anomalia. Test: Task 6 `test_merged_content_differs_from_approved_render`.

---

### Task 1: Client GitHub in scrittura e configurazione

**Files:**
- Modify: `app/config.py` (settings dei Global Constraints)
- Create: `app/services/test_lifecycle/git_client.py`
- Test: `tests/test_tl_git_client.py`

**Interfaces:**
- Produces:
  - `class TlGitError(Exception)` con `status: int`, `message: str`
  - `class TlGitNotConfigured(Exception)`
  - `def get_git_client() -> TlGitClient`: token `settings.tl_github_token or settings.tas_github_token`; se entrambi sono vuoti solleva `TlGitNotConfigured`
  - `class TlGitClient(token, repo)` con metodi async:
    - `head_sha(branch) -> str`
    - `branch_exists(branch) -> bool`
    - `create_branch(branch, from_sha) -> None` (422 "Reference already exists" = successo idempotente)
    - `get_file(path, ref) -> GitFile | None` (`GitFile(path, text, blob_sha)`, 404 → None)
    - `put_file(path, text, branch, message, blob_sha=None) -> str` (restituisce il commit sha)
    - `find_open_pull(head_branch) -> PullInfo | None`
    - `create_pull(head, base, title, body) -> PullInfo`
    - `get_pull(number) -> PullInfo` (`PullInfo(number, url, state: "open"|"merged"|"closed", merge_commit_sha, head_branch)`)
    - `compare(base_sha, head_sha) -> list[ChangedFile]` (`ChangedFile(path, status: "added"|"modified"|"removed"|"renamed", previous_path)`)
    - `list_tree(ref) -> list[str]` (percorsi dei blob, ricorsivo)
- Il client normalizza i testi letti in UTF-8 rimuovendo il BOM. Gli errori GitHub diventano `TlGitError` con il messaggio della risposta.

- [ ] **Step 1: Test** con `httpx.MockTransport` (il client accetta un `transport` opzionale per i test), uno per metodo:
  - `test_put_file_sends_base64_and_sha`;
  - `test_create_branch_already_exists_is_ok`;
  - `test_get_file_404_returns_none`;
  - `test_get_pull_maps_merged_state` (`merged: true` → `"merged"`; `closed` e non mergiata → `"closed"`);
  - `test_compare_maps_renamed_previous_path`;
  - `test_get_file_strips_bom`;
  - `test_error_carries_github_message`;
  - `test_get_git_client_without_token_raises`;
  - `test_get_git_client_falls_back_to_tas_token`.
- [ ] **Step 2: Eseguire** `uv run --extra dev pytest tests/test_tl_git_client.py -q`. Atteso: FAIL.
- [ ] **Step 3: Implementare** (stile di `app/services/github.py`, header `X-GitHub-Api-Version: 2022-11-28`, timeout 30 s).
- [ ] **Step 4: Eseguire.** Atteso: PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): github write client and settings`.

### Task 2: Modelli e migrazione 0037

**Files:**
- Modify: `app/models/test_lifecycle.py`
- Create: `alembic/versions/0037_test_lifecycle_git.py`
- Test: `tests/test_tl_models_git.py`

**Interfaces:**
- Produces:
  - `TlFeature` + `git_path: str|None` (String(500)), `git_blob_sha: str|None`.
  - `TlFeatureRevision` +:
    - `origin: str` (`"hub"`|`"git"`, default `"hub"`);
    - `pr_number: int|None`, `pr_url: str|None`, `pr_branch: str|None`, `pr_state: str|None`;
    - `pr_error: str|None` (Text);
    - `base_commit_sha: str|None`, `merged_commit_sha: str|None`;
    - `base_stale: bool = False`.
  - Enum `TlIssueKind`, `TlIssueStatus` (valori dei Global Constraints).
  - `TlIssue`:
    - `id`, `feature_id` (FK nullable, cascade), `revision_id` (nullable, SET NULL);
    - `kind`, `status` (default `open`);
    - `path: str|None`, `commit_sha: str|None`, `detail: dict` (JSONB);
    - `created_at`, `updated_by: str|None`, `updated_at`;
    - indice `(status, created_at)`.
  - `TlGitSyncState` (riga singola `id=1`): `last_main_sha`, `last_run_at`, `last_error`, `last_summary` (JSONB).
  - `TlStepPhrase`: `id`, `keyword` (`given`|`when`|`then`), `text` (Text), `occurrences: int`, `implemented: bool`, `updated_at`; unico `(keyword, text)`.

- [ ] **Step 1: Test** (DB reale come in fase 1):
  - `test_issue_defaults_open`;
  - `test_step_phrase_unique_keyword_text`;
  - `test_revision_git_defaults` (`origin == "hub"`, `base_stale` False).
- [ ] **Step 2–4:** FAIL → implementare modelli e migrazione (`downgrade` elimina colonne, tabelle e tipi) → `alembic upgrade head && downgrade -1 && upgrade head` → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): git models and migration`.

### Task 3: Parser dei feature file e classificazione delle differenze

**Files:**
- Create: `app/services/test_lifecycle/feature_parser.py`
- Test: `tests/test_tl_feature_parser.py`

**Interfaces:**
- Consumes: `RenderFeature`, `RenderScenario`, `render_feature_file`, `scenario_tag`, `ALLOWED_EXTRA_TAGS` (fase 1, `gherkin.py`).
- Produces:
  - `normalize_text(text: str) -> str`: rimuove il BOM, converte CRLF in LF, toglie gli spazi a fine riga, una sola newline finale.
  - `@dataclass ParsedFeature(code: str|None, feature_tag: str|None, title, description, background_steps, scenarios: list[ParsedScenario], errors: list[str])`
  - `@dataclass ParsedScenario(identity_tag: str|None, all_tags: list[str], title, kind, polarity, extra_tags, steps, examples, detail_link, line: int)`
  - `parse_feature_file(text: str) -> ParsedFeature`: usa `gherkin.parser.Parser`. In caso di errore di parsing restituisce `errors` non vuoto e `scenarios=[]`.
    - Il codice viene letto da `# qa-hub: <code>`.
    - Tag identità = il tag che corrisponde a `^@[A-Z0-9]+_\d{3}_\d{2}$`.
    - Polarità da `@positivo` / `@negativo`; extra tag = gli altri tag che stanno in `ALLOWED_EXTRA_TAGS`. I tag sconosciuti finiscono in `all_tags`.
  - `@dataclass DiffResult(textual: list[str], structural: list[StructuralChange], new_scenarios: list[ParsedScenario])`, dove `StructuralChange(kind: str, scenario_tag: str|None, message: str)`.
  - `classify(expected: RenderFeature, parsed: ParsedFeature, *, scope_prefix: str, feature_number: int) -> DiffResult`, con le regole dei Global Constraints. I messaggi sono in italiano.

- [ ] **Step 1: Test**:
  - `test_roundtrip_render_parse` (per il golden del Task 2 della fase 1, `parse(render(x))` restituisce gli stessi campi);
  - `test_parse_normalizes_crlf_bom_trailing_spaces` (Review Focus 3);
  - `test_parse_error_returns_errors`;
  - `test_classify_textual_only` (step cambiato → `textual` non vuoto, `structural` vuoto);
  - `test_classify_missing_identity_tag_is_structural`;
  - `test_classify_duplicate_tag_is_structural`;
  - `test_classify_wrong_prefix_is_structural`;
  - `test_classify_removed_scenario_is_structural`;
  - `test_classify_new_valid_tag_is_new_scenario`;
  - `test_classify_missing_header_is_structural`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): feature file parser and diff classification`.

### Task 4: Generazione della PR

**Files:**
- Create: `app/services/test_lifecycle/git_publish.py`
- Modify: `app/services/test_lifecycle/workflow.py` (motivo di submit nuovo), `app/api/v1/test_lifecycle.py`, `app/schemas/test_lifecycle.py`
- Test: `tests/test_tl_git_publish.py`

**Interfaces:**
- Consumes: `TlGitClient` (Task 1), `history.build_render` e `render_feature_file` (fase 1), `history.record_event`.
- Produces:
  - `slugify(title: str) -> str`
  - `feature_path(scope, feature, title) -> str`
  - `async publish_revision(db, feature_id, revision_id, actor: User, client: TlGitClient) -> TlFeatureDetail`
    - richiede revisione `approved` e `pr_state` in `{None, "closed"}`, oppure `base_stale` appena riallineata;
    - flusso idempotente:
      1. `head_sha(base)`;
      2. `create_branch` (ok se esiste già);
      3. `get_file(path, branch)` per il `blob_sha`;
      4. `put_file`;
      5. `find_open_pull(branch)` oppure `create_pull`.
    - Salva `pr_*`, `base_commit_sha`, `git_path` (solo se nullo).
    - Evento `tl.pr.created`. Gli errori vanno in `pr_error` e nell'anomalia `pr_creation_failed`, poi il servizio rilancia `TlGitError`.
  - Endpoint `POST /features/{id}/revisions/{rev}/pr` (`{actor_email}`; permesso UI `manage:tl_git` o `edit:tl_feature`) → `TlFeatureDetail`.
  - `POST …/transitions` con `approve`: dopo il commit dell'approvazione chiama `publish_revision` best effort. Se fallisce, l'approvazione resta valida e la risposta contiene `pr_error`.
- Nuovo motivo di blocco per `submit`: "L'ambito non ha una cartella del repository configurata" quando `scope.repo_folder` è vuoto.
- `TlRevisionOut` include `origin`, `pr_number`, `pr_url`, `pr_state`, `pr_error`, `base_stale`, `merged_commit_sha`. `TlFeatureDetail` include `git_path` e `open_issues: int`.

- [ ] **Step 1: Test** (client finto in memoria che registra le chiamate; `FakeGitClient` in `tests/tl_fakes.py`, riusato dai Task 5–9):
  - `test_slugify` (`"Creazione di un flusso è OK!"` → `"Creazione_di_un_flusso_e_OK"`);
  - `test_approve_creates_branch_file_and_pr` (percorso, branch, titolo, body con link e tag, messaggio di commit esatti);
  - `test_git_path_stable_after_title_change`;
  - `test_retry_reuses_existing_branch_and_pr` (Review Focus 1);
  - `test_pr_failure_keeps_approval_and_opens_issue`;
  - `test_submit_blocked_without_repo_folder`;
  - `test_publish_without_token_returns_503`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): generate feature file and open pull request`.

### Task 5: API delle anomalie

**Files:**
- Create: `app/services/test_lifecycle/issues.py`
- Modify: `app/api/v1/test_lifecycle.py`, `app/schemas/test_lifecycle.py`
- Test: `tests/test_tl_issues.py`

**Interfaces:**
- Produces:
  - `open_issue(db, *, kind, feature_id=None, revision_id=None, path=None, commit_sha=None, detail: dict) -> TlIssue`: deduplica le anomalie aperte con lo stesso `(kind, feature_id, path, detail.key)` se presente.
  - `GET /issues?status=&feature_id=&kind=` → `[TlIssueOut]` (con `feature_code` e `feature_title`), ordinate per `created_at` decrescente.
  - `PATCH /issues/{id}` con `{status: "acknowledged"|"resolved", actor_email}`:
    - evento `tl.issue.updated` sulla feature, se c'è;
    - quando si risolve l'ultima anomalia `structural_change` aperta di una feature `designed` che ha una revisione pubblicata, la feature passa a `implemented`.

- [ ] **Step 1: Test**:
  - `test_open_issue_dedup`;
  - `test_list_filters`;
  - `test_resolve_last_structural_sets_implemented`;
  - `test_unknown_actor_403`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): git issues api`.

### Task 6: Import, pubblicazione e anomalie da Git

**Files:**
- Create: `app/services/test_lifecycle/git_import.py`
- Test: `tests/test_tl_git_import.py`

**Interfaces:**
- Consumes: Task 3 (`parse_feature_file`, `classify`, `normalize_text`), Task 5 (`open_issue`), `history.snapshot` e `record_event`.
- Produces:
  - `async mark_revision_merged(db, rev, merge_commit_sha, client) -> None`
    - legge il file a `merge_commit_sha` e chiama `import_changed_file` con `merged_revision=rev`;
    - imposta `pr_state="merged"`, `merged_commit_sha`, `status=merged`;
    - `published_revision_id = rev.id`; la pubblicata precedente diventa `superseded`; `working_revision_id = None`;
    - evento `tl.pr.merged`.
  - `async import_changed_file(db, path, previous_path, commit_sha, client, merged_revision=None) -> None`:
    1. `normalize_text`;
    2. `parse`; un errore di parsing apre l'anomalia `parse_error`;
    3. trova la Feature dal codice dell'header; se il codice è sconosciuto apre `unknown_feature_code`;
    4. se `path != feature.git_path` aggiorna `git_path` (spostamento, Review Focus 4);
    5. confronta con la pubblicata, o con `merged_revision`;
    6. se il contenuto normalizzato è uguale al render atteso non fa nulla (Review Focus 3);
    7. altrimenti, se c'è `merged_revision`, ne aggiorna il contenuto con quello di Git (Review Focus 5); in assenza crea una nuova revisione `origin="git"`, `status=merged`, che diventa la pubblicata;
    8. crea le identità dei nuovi scenari validi (`next_scenario_number = max(…)+1`) e apre l'anomalia `new_scenario_from_git`;
    9. le differenze strutturali aprono `structural_change` con `detail.changes`;
    10. snapshot con `origin="git"` e `commit_sha`; evento `tl.git.imported`;
    11. alla prima pubblicazione la Feature passa a `implemented` solo se non ci sono anomalie strutturali aperte (altrimenti resta `designed`);
    12. se esiste una revisione di lavoro aperta con base diversa dalla pubblicata, imposta `base_stale=True` (se era `approved` torna `draft` con evento; la PR aperta resta e viene aggiornata dopo il riallineamento).
  - `async handle_removed_file(db, path, commit_sha) -> None`: se il file è gestito apre `file_deleted`; se nello stesso compare c'è un file con lo stesso header si tratta come spostamento e non apre nulla.

- [ ] **Step 1: Test**:
  - `test_merge_publishes_and_sets_implemented`;
  - `test_merged_content_differs_from_approved_render` (Review Focus 5);
  - `test_git_edit_creates_git_version_textual_no_issue`;
  - `test_structural_change_opens_issue_and_keeps_designed`;
  - `test_new_valid_scenario_creates_identity_and_issue`;
  - `test_reimport_identical_content_creates_no_version` (Review Focus 3);
  - `test_moved_file_updates_git_path_without_issue` (Review Focus 4);
  - `test_removed_managed_file_opens_issue`;
  - `test_unknown_code_opens_issue`;
  - `test_open_working_revision_becomes_stale`;
  - `test_approved_revision_returns_to_draft_when_stale`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): import feature files from git`.

### Task 7: Indice degli step e API di autocompletamento

**Files:**
- Create: `app/services/test_lifecycle/step_index.py`
- Modify: `app/api/v1/test_lifecycle.py`, `app/schemas/test_lifecycle.py`
- Test: `tests/test_tl_step_index.py`

**Interfaces:**
- Produces:
  - `extract_step_phrases(feature_text: str) -> list[tuple[str, str]]`: coppie `(keyword, text)`. Usa il parser Gherkin. `E` e `Ma` ereditano la keyword dello step precedente; `Dato`/`Data`/`Dati`/`Date` → `given`, `Quando` → `when`, `Allora` → `then`.
  - `extract_step_patterns(py_text: str) -> list[tuple[str, str]]`: decoratori `@given/@when/@then/@step(...)` con stringa letterale (anche `u"..."` e `r'...'`). `@step` vale per tutte e tre le keyword.
  - `pattern_matches(pattern: str, phrase: str) -> bool`: formato `parse` di behave (`{name}` e `{name:d}` → `.+?`), match completo.
  - `async rebuild_step_index(db, client) -> int`:
    - legge con `list_tree` i file `src/**/*.feature` e `src/**/steps/**/*.py` e scarica il contenuto;
    - aggrega le occorrenze per `(keyword, text)`;
    - `implemented` = almeno un pattern della stessa keyword corrisponde;
    - sostituisce la tabella in una transazione.
  - `GET /steps?q=&keyword=&limit=10` → `[{keyword, text, occurrences, implemented}]`, con ordine:
    1. prefisso case-insensitive;
    2. poi sottostringa;
    3. a parità, `implemented` prima e poi `occurrences` decrescente.

- [ ] **Step 1: Test**:
  - `test_extract_phrases_inherits_keyword_for_e_ma`;
  - `test_extract_patterns_variants`;
  - `test_pattern_matches_parse_placeholders`;
  - `test_rebuild_marks_implemented` (con `FakeGitClient`);
  - `test_steps_endpoint_ranking`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): step index and autocomplete api`.

### Task 8: Job di sync e stato delle PR

**Files:**
- Create: `app/services/test_lifecycle/git_sync.py`, `app/tasks/sync_test_lifecycle.py`
- Modify: `app/tasks/schedule.py` (voce beat `sync-test-lifecycle-git`, `schedule: settings.tl_sync_interval_s`, `options.expires` uguale), `app/api/v1/test_lifecycle.py`
- Modify: `helm/values-worker-dev.yaml`, `helm/values-worker-uat.yaml`, `helm/values-worker-prod.yaml` (in `envSecret` aggiungere `TAS_GITHUB_TOKEN: "pagopa-platform-tas-github-bot-pat"`, come già fatto nei values del backend; in prod il backend non lo monta ancora e non serve per questo task)
- Test: `tests/test_tl_git_sync.py`

**Interfaces:**
- Consumes: Task 1 (client), Task 5 (`open_issue`), Task 6 (`import_changed_file`, `handle_removed_file`, `mark_revision_merged`), Task 7 (`rebuild_step_index`).
- Produces:
  - `@dataclass SyncSummary(prs_merged: int, prs_closed: int, files_imported: int, issues_opened: int, steps_indexed: bool, head_sha: str)`
  - `async run_sync(db, client) -> SyncSummary`:
    1. ottiene l'advisory lock, altrimenti `SyncInProgress`;
    2. per ogni revisione con `pr_state == "open"` chiama `get_pull`:
       - `merged` → `mark_revision_merged(db, rev, merge_commit_sha, client)`;
       - `closed` → `pr_state="closed"`, evento `tl.pr.closed`, anomalia `pr_closed_unmerged`;
    3. `compare(last_main_sha, head)` (al primo giro: nessun import, salva solo `head`); per ogni `.feature`:
       - `removed` → `handle_removed_file`;
       - altrimenti → `import_changed_file(db, path, previous_path, head, client)`;
    4. se è cambiato un `.feature` o un file `steps/*.py`, ricostruisce l'indice degli step (Task 7, `rebuild_step_index`);
    5. salva `TlGitSyncState`.
  - Task Celery `app.tasks.sync_test_lifecycle.sync_test_lifecycle_git`: senza token esce in silenzio e lo registra nel log.
  - `POST /git/sync` (`{actor_email}`) → `SyncSummary`; `GET /git/status` → `{configured: bool, last_main_sha, last_run_at, last_error, last_summary}`.

- [ ] **Step 1: Test** (con `FakeGitClient`):
  - `test_first_run_records_head_without_import`;
  - `test_open_pr_merged_marks_revision`;
  - `test_open_pr_closed_opens_issue`;
  - `test_changed_feature_file_is_imported`;
  - `test_unmanaged_file_is_ignored`;
  - `test_concurrent_sync_second_gets_409` (Review Focus 2: due sessioni, la prima tiene il lock);
  - `test_sync_without_token_returns_503`;
  - `test_status_endpoint`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): git sync job and pull request tracking`.

### Task 9: Riapertura e riallineamento a tre vie

**Files:**
- Create: `app/services/test_lifecycle/rebase.py`
- Modify: `app/services/test_lifecycle/workflow.py`, `app/api/v1/test_lifecycle.py`, `app/schemas/test_lifecycle.py`
- Test: `tests/test_tl_rebase.py`

**Interfaces:**
- Produces:
  - **`reopen`** diventa consentito se la Feature ha una revisione pubblicata e nessuna revisione di lavoro (altrimenti motivo "Esiste già una revisione in corso"). Crea una revisione `draft` copiando contenuto e scenari della pubblicata, con `base_revision_id = published`, e la imposta come revisione di lavoro. Evento `tl.revision.reopened`. Il testo della fase 1 "Disponibile dopo il merge del feature file (fase Git)" resta solo quando non c'è ancora una pubblicata.
  - **Blocco di `submit` e `approve`** quando `base_stale`, con motivo "La bozza è basata su una versione superata: riallineala".
  - **Confronto:** `GET /features/{id}/revisions/{rev}/rebase` → `TlRebaseView` con:
    - `scenarios`: lista di `{scenario_id, tag, base: TlScenarioContentOut|None, git: …|None, draft: …|None, conflict: bool}`;
    - `metadata`: confronto a tre per titolo, descrizione e Contesto, nello stesso formato.
    - `conflict` è vero quando sia Git sia la bozza hanno cambiato quello scenario rispetto alla base.
  - **Applicazione:** `POST …/rebase` con `{lock_version, actor_email, choices: {<scenario_id|"title"|"description"|"background">: "git"|"draft"}}`:
    - ogni conflitto richiede una scelta, altrimenti 409 `rebase_required` con l'elenco;
    - i campi non in conflitto prendono automaticamente il lato che è cambiato;
    - poi `base_revision_id = published`, `base_stale=False`, `lock_version += 1`, evento `tl.revision.rebased`.
  - Dopo il riallineamento di una revisione che ha una PR aperta, la successiva approvazione chiama `publish_revision`, che aggiorna lo stesso branch e la stessa PR.

- [ ] **Step 1: Test**:
  - `test_reopen_copies_published_content`;
  - `test_reopen_blocked_with_open_revision`;
  - `test_submit_blocked_when_stale`;
  - `test_rebase_view_marks_conflicts`;
  - `test_rebase_requires_choice_for_conflicts`;
  - `test_rebase_applies_choices_and_clears_stale`;
  - `test_rebase_stale_lock_returns_409`;
  - `test_reapproval_updates_same_pr_branch`.
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): reopen and three-way rebase`.

### Task 10: Permessi, OpenAPI e verifica del backend

**Files:**
- Modify: `app/core/permissions.py` (`manage:tl_git`, categoria "Test Lifecycle"), `tests/test_permissions_catalog.py`, `openapi/qa-hub-be.json`

- [ ] **Step 1: Test** `test_tl_git_permission_defaults`. **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5:** `uv run --extra dev python -m scripts.generate_openapi --apim-compatible` (le modifiche devono essere solo aggiunte), poi `uv run --extra dev pytest -q` (sono attesi solo gli 11 fallimenti preesistenti: ea_sync_tracing, ea_service, gpd_position_service, status_page_service) e `uv run ruff check` sui file toccati.
- [ ] **Step 6: Commit** `feat(test-lifecycle): git permission and openapi`.

### Task 11: Controllo convenzioni in CI (repo dei test)

**Repo:** `/Users/cristiano.sticca/My Projects/pagopa-platform-integration-test`

**Files:**
- Create: `.github/scripts/check_feature_conventions.py`, `.github/scripts/test_check_feature_conventions.py`, `.github/workflows/check_feature_conventions.yml`

**Interfaces:**
- Produces: `check_file(path: str, text: str) -> list[str]`, solo stdlib (niente dipendenze). Lo script controlla solo i file la cui prima riga è `# qa-hub: FEAT_<NNN>_<Ambito>`. Regole:
  - seconda riga `# language: it`;
  - un tag di feature `@<AMBITO_MAIUSCOLO>_<NNN>` coerente con l'header;
  - ogni `Scenario:` / `Schema dello scenario:` preceduto da esattamente un tag identità `@<AMBITO_MAIUSCOLO>_<NNN>_<NN>`, con prefisso e numero di feature coerenti con l'header;
  - tag identità univoci nel file.

  `main(argv)` accetta un elenco di percorsi, stampa gli errori come `path:line: messaggio` e restituisce 1 se ne trova. Il workflow gira sulle PR verso `main` che toccano `**/*.feature` e passa i file `.feature` cambiati (con `git diff --name-only --diff-filter=d origin/main...HEAD`). Permessi `contents: read`. Le action vanno fissate per SHA come negli altri workflow del repo.

- [ ] **Step 0: Preparazione repo:** `gh repo clone pagopa/pagopa-platform-integration-test "/Users/cristiano.sticca/My Projects/pagopa-platform-integration-test"`, poi `git switch -c feature/test-lifecycle`. Nessun push.
- [ ] **Step 1: Test** (`python -m unittest .github/scripts/test_check_feature_conventions.py`):
  - `test_unmanaged_file_skipped`;
  - `test_valid_file_passes` (usare come fixture l'output golden della fase 1);
  - `test_missing_language`;
  - `test_scenario_without_identity_tag`;
  - `test_duplicate_identity_tag`;
  - `test_wrong_prefix`;
  - `test_main_exit_code`.
- [ ] **Step 2–4:** FAIL → implementare → PASS. Eseguire lo script anche su tutti i `.feature` esistenti del repo: atteso nessun errore, perché nessuno ha ancora l'header `qa-hub`.
- [ ] **Step 5: Commit** `ci: check qa-hub feature file conventions`.

### Task 12 (frontend): Tipi, hook e helper Git

**Files:**
- Modify: `src/types/test-lifecycle.ts`, `src/hooks/useTestLifecycle.ts`, `src/lib/test-lifecycle.ts` e i relativi test

**Interfaces:**
- Produces:
  - tipi che rispecchiano i nuovi schemi (campi Git di revisione e feature, `TlIssue`, `TlRebaseView`, `TlStepSuggestion`, `TlGitStatus`, `SyncSummary`);
  - hook:
    - `usePublishTlRevision` (POST pr);
    - `useTlGitStatus`, `useTlGitSync`;
    - `useTlIssues(filters)`, `useUpdateTlIssue`;
    - `useTlRebase(featureId, revId)`, `useApplyTlRebase`;
    - `useTlStepSuggestions(q, keyword)` (attivo con `q.length >= 2`, `staleTime` 60 s);
  - le mutation invalidano feature, history, issues e git status;
  - helper `ISSUE_KIND_LABEL`, `ISSUE_STATUS_LABEL`, `PR_STATE_LABEL` (etichette italiane) e l'evento `tlEventLabel` esteso con i nuovi eventi.
- [ ] **Step 1–4:** test degli hook (URL, `actor_email`, invalidazioni) e degli helper → FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): git types and hooks`.

### Task 13 (frontend): Tab Git, Riapri, anomalie e sincronizzazione

**Files:**
- Create: `src/components/test-lifecycle/GitPanel.tsx`, `src/components/test-lifecycle/IssueList.tsx`, `src/app/(portal)/test-lifecycle/anomalie/page.tsx`
- Modify: `src/app/(portal)/test-lifecycle/[id]/page.tsx` (tab "Git" dopo "Storico"), `src/components/test-lifecycle/FeatureHeader.tsx`, `src/app/(portal)/test-lifecycle/page.tsx` (pulsante "Anomalie (n)" e "Sincronizza ora" con stato dell'ultimo sync)

**Comportamento:**
- **GitPanel** mostra:
  - percorso del file;
  - PR con link (solo http/https, `safeHttpUrl`), stato tradotto e `pr_error`;
  - pulsante "Riprova PR", visibile quando la revisione è `approved` e la PR non è aperta (Gate `manage:tl_git` o `edit:tl_feature`);
  - SHA di base e di merge, abbreviati;
  - anomalie della feature (`IssueList` con le azioni "Presa visione" e "Risolta").
- **Header:**
  - badge "Base superata" quando `base_stale`, con pulsante "Riallinea" che apre il dialog del Task 14;
  - "Riapri" usa la transizione `reopen` già esistente: con il backend della fase 2 diventa abilitata.
- **Pagina `/test-lifecycle/anomalie`:** filtri per stato e tipo, link alla feature. È coperta dalla RouteGuard già presente nel layout.
- Se Git non è configurato (`configured=false`) compare un avviso informativo e "Sincronizza ora" è disabilitato.

- [ ] **Step 1: Test**:
  - GitPanel mostra il link della PR solo se è http(s);
  - "Riprova PR" è visibile solo nei casi corretti;
  - l'anomalia si risolve chiamando l'hook con lo stato giusto;
  - "Sincronizza ora" disabilitato quando non configurato;
  - badge "Base superata".
- [ ] **Step 2–4:** FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): git panel, reopen and issues UI`.

### Task 14 (frontend): Dialog di riallineamento a tre vie

**Files:**
- Create: `src/components/test-lifecycle/RebaseDialog.tsx`, `src/components/test-lifecycle/RebaseDialog.test.tsx`
- Modify: `src/app/(portal)/test-lifecycle/[id]/page.tsx`

**Comportamento:**
- Una sezione per ogni elemento: metadati e poi scenari in ordine di tag.
- Tre colonne Base / Git / Bozza, con le righe diverse evidenziate tramite `lineDiff`.
- Per i conflitti la scelta "Usa Git" / "Mantieni bozza" è obbligatoria; i non conflitti sono già risolti e mostrati come tali.
- "Applica" è disabilitato finché manca una scelta e invia `lock_version` tramite il writer della pagina (`afterPendingWrites`). Dopo il successo la pagina ricarica e azzera lo stato stale.
- Un 409 `stale_lock` attiva il banner della pagina.

- [ ] **Step 1–4:** test:
  - "Applica" disabilitato con un conflitto senza scelta;
  - le scelte inviate hanno la forma `{id: "git"|"draft"}`;
  - i non conflitti non chiedono scelta;
  - 409 `rebase_required` mostra l'elenco.

  FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): three-way rebase dialog`.

### Task 15 (frontend): Autocompletamento degli step

**Files:**
- Modify: `src/components/test-lifecycle/StepsEditor.tsx`
- Create: `src/components/test-lifecycle/StepSuggestions.tsx`, `src/lib/step-autocomplete.ts` (+ test)

**Interfaces:**
- `currentStepQuery(text: string, caret: number) -> {keyword: "given"|"when"|"then"|null, query: string, lineStart: number} | null`. Si basa sulla riga corrente: la keyword italiana viene mappata come nel backend, mentre `E`/`Ma` ereditano dalla riga di step precedente.
- `applySuggestion(text, caret, suggestion) -> {text, caret}`: sostituisce il testo dopo la keyword nella riga corrente.

**Comportamento:**
- Il menu compare sotto la riga corrente quando la query ha almeno 2 caratteri. Si naviga con ↑/↓, si conferma con Invio o Tab e si chiude con Esc.
- Le frasi implementate sono marcate "implementato".
- Il menu non intercetta Invio se è chiuso.
- È disattivato se l'editor è in sola lettura.

- [ ] **Step 1–4:** test:
  - `currentStepQuery` con E/Ma ereditati;
  - `applySuggestion` mantiene la keyword;
  - la tastiera sceglie un suggerimento;
  - con il menu chiuso Invio inserisce la nuova riga.

  FAIL → implementare → PASS.
- [ ] **Step 5: Commit** `feat(test-lifecycle): step autocomplete in editor`.

### Task 16: Verifica finale

- [ ] **Step 1:**
  - Backend: `uv run --extra dev pytest -q` (solo gli 11 fallimenti preesistenti) e `ruff` sui file nuovi.
  - Frontend: `pnpm test && pnpm lint && pnpm build`.
  - Repo test: `python -m unittest .github/scripts/test_check_feature_conventions.py`.
- [ ] **Step 2:** Prova manuale contro GitHub **solo se l'utente fornisce un token di test e un repo di prova** (mai il repo reale senza autorizzazione esplicita). Altrimenti la prova si fa con `FakeGitClient` attraverso l'API, e il fatto va riportato.
- [ ] **Step 3:** nessun push. Riportare per ogni repo i commit sul branch `feature/test-lifecycle`.
