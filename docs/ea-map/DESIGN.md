# EA Map nel QA Hub – Design tecnico

> Branch: `feature/ea-map` (backend e frontend) · Stato: **approvato il 15/10/2026 con le decisioni del §11** · Autore: Claude con Cristiano Sticca
>
> Obiettivo: portare nel QA Hub la Mappa Piattaforma pagoPA (Blueprint, Architettura, Matrice QA, Guida) con dati persistiti su PostgreSQL, editing dei legami e sincronizzazione periodica con revisione umana.

---

## 1. Perimetro

| # | Capacità | Fase |
|---|---|---|
| 1 | Visualizzazione nel Hub (le 4 viste attuali) alimentata da API | PR1 |
| 2 | Persistenza su PostgreSQL con import iniziale della KB attuale | PR1 |
| 3 | Editing: correggere, aggiungere, rimuovere e verificare legami, nodi e dati QA manuali | PR2 |
| 4 | Sync periodica da GitHub e Confluence con coda di revisione | PR3 |
| 5 | Notifiche, export, hardening | PR4 |

Evoluzione **Data Lake** (data contract, vista Data Lake, overlay DQ): vedi [DESIGN-data-lake.md](./DESIGN-data-lake.md).

Fuori perimetro per ora: analisi del repo privato `pagopa-nodo4-nodo-dei-pagamenti`, metriche di esecuzione dei test (esiti, durate), che restano negli altri moduli del Hub.

## 2. Cosa ho trovato nel codice del Hub

**Backend** (`qa-hub-backend`):
- FastAPI, SQLAlchemy async, Alembic (ultima migrazione `0018_remove_catalog_code`), Celery + Redis, PostgreSQL.
- Pattern di sync consolidato in `sanp_health`: servizio, task Celery, `beat_schedule`, endpoint `POST /sync` e `GET /sync-status`, advisory lock Postgres (`pg_try_advisory_xact_lock`) contro le esecuzioni concorrenti, tabella di stato della sync.
- `GitHubClient` in `app/services/github.py` e `github_token` già in configurazione.
- Credenziali Atlassian già presenti per Jira (`jira_base_url`, `jira_email`, `jira_api_token`). Sullo stesso sito `pagopa.atlassian.net` valgono anche per le API Confluence, se l'utenza ha accesso agli spazi.
- Ruoli con permessi in JSONB (`roles.permissions`), catalogo azioni in `app/core/permissions.py`, feature flag in `app/core/features.py`, tabella `audit_logs`.

**Frontend** (`qa-hub-frontend`):
- Next.js 16 App Router, React 19, TanStack Query, Tailwind 4, shadcn.
- Un modulo è una cartella `src/app/(portal)/<modulo>` più `src/components/<modulo>`, con voce in `Sidebar.tsx` legata a un'azione (`action`) e a una feature (`featureKey`), e gate `<Gate action=…>`.

**Punto di attenzione sulla sicurezza.** Negli endpoint di `app/api/v1` non c'è l'uso di `CurrentUser` né di `require_role`: oggi i permessi sono applicati solo dal frontend. **Decisione:** l'EA Map segue lo stesso modello del resto del Hub; l'enforcement lato server sarà affrontato per tutto il Hub in un'attività di debito tecnico separata.

## 3. Modello dei dati

Principio guida: **separare ciò che è osservato da ciò che è asserito.** Ogni nodo e ogni legame dichiara da dove viene e chi l'ha confermato. La sync aggiorna solo ciò che le appartiene e non sovrascrive mai una decisione umana.

Volumi attesi: ~300 nodi, qualche centinaio di legami, alcune migliaia di pagine Confluence indicizzate. Un database a grafo non serve: l'analisi d'impatto multilivello si fa con CTE ricorsive in Postgres.

### 3.1 Tabelle (migrazione `0019_ea_map_tables`)

**`ea_nodes`**: tutti gli elementi della mappa.

| Colonna | Tipo | Note |
|---|---|---|
| `id` | `varchar(200)` PK | Slug stabile: nome repo per i componenti, `prod:gpd`, `area:gps`, `ns-gps`, `pr-m3`… (gli stessi id della KB attuale, così i deep link restano validi) |
| `kind` | enum `ea_node_kind` | `area`, `product`, `component`, `external`, `datastore`, `segment`, `service`, `function`, `process`, `infra` |
| `parent_id` | FK → `ea_nodes.id`, null | Gerarchia area → prodotto → componente |
| `name`, `description` | text | |
| `attrs` | JSONB | Attributi per tipo: lang, fw, ns, ingress, owners, apis, workflows, readme, repo_url, private, risk, domain… |
| `source` | enum `ea_source` | `sync`, `curated`, `manual` |
| `status` | enum `ea_status` | `active`, `proposed`, `archived` |
| `verified_by`, `verified_at` | FK users, timestamptz | Conferma umana |
| `sort_order` | int | Ordinamento nelle viste |
| `created_at`, `updated_at` | timestamptz | |

