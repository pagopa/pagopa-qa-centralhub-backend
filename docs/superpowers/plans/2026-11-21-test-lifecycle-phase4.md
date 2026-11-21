# Test Lifecycle — Fase 4 (NRT) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (inline, scelto dall'utente per risparmiare crediti). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** collegare ogni scenario dell'hub alle sue esecuzioni NRT tramite il tag identità, mostrare l'esito per scenario nella feature e portare la feature in RUNNING alla prima esecuzione.

**Architecture:** i tag behave sono già in `test_executions.allure_report.labels` (name `tag`, valore senza `@`), salvati via `POST /test-executions` dal repo dei test. Una colonna generata `tags` (JSONB, indice GIN) li estrae senza toccare il repo né l'ingest. Un servizio `nrt.py` fa il match tag ↔ scenario (più alias), aggrega per scenario/ambiente e promuove IMPLEMENTED → RUNNING (subito dopo l'ingest, best effort, e con un giro periodico di recupero).

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Alembic 0039, Celery beat; Next.js 16, TanStack Query, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-20-test-lifecycle-design.md` §6 (RUNNING), §9 (NRT), §11 (tab NRT, panoramica).

## Global Constraints

- Tabelle nello schema `qachub`, prefisso `tl_`; nessuna scrittura nel repo dei test.
- Match per tag identità: tag scenario dell'hub senza `@` = valore della label `tag`; confronto esatto. Gli alias (`tl_scenario_aliases`) coprono tag storici (riempiti in fase 5).
- RUNNING: solo da IMPLEMENTED, mai all'indietro; evento `tl.feature.running`.
- Esito per run di uno scenario (outline = più esecuzioni): FAILED se almeno una FAILED, poi BROKEN, PASSED se tutte passate o saltate con almeno una passata, SKIPPED se tutte saltate.
- Pass rate scenario: esecuzioni-run PASSED / (PASSED+FAILED+BROKEN) sulle ultime `tl_nrt_window = 10` run in cui compare lo scenario (tutti gli ambienti); `None` senza dati.
- Flaky: `flaky_registry.is_flaky` vero per lo `scenario_name` di un'esecuzione collegata.
- Nessuna chiamata di rete; i test non toccano GitHub/Jira reali.

## Review Focus

1. Executions senza `allure_report` o senza label `tag` → ignorate, nessun errore.
2. Outline: più esecuzioni dello stesso scenario nella stessa run → un solo esito aggregato per run.
3. Feature DESIGNED già eseguita (branch PR) → resta DESIGNED; diventa RUNNING al giro dopo la promozione a IMPLEMENTED.
4. Promozione concorrente (ingest + giro periodico) → un solo evento `tl.feature.running` (lock della feature e ricontrollo dello stato).
5. Ingest NRT: se la promozione fallisce, `POST /test-executions` risponde comunque 200.

---

### Task 1: Tag sulle esecuzioni e alias

**Files:** `alembic/versions/0039_test_lifecycle_nrt.py`, `app/models/test_metrics.py`, `app/models/test_lifecycle.py`, `tests/test_tl_nrt_tags.py`.
- `test_executions.tags` JSONB `GENERATED ALWAYS AS (coalesce(jsonb_path_query_array(allure_report, '$.labels[*] ? (@.name == "tag").value'), '[]'::jsonb)) STORED`, indice GIN `ix_qachub_test_executions_tags`. Modello: `Column(JSONB, Computed(...))`.
- `tl_scenario_aliases(id, scenario_id FK cascade, alias String(128) unique, created_at)`; modello `TlScenarioAlias`.
- `tl_features.running_at` TIMESTAMPTZ nullable.
- [ ] Test: esecuzione con labels `[{"name":"tag","value":"FDR3_001_01"},{"name":"severity","value":"x"}]` → `tags == ["FDR3_001_01"]`; senza report → `[]`. → **Commit** `feat(test-lifecycle): execution tags and scenario aliases`.

### Task 2: Servizio di match e aggregazione

**Files:** `app/services/test_lifecycle/nrt.py`, `app/schemas/test_lifecycle.py`, `app/config.py` (`tl_nrt_window: int = 10`, `tl_nrt_running_interval_s: float = 900.0`), `tests/test_tl_nrt.py`.
- `match_tags(feature) -> dict[str, uuid]` (tag senza `@` e alias → scenario_id).
- `async feature_nrt(db, feature_id) -> TlFeatureNrt`: per scenario (della revisione pubblicata, altrimenti di lavoro) `envs: list[TlNrtEnvResult(env, status, run_id, run_at, allure_url, origin)]` (ultima run per ambiente), `pass_rate`, `runs` (contate nella finestra), `flaky`, `last_run_at`; a livello feature `last_run_at`, `pass_rate`.
- `async run_feature_links(db, run_id) -> list[TlNrtRunLink(execution_id, feature_id, feature_code, scenario_tag)]`.
- `async scope_pass_rates(db) -> dict[scope_id, float|None]`.
- [ ] Test: due run (UAT FAILED poi PASSED, DEV PASSED) → ultimo per ambiente e pass rate 2/3; outline con un esempio fallito → run FAILED; alias conta; esecuzioni senza tag ignorate; flaky dal registro; link run → feature. → **Commit** `feat(test-lifecycle): nrt results per scenario`.

### Task 3: RUNNING automatico

**Files:** `nrt.py`, `app/api/v1/test_metrics.py`, `app/tasks/sync_test_lifecycle_nrt.py`, `app/tasks/__init__.py`, `app/tasks/schedule.py`, `tests/test_tl_nrt_running.py`.
- `async promote_running(db, *, run_ids=None, limit=500) -> int`: feature IMPLEMENTED con almeno un tag presente in esecuzioni (delle run indicate, o tutte) → lock feature, ricontrollo stato, `status=RUNNING`, `running_at`, evento `tl.feature.running` (`{"run_id", "tag"}`, attore None), commit per feature.
- Dopo `create_test_execution`: `promote_running(db, run_ids={...})` in try/except con log (mai errore all'ingest).
- Celery `app.tasks.sync_test_lifecycle_nrt.promote_running`, beat `promote-test-lifecycle-running` ogni `tl_nrt_running_interval_s`, `expires` uguale, lock advisory `tl_nrt_running`.
- [ ] Test: IMPLEMENTED + esecuzione → RUNNING con evento; DESIGNED resta; seconda chiamata nessun nuovo evento; ingest con promozione che solleva → 200; beat entry. → **Commit** `feat(test-lifecycle): automatic running status`.

### Task 4: API e OpenAPI

**Files:** `app/api/v1/test_lifecycle.py`, `app/services/test_lifecycle/scopes.py`, `tests/test_tl_nrt_api.py`, `openapi/qa-hub-be.json`.
- `GET /test-lifecycle/features/{id}/nrt` → `TlFeatureNrt` (404 feature inesistente).
- `GET /test-lifecycle/nrt/runs/{run_id}/links` → `list[TlNrtRunLink]`.
- `TlScopeOut.nrt_pass_rate: float | None`.
- [ ] Test endpoint + suite completa (11 preesistenti) + ruff + OpenAPI solo aggiunte → **Commit** `feat(test-lifecycle): nrt api`.

### Task 5 (frontend): tipi, hook, tab NRT, link dalla run, panoramica, guida

**Files:** `src/types/test-lifecycle.ts`, `src/hooks/useTestLifecycle.ts`, `src/components/test-lifecycle/NrtPanel.tsx` (+ test), pagina feature, `src/app/(portal)/nrts/report/[runId]/page.tsx` e `tas-nrts/...`, `ScopeOverview.tsx`, guida, `LifecycleDiagram.tsx`, `src/lib/test-lifecycle.ts`.
- Hook `useTlFeatureNrt(id)`, `useTlRunLinks(runId)`.
- Tab **NRT** (label con l'ultimo esito): tabella per scenario — tag, titolo, badge per ambiente (link Allure solo http(s)), pass rate, badge flaky, "Mai eseguito".
- Report run NRT/TAS: colonna "Feature" con link a `/test-lifecycle/<id>` per gli scenari collegati.
- Panoramica ambiti: pass rate NRT. Guida: sezione "NRT" (match per tag, RUNNING automatico, flaky). Diagramma: sottotitolo Implementata "→ In esecuzione con NRT". Etichette eventi `tl.feature.running`.
- [ ] TDD: badge e link http(s), "Mai eseguito", flaky, colonna Feature nel report → **Commit** `feat(test-lifecycle): nrt tab and run links`.

### Task 6: Verifica finale

- [ ] Backend `pytest -q` (11 preesistenti), frontend test + lint + build. Nessun push.
