# Test Lifecycle — Importa da Git — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, scelto dall'utente per risparmiare crediti). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dal tab Scenari di una bozza, importare un feature file da un link GitHub (qualsiasi branch) nella revisione di lavoro; all'approvazione la PR dell'hub scrive nel percorso importato e una PR di pulizia rimuove il file dal branch di partenza.

**Architecture:** la provenienza (`import_source`) vive sulla feature; un servizio di importazione legge il file con il client Git esistente, sostituisce gli scenari della bozza (tag dell'hub, alias dei vecchi tag, tag di categoria) e registra l'evento. La pubblicazione usa il percorso importato; dopo la PR dell'hub, un passo best effort apre la PR di pulizia verso il branch di partenza (nuova operazione `delete_file` nei client Git).

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic 0041; Next.js 16, TanStack Query, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-21-test-lifecycle-import-from-git-design.md`.

## Global Constraints

- Solo revisione di lavoro in bozza (`revision_locked` 409 altrimenti), `lock_version` obbligatorio (`stale_lock` 409).
- Link: `https://github.com/<settings.tl_repo>/blob/<branch>/<percorso>.feature`; branch con `/` risolto provando le divisioni con `branch_exists` (dalla più lunga alla più corta).
- Nessuna scrittura su branch esistenti: PR di pulizia da `qa-hub/cleanup-<codice minuscolo>` verso il branch di partenza; mai quando il branch di partenza è `tl_base_branch`.
- Il titolo della revisione non cambia; `git_path` resta vuoto fino alla PR.
- Test senza GitHub reale (`FakeGitClient`), modalità Git locale supportata.

## Review Focus

1. Bozza con scenari già presenti (anche di una feature riaperta, con identità usate dalla pubblicata) → le identità della pubblicata non si cancellano; quelle solo della bozza sì; numeri riassegnati senza collisioni.
2. File con errori di parsing → 422 con i messaggi, bozza invariata.
3. Riesecuzione dell'importazione con lo stesso link → stesso risultato, nessun alias duplicato.
4. Approvazione con branch di partenza già cancellato → PR dell'hub ok, pulizia "non necessaria", nessun errore.
5. PR di pulizia fallita → PR dell'hub comunque aperta, errore salvato, "Riprova" funziona.

---

### Task 1: Modello, client Git, lettura del link

**Files:** `alembic/versions/0041_test_lifecycle_import_source.py`, `app/models/test_lifecycle.py`, `app/services/test_lifecycle/git_client.py`, `local_git.py`, `tests/tl_fakes.py`, `app/services/test_lifecycle/git_import_file.py`, `tests/test_tl_import_git_link.py`.
- `tl_features.import_source` JSONB null: `{url, branch, path, blob_sha, imported_at, cleanup: {state: "pending"|"open"|"not_needed"|"error", pr_number, pr_url, error, note}}`.
- `delete_file(path, branch, message, blob_sha) -> str` (sha del commit) su `TlGitClient` (`DELETE contents/<path>`), `LocalGitClient` (via `_commit_tree_change --force-remove`), `FakeGitClient`.
- `parse_blob_url(url) -> (repo, rest)` e `async resolve_blob(client, url) -> (branch, path)`; errori `ImportUrlInvalid(message)`.
- [ ] Test: link valido (branch semplice e con `/`), repo diverso, non `blob`, non `.feature`, branch inesistente; `delete_file` sul fake e sul clone locale. → **Commit** `feat(test-lifecycle): import source model and git file deletion`.

### Task 2: Importazione nella bozza

**Files:** `git_import_file.py`, `app/schemas/test_lifecycle.py`, `app/api/v1/test_lifecycle.py`, `tests/test_tl_import_git.py`.
- `async import_from_git(db, feature_id, revision_id, url, lock_version, actor_email, client) -> TlImportGitResult(feature: TlFeatureDetail, ignored_tags: list[str], replaced: int)`: risolve il link e legge il file (prima dei lock), parse con i tag di categoria; errori di parsing → `ImportParseError(messages)`; lock feature e revisione, bozza e `lock_version`; rimuove i contenuti della revisione e le identità usate solo da questa revisione; crea scenari con tag `scenario_tag(scope, number, i)` dai numeri liberi (`next_scenario_number` riallineato), polarità dal tag o positivo, extra tag = tag di categoria (anche dalla feature); alias dei vecchi tag identità di scenario (senza duplicati); `description`/`background_steps` dal file; `lock_version += 1`; `import_source` con `cleanup.state="pending"`; evento `tl.feature.imported`.
- `POST /features/{id}/revisions/{rid}/import-git` (`TlImportGitRequest{actor_email, lock_version, url}`) → `TlImportGitResult`; errori `import_url_invalid` 422, `import_file_not_found` 404, `import_parse_error` 422 (`reasons`), più i soliti 409. `TlFeatureDetail.import_source`.
- [ ] Test: importazione completa (tag, alias, Esempi, Contesto, titolo invariato, evento); sostituzione di scenari esistenti; feature riaperta non perde le identità della pubblicata; file illeggibile → 422 e nulla cambia; non bozza → 409; reimportazione idempotente. → **Commit** `feat(test-lifecycle): import feature file from git into the draft`.

### Task 3: Percorso adottato e PR di pulizia

**Files:** `git_publish.py`, `git_import_file.py`, `workflow.py`, `app/api/v1/test_lifecycle.py`, `tests/test_tl_import_git_cleanup.py`.
- Pubblicazione: `path = feature.git_path or import_source.path or feature_path(...)`; corpo PR con "Importata da `<branch>`/`<percorso>`" e la PR di pulizia.
- `async open_cleanup_pr(db, feature_id, client) -> None`: niente se non c'è provenienza, branch = base o stato `open`/`not_needed`; branch o file spariti → `not_needed` con nota; altrimenti branch `qa-hub/cleanup-<codice>` dal branch di partenza, `delete_file`, PR "Rimuovi <codice>: ora gestito da QA Hub" verso il branch di partenza; errori in `cleanup.error` (`state="error"`). Chiamato best effort dopo la pubblicazione all'approvazione.
- `POST /features/{id}/import-cleanup` (retry) → `TlFeatureDetail`.
- [ ] Test: approvazione → PR dell'hub nel percorso importato + PR di pulizia con file rimosso sul branch di pulizia; file da `main` → nessuna pulizia; branch cancellato → `not_needed`; errore → stato `error` e retry ok. → **Commit** `feat(test-lifecycle): adopt imported path and open cleanup pr`.

### Task 4: API e OpenAPI

- [ ] Suite completa (11 preesistenti), ruff, OpenAPI solo aggiunte (verifica semantica). → **Commit** `feat(test-lifecycle): import from git openapi`.

### Task 5 (frontend): dialog, tab Git, guida

**Files:** tipi e hook (`useImportTlGit`, `useRetryTlImportCleanup`), `ImportFromGitDialog.tsx` (+ test), pagina feature (pulsante "Importa da Git" nel tab Scenari, visibile se la bozza è modificabile), `GitPanel.tsx` (provenienza, PR di pulizia con link http(s), "Riprova PR di pulizia"), guida (sezione "Importa da Git"), etichetta evento `tl.feature.imported`.
- Dialog: campo link, conferma esplicita se la bozza ha scenari ("verranno sostituiti N scenari"), errori dal backend (anche `reasons`), esito con i tag non importati e il promemoria sulla PR di pulizia.
- [ ] TDD: invio con `lock_version`; conferma richiesta con scenari presenti; errori mostrati; tab Git con stati della pulizia e retry. → **Commit** `feat(test-lifecycle): import from git ui`.

### Task 6: Verifica finale

- [ ] Backend `pytest -q`, frontend test + lint + build; prova locale col clone (branch di prova creato con `tl_local_git write`). Nessun push.