**`ea_edges`**: i legami.

| Colonna | Tipo | Note |
|---|---|---|
| `id` | uuid PK | |
| `from_id`, `to_id` | FK → `ea_nodes.id` | |
| `type` | enum `ea_edge_type` | Tecnici: `api`, `soap`, `event`, `data`, `ui`. Blueprint: `serves` (segmento→servizio), `realizes` (servizio→processo), `groups` (funzione→processo), `uses` (processo→prodotto), `runs_on` (prodotto→infra) |
| `label` | text | |
| `origin` | enum `ea_edge_origin` | `helm`, `infra_rule`, `curated`, `manual` |
| `status` | enum `ea_edge_status` | `proposed`, `active`, `verified`, `rejected`, `removed` |
| `evidence` | JSONB | Prova del legame: file e valore Helm, URL Confluence, utente e nota |
| `confidence` | smallint | 0–100. Helm 90, regola infra 70, curato 60, manuale verificato 100 |
| `created_by`, `verified_by`, `verified_at`, `note` | | |
| | `UNIQUE (from_id, to_id, type)` | |

**`ea_qa_signals`**: un record per componente.

| Gruppo | Colonne | Chi le scrive |
|---|---|---|
| Osservate | `unit_files`, `it_features`, `it_workflow`, `perf`, `e2e`, `sonar`, `central_suites text[]`, `nrt_suites text[]`, `nodo_bdd`, `tas_ci_observed`, `observed_at` | Solo la sync |
| Manuali | `under_qa bool null`, `tas_ci_override bool null`, `qa_owner`, `qa_notes`, `manual_updated_by/at` | Solo gli utenti |

Il punteggio di maturità non si salva: si calcola nel servizio con la stessa formula della Guida, così una modifica alla formula vale subito per tutto.

**`ea_doc_links`**: `node_id`, `url`, `title`, `space`, `pages`, `source` (`rule` | `manual`), `status`.

**`ea_sync_runs`**: `id`, `trigger` (`beat` | `manual`), `triggered_by`, `started_at`, `finished_at`, `status` (`running` | `success` | `partial` | `failed`), `stats` JSONB (repo letti, saltati perché invariati, modifiche proposte e applicate), `source_versions` JSONB (SHA per repo, numero di pagine per spazio), `error`.

**`ea_repo_snapshots`**: `repo`, `default_branch`, `head_sha`, `extracted` JSONB, `fetched_at`. Serve a saltare i repo con lo stesso SHA dell'ultima sync e a mostrare da quale commit viene un dato.

**`ea_changes`**: la coda di revisione.

| Colonna | Note |
|---|---|
| `id`, `run_id` | |
| `entity` | `node`, `edge`, `qa`, `doc` |
| `op` | `add`, `update`, `remove` |
| `target_id` | id del nodo o legame |
| `before`, `after` | JSONB (diff leggibile) |
| `structural` | bool: se vero richiede revisione |
| `status` | `pending`, `approved`, `rejected`, `auto_applied`, `superseded`, `conflict` |
| `reviewed_by`, `reviewed_at`, `review_note` | |

**`ea_sync_status`**: riga singola con l'ultimo esito, sullo stesso schema di `SanpHealthSyncStatus`.

Le modifiche manuali vanno anche in `ea_edit_log` (`action` = `ea.edge.create`, `ea.edge.verify`, `ea.node.update`, `ea.qa.update`, `ea.change.approve`…, `entity` = `edge`/`node`, `actor`, `detail` JSONB con before/after). La tabella generica `audit_logs` del modello Hub non è mai stata creata da una migrazione, per questo l'EA usa una tabella propria.

### 3.2 Regole di precedenza

1. Un record con `source = manual` o `status = verified` **non viene mai modificato dalla sync** nei campi che appartengono alle persone: nome, descrizione, prodotto, tipo di componente, campi QA manuali, legami. Gli attributi osservati (linguaggi, framework, namespace, owner, API, pipeline, README, segnali QA osservati) appartengono invece alla sync e si aggiornano su tutti i componenti attivi.
2. Se la sync non trova più un legame verificato o manuale, crea un `ea_change` con stato `conflict`: decide una persona.
3. Un legame rifiutato (`rejected`) resta in tabella, così la sync non lo ripropone a ogni giro.
4. Rimuovere un legame è una cancellazione logica (`status = removed`), per conservare lo storico.

## 4. API – prefisso `/api/v1/ea`

| Metodo | Path | Permesso | Descrizione |
|---|---|---|---|
| GET | `/kb` | `view:ea_map` | Grafo completo nel formato atteso dal viewer. ETag e cache Redis, invalidata a ogni scrittura |
| GET | `/nodes/{id}` | `view:ea_map` | Dettaglio con legami, segnali QA, link e storico |
| GET | `/nodes/{id}/impact` | `view:ea_map` | Catena verticale (CTE ricorsiva), stessa logica del pannello Impatto |
| POST | `/nodes` | `edit:ea_map` | Nuovo nodo manuale (es. componente non su GitHub, sistema esterno) |
| PATCH | `/nodes/{id}` | `edit:ea_map` | Nome, descrizione, prodotto padre, attributi editabili |
| GET | `/edges` | `view:ea_map` | Filtri per nodo, tipo, origine, stato |
| POST | `/edges` | `edit:ea_map` | Nuovo legame (`origin = manual`, nota obbligatoria) |
| PATCH | `/edges/{id}` | `edit:ea_map` | Tipo, etichetta, nota |
| DELETE | `/edges/{id}` | `edit:ea_map` | Cancellazione logica |
| POST | `/edges/{id}/verify` | `edit:ea_map` | Conferma (`status = verified`) |
| PATCH | `/nodes/{id}/qa` | `edit:ea_qa` | `under_qa`, `tas_ci_override`, `qa_owner`, `qa_notes` |
| GET | `/changes` | `review:ea_map` | Coda di revisione, filtrabile per run, stato e tipo |
| POST | `/changes/{id}/approve` · `/reject` | `review:ea_map` | Singola decisione, con nota |
| POST | `/changes/bulk` | `review:ea_map` | Decisioni in blocco |
| POST | `/sync` | `sync:trigger` | Sync manuale; 409 se già in corso |
| GET | `/sync-status` · `/sync-runs` | `view:ea_map` | Stato ed elenco delle esecuzioni |
| GET | `/export` | `view:ea_map` | Export JSON (formato `kb.json`) e CSV della matrice QA |

L'import iniziale è un comando (`python -m scripts.ea_import`), non un endpoint.

## 5. Sync periodica

Task Celery `app.tasks.sync_ea.sync_ea`, registrato in `beat_schedule`: **una volta a settimana**, più il pulsante "Sincronizza ora" nel frontend per forzarla, come negli altri moduli. Si usa lo stesso advisory lock di `sanp_health` per evitare esecuzioni concorrenti.

| Passo | Cosa fa | Fonte |
|---|---|---|
| 1. Censimento | Elenca i repo dell'org e applica le regole di perimetro (attivi, non fork, nome che contiene `pagopa`). Repo nuovi → `ea_change add` strutturale; repo archiviati → `ea_change update` strutturale | GitHub API (`github_token`) |
| 2. Delta | Per ogni repo confronta `head_sha` con `ea_repo_snapshots`; se è uguale lo salta. In una notte normale si rileggono pochi repo | GitHub API |
| 3. Estrazione | Albero del repo (`git/trees?recursive=1`) e lettura dei soli file di metadati, con la logica di `extract.py` portata in `app/services/ea/extract.py` come funzioni pure testate | GitHub contents/raw |
| 4. Contesto | Domini e API da `pagopa-infra`; suite, `nrt-suites.json` e workflow TAS da `pagopa-platform-integration-test`; feature BDD del Nodo | GitHub |
| 5. Confluence | Titoli delle pagine dei 7 spazi (API v2) e regole di collegamento ai prodotti | Utenza Atlassian già configurata per Jira (`jira_email`, `jira_api_token`) |
| 6. Grafo candidato | Nodi, legami Helm e regole infra, segnali QA, link documentali | – |
| 7. Diff | Confronto con il DB, rispettando le precedenze del §3.2 | – |
| 8. Applicazione | Metadati, segnali QA osservati e link da regola si applicano subito (`auto_applied`); le modifiche strutturali vanno in coda (`pending`) | – |
| 9. Notifica | Se ci sono modifiche in coda, avviso tramite il sistema di notifiche esistente | `notification_rules` |

**Classificazione dei repo nuovi.** Un repo è candidato se il suo nome contiene `pagopa` (perimetro deciso il 22/10/2026: prima valevano anche i prefissi `arc-`, `emd-`, `rtp-`, `qe_`…; i componenti già in mappa fuori perimetro vengono proposti per l'archiviazione, insieme ai prodotti che restano vuoti), non è fork né archiviato, ha avuto push nell'ultimo anno e il suo CODEOWNERS cita almeno un team che possiede già un componente della mappa. La sync propone il prodotto in base al prefisso del nome condiviso con i componenti noti, verificato con il namespace Helm / dominio `.identity`. Chi revisiona conferma o sceglie un altro prodotto, nome e tipo. Le regole sono in `app/services/ea/sync/rules.py`, portate dagli script di discovery (renderle editabili da UI è un'evoluzione possibile).

**Costo.** Per repo: una chiamata REST per l'albero e una GraphQL per tutti i file di metadati. Primo giro ≈ 190 + 190 chiamate; a regime si rileggono solo i repo con push successivi all'ultimo snapshot, e se lo SHA dell'albero è invariato si riusa l'estrazione salvata. Margine ampio rispetto alle 5.000 richieste/ora.

### 5.1 Implementazione (PR3)

| Modulo | Ruolo |
|---|---|
| `app/services/ea/sync/rules.py` | Regole di URL→legame, datastore, suite centrali/NRT, link Confluence, perimetro dei candidati |
| `app/services/ea/sync/extract.py` | Fatti di un repo da albero + file (funzioni pure; parità verificata con l'estrazione originale su 185 repo) |
| `app/services/ea/sync/derive.py` | Fatti → attributi, segnali QA, legami Helm, link Confluence, proposta di prodotto/tipo |
| `app/services/ea/sync/sources.py` | Client GitHub (REST + GraphQL) e Confluence v2, dietro protocolli sostituibili nei test |
| `app/services/ea/sync/engine.py` | Esecuzione: lettura stato → raccolta (solo rete) → applicazione in un'unica transazione; `dry_run` |
| `app/services/ea/sync/review.py` | Approvazione/rifiuto (singolo e in blocco) e applicazione delle proposte |
| `app/tasks/sync_ea.py` | Task Celery; beat ogni lunedì alle 04:17 UTC |
| `scripts/ea_sync.py` | Esecuzione da riga di comando, anche `--dry-run` |

- **Concorrenza**: una sola run alla volta (`409` sull'endpoint). Una run ferma da più di due ore viene chiusa come fallita alla run successiva.
- **Avvio manuale**: `POST /sync` registra la run e la affida al worker Celery; se il broker non risponde (sviluppo locale) la esegue nel processo API in background.
- **Esiti**: `success`; `partial` se qualche repo non è leggibile, se Confluence non risponde o non è configurato (i dati di quella fonte restano invariati); `failed` se non si riesce a fare il censimento GitHub.
- **Deduplica**: una proposta già in coda o rifiutata non viene ricreata. Una proposta in coda che la run successiva non rileva più diventa `superseded`, ma solo se quella fonte è stata effettivamente riletta.
- **Rifiuti con effetto**: rifiutare un nuovo legame lo salva come `rejected`; rifiutare la rimozione di un legame lo rende `verified` (una persona ha confermato che esiste).
- **Verifica su dati reali**: eseguito il motore sui 185 repo clonati in discovery: prima sync 0 proposte e 0 modifiche dopo l'import, sync successive tutte "invariate".

## 6. Permessi e feature flag

Nuove voci in `ACTION_CATALOG`:

| Azione | qa_manager | qa_engineer | qa_analyst | guest |
|---|---|---|---|---|
| `view:ea_map` | ✓ | ✓ | ✓ | ✓ |
| `edit:ea_map` | ✓ | ✓ | – | – |
| `edit:ea_qa` | ✓ | ✓ | – | – |
| `review:ea_map` | ✓ | – | – | – |

`superadmin` è un ruolo di sistema e ha tutti i permessi: la revisione è quindi in carico a **qa_manager e superadmin**.

Nuova feature `ea_map` in `FEATURE_CATALOG`.

I permessi sono applicati dal frontend (`<Gate action=…>`), come nel resto del Hub. L'enforcement lato server è rimandato all'attività di debito tecnico (vedi §2).

## 7. Frontend

Struttura in `qa-hub-frontend`:

```
src/app/(portal)/ea-map/
  layout.tsx            gate view:ea_map + feature ea_map
  page.tsx              tab Blueprint | Architettura | Matrice QA | Guida | Revisione
src/components/ea-map/
  EaBlueprint.tsx       piani a livelli, tracciamento e pannello Impatto
  EaArchitecture.tsx    piattaforma → area → prodotto (diagramma a tre colonne)
  EaComponentDrawer.tsx scheda componente, con modifica di legami e dati QA
  EaQaMatrix.tsx        matrice con filtri
  EaGuide.tsx           guida (testi e numeri calcolati dalla KB)
  EaReviewQueue.tsx     coda di revisione con diff before/after
  EaEdgeDialog.tsx      creazione e modifica di un legame (shadcn Dialog)
  lib/ea-graph.ts       calcoli puri: trace, punteggi, aggregazioni (con test)
src/hooks/useEaKb.ts    TanStack Query su /ea/kb
```

Scelte:
- **Porting del viewer attuale in componenti React**, senza iframe. SVG disegnato a mano come oggi: nessuna libreria nuova (niente React Flow o D3) finché non serve.
- **Editing contestuale.** Nella scheda componente la sezione Relazioni mostra per ogni legame origine e stato (badge "dal codice", "curato", "manuale", "verificato") con le azioni verifica, modifica e rimuovi; in fondo "Aggiungi legame". Gli stessi controlli nella vista prodotto.
- **Revisione.** Tab visibile con `review:ea_map`: elenco delle modifiche proposte dall'ultima sync, diff leggibile, approva/rifiuta singolo o in blocco, filtro per run.
- **Stile.** Token e componenti del Hub (Tailwind, shadcn). Le scelte grafiche della mappa attuale si adattano al design system del Hub.

## 8. Migrazione dei dati attuali

1. La migrazione `0019` crea le tabelle.
2. Il comando di bootstrap `uv run python -m scripts.ea_import` carica la KB attuale (seed in `scripts/data/ea_kb_seed.json`):
   - aree, prodotti, servizi, processi, legami curati → `source = curated`;
   - legami Helm → `origin = helm`;
   - segnali QA → osservati;
   - `qa_overrides.json`, se compilato → campi manuali.
3. Da quel momento `catalog.py` e `blueprint.py` non sono più la fonte: la parte curata si modifica dall'interfaccia. Gli script restano come strumento offline e di test.

## 9. Test

- **Backend:** test pytest per estrazione (fixture con file Helm, pom e workflow reali), diff e precedenze (la sync non deve mai sovrascrivere manuale e verificato), API con permessi (403 senza permesso), CTE d'impatto.
- **Frontend:** test sui calcoli di `ea-graph.ts` (trace, punteggi) e sui componenti principali, con lo stesso stile dei test esistenti (`*.test.tsx`).
- **Validazione:** lint, type check e test di entrambi i repo prima di ogni consegna, con esito riportato.

## 10. Piano di consegna

| PR | Backend | Frontend |
|---|---|---|
| **PR1 – Lettura** | Modelli, migrazione `0019`, import, `GET /ea/kb`, `/nodes/{id}`, `/impact`, permessi e feature | Pagina `ea-map` con Blueprint, Architettura, Matrice QA, Guida; voce di menu |
| **PR2 – Editing** | Endpoint di scrittura, `ea_edit_log` (storico), attori come testo (migrazione 0020) | Dialoghi di modifica, badge di origine e stato, editing QA |
| **PR3 – Sync** | Servizio e task di sync, diff, coda, `/changes`, `/sync`, schedulazione | Tab Revisione, pulsante "Sincronizza ora", stato della sync |
| **PR4 – Rifinitura** | Notifiche, export, metriche della sync | Export, rifiniture UX |

Ogni PR passa da te prima di commit e push.

## 11. Decisioni (15/10/2026)

| # | Tema | Decisione |
|---|---|---|
| 1 | Frequenza della sync | Settimanale, più pulsante "Sincronizza ora" nel frontend |
| 2 | Credenziali Confluence | Utenza Atlassian attuale del Hub (quella di Jira) |
| 3 | Chi revisiona | `qa_manager` e `superadmin` |
| 4 | Guest | Vede la mappa, senza editing |
| 5 | Repo privato del Nodo | Il token del Hub può leggerlo: entra nella sync |
| 6 | Autenticazione lato server | Invariata come nel resto del Hub; attività di debito tecnico separata |
