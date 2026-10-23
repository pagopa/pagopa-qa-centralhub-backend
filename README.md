# QA Hub — Backend

FastAPI backend for QA Hub. Serves the REST API consumed by `qa-hub-frontend`.

## Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Docker + Docker Compose (for Postgres + Redis)

## Setup

```bash
# Install uv (if not installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync

# Start Postgres + Redis
docker-compose up -d postgres redis

# Copy env and fill in values
cp .env.example .env

# Run database migrations
uv run alembic upgrade head

# Start dev server
uv run uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs  
OpenAPI JSON: http://localhost:8000/openapi.json

## Run tests

```bash
uv run pytest
```

## Lint + type-check

```bash
uv run ruff check .
uv run mypy .
```

## Project structure

```
app/
├── main.py                 # FastAPI app factory + middleware
├── config.py               # pydantic-settings
├── deps.py                 # Shared FastAPI dependencies
├── api/v1/
│   ├── bdd.py              # Gherkin Generator: projects, scenarios, generate (SSE), settings, Ollama controls
│   ├── e2e.py              # E2E suites and runs (sync, CRUD, bulk delete)
│   ├── jira.py             # Jira KPI: /overview /trend /sanp/overview /sanp/trend /data/overview /data/trend
│   ├── gpd_position.py     # GPD Positions Data Hub: GET /snapshots, POST /sync
│   ├── docs.py             # Docs & Knowledge Base CRUD + HTML proxy
│   ├── users.py            # GET/PATCH /api/v1/users, POST /api/v1/users/sync-login
│   ├── roles.py            # GET /api/v1/roles (role/permission matrix), PATCH /api/v1/roles/{role}
│   └── ...
├── core/
│   ├── permissions.py      # ACTION_CATALOG, ACTION_KEYS, EDITABLE_ROLES, compute_role_matrix
│   └── ...                 # DB engine, security helpers
├── models/                 # SQLAlchemy ORM models (incl. Role, User.role FK)
├── schemas/
│   ├── bdd.py              # BDD request/response models (SettingsOut, ScenarioOut, ...)
│   ├── gpd_position.py     # GpdPositionSnapshotOut, GpdPositionSyncStatusOut, ...
│   ├── user.py             # UserOut, UserListResponse, UserUpdate, sync-login schemas
│   ├── role.py             # RoleOut, RoleMatrixResponse, RolePermissionUpdate
│   └── ...
├── services/
│   ├── bdd.py              # BDD CRUD + settings encryption/decryption
│   ├── bdd_ai.py           # AI provider abstraction (Ollama, Claude)
│   ├── bdd_parsers.py      # Source parsers (Confluence, PDF, DOCX, text)
│   ├── jira.py             # JiraClient (board/queue/jql), compute_overview, compute_trend
│   ├── gpd_position.py     # parse_report_line, sync_from_source, list_snapshots, get_sync_status
│   ├── github.py           # GitHubClient (Contents API + Actions API: list_workflow_runs, get_job_log)
│   ├── users.py            # sync_login, list_users, update_user
│   ├── roles.py            # get_role_matrix, update_role_permissions
│   └── ...
└── tasks/                  # Celery/Arq background workers (sync_e2e, sync_gpd_position, ...)
alembic/                    # DB migration scripts
tests/                      # pytest test suite
docker-compose.yml
```

## Environment variables

See `.env.example` for the full list. Key variables:

```env
DATABASE_URL=postgresql+asyncpg://qachub:changeme@localhost:5432/qachub
SECRET_KEY=<genera con: openssl rand -hex 32>   # usato per cifrare API key BDD in DB
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
CORS_ORIGINS=["http://localhost:3000"]
```

BDD settings (AI provider, Confluence token, Claude API key) are stored encrypted in the database and managed via `PUT /api/v1/bdd/settings`. They are **not** read from environment variables.

### Celery queue

`CELERY_QUEUE` selects the queue, exchange and routing key used by CentralHub.
The default is `qa-centralhub`, including local Docker Compose. Set the same value
in the environment of the API, worker and beat processes. Use a unique name per
application/environment when sharing Redis; do not use the generic `celery` queue.

Start a local worker with `uv run celery -A app.tasks worker --loglevel=info`.
Without `-Q`, the worker consumes the queue configured by `CELERY_QUEUE`; no
separate command override is needed when changing the queue name.
See [Helm deployment notes](helm/README.md#celery-queue-isolation) for rollout and
verification.

### Jira integration

Requires `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` in `.env`.

- **Testing board** (`BOARD_TESTING = 597`): fetched via Agile board API
- **SANP/Data queues** (`QUEUE_SANP = 1919`, `QUEUE_DATA = 1416`, `SD_PIDM = 85`): two-step fetch — issue keys from JSM Queue API, full details from `POST /rest/api/3/search/jql`
- `compute_overview` returns status/component/type/assignee breakdowns and 5 alert lists (no_estimate, backlog_old, blocked_old, open_old, in_progress_old)
- `open_old`: issue in open/pending statuses with no update for > 5 days
- `in_progress_old`: issue in progress/review statuses with no update for > 10 days

### GPD Positions Data Hub

Snapshot giornalieri delle posizioni debitorie GPD, sincronizzati dai run del workflow `gpd_report.yml` del repo `pagopa/pagopa-qa` (richiede `GITHUB_TOKEN` con accesso alle Actions di quel repo).

- `GET /api/v1/gpd-position/snapshots`: lista snapshot ordinati per `report_date` + stato ultimo sync
- `POST /api/v1/gpd-position/sync`: trigger sync manuale (503 se manca `GITHUB_TOKEN`, 502 su errori GitHub API)
- `app/services/gpd_position.py`: `sync_from_source` itera i run di successo dal più recente, scarica e concatena i log di **tutti** i job di ciascun run, estrae la riga `report data {dict}` con `parse_report_line` (regex + `ast.literal_eval`) e mappa i campi su `GpdPositionSnapshot`. Backfill di 90 giorni al primo sync, poi incrementale (si ferma al primo `run_id` già noto)
- Task Celery `sync_gpd_position_snapshots` schedulato ogni 24h (vedi `app/tasks/schedule.py`)

Vedi `../README.md` per il dettaglio dell'algoritmo di sync.

### SANP Health

SANP Health importa dal repository `pagopa/pagopa-api` i report del workflow GitHub Actions `daily_api_spec_drift_detector.yaml`. Il deployment deve fornire `GITHUB_TOKEN` con permesso repository **Actions: Read**. Il polling Celery Beat viene eseguito ogni ora e conserva i run completati negli ultimi sette giorni; i risultati MAIN sono esclusi.

Endpoint disponibili sotto `/api/v1/sanp-health`:

- `GET /reports`: storico dei run conservati;
- `GET /reports/latest`: ultimo report completo, con eventuale stato stale;
- `GET /reports/{run_id}`: matrice e dettaglio di un run;
- `GET /sync-status`: esito dell'ultimo tentativo di sincronizzazione;
- `POST /sync`: sincronizzazione manuale, usata dal pulsante **Sincronizza ora** del frontend.

Il sync manuale e quello schedulato condividono lo stesso servizio e sono serializzati. Un errore GitHub o di un artifact non elimina l'ultimo report completo; i run parziali vengono ritentati finché gli artifact restano disponibili.

### EA Map

Mappa di Enterprise Architecture della piattaforma pagoPA (Blueprint, Architettura, Data Lake, Infrastruttura, Matrice QA, Guida). Il design completo è in `docs/ea-map/DESIGN.md`; la parte Data Lake in `docs/ea-map/DESIGN-data-lake.md`; la vista Infrastruttura in `docs/ea-map/DESIGN-infra.md`.

I dati vivono nelle tabelle `ea_*` (migrazioni `0019`, `0020`, `0021` e `0023`). Il primo caricamento importa la knowledge base prodotta dagli script di discovery:

```bash
uv run python -m scripts.ea_import                         # seed in scripts/data/ea_kb_seed.json
uv run python -m scripts.ea_import path/to/kb.json --replace  # ricarica da zero
```

Endpoint disponibili sotto `/api/v1/ea`:

- `GET /kb`: knowledge base completa nel formato usato dalle viste;
- `GET /nodes/{id}`: dettaglio di un nodo con legami in ingresso e uscita, segnali QA e documentazione;
- `GET /nodes/{id}/impact?depth=3`: elementi a monte e a valle (CTE ricorsiva);
- `GET /nodes/{id}/history`: storico delle modifiche manuali (tabella `ea_edit_log`).

Endpoint di modifica (nel frontend dietro `edit:ea_map`, i dati QA dietro `edit:ea_qa`; l'attore viaggia nel body come negli altri moduli):

- `POST /edges`, `PATCH /edges/{id}`, `POST /edges/{id}/verify`, `POST /edges/{id}/remove`: aggiunta, correzione, conferma e rimozione logica di un legame;
- `POST /nodes`, `PATCH /nodes/{id}`: creazione (componente, sistema esterno, datastore) e correzione di un nodo;
- `PATCH /nodes/{id}/qa`: campi QA manuali (`under_qa`, `tas_ci_override`, `qa_owner`, `qa_notes`).

Sincronizzazione con GitHub e Confluence (settimanale via Celery beat, oppure `POST /sync` dal pulsante "Sincronizza ora"):

- `POST /sync` (202, 409 se già in corso), `GET /sync-status`, `GET /sync-runs`;
- `GET /changes?status=pending,conflict`, `POST /changes/{id}/approve` · `/reject`, `POST /changes/bulk`: coda di revisione (nel frontend dietro `review:ea_map`).

Usa `GITHUB_TOKEN` (deve poter leggere anche il repo privato del Nodo) e, per Confluence, `JIRA_BASE_URL` / `JIRA_EMAIL` / `JIRA_API_TOKEN`. Da riga di comando:

```bash
uv run python -m scripts.ea_sync --dry-run   # calcola tutto e mostra le proposte, senza scrivere
uv run python -m scripts.ea_sync             # esecuzione reale
```

Ogni legame dichiara `origin` (`helm`, `infra_rule`, `curated`, `manual`, `contract`, `contract_rule`) e `status`; solo i legami `active` e `verified` sono serviti alle viste.

#### Impatto dei rilasci e assistente della mappa

Due funzioni di sola lettura calcolate sul grafo che la mappa mostra (`app/services/ea/graph_index.py`):

- `POST /impact/release` con `{"url": "<PR o confronto GitHub>"}` oppure `{"repo", "base", "head"}`: legge i file cambiati (`GITHUB_TOKEN`), li classifica (contratto API, dati, configurazione, codice, test, pipeline, documentazione), risale a chi chiama il componente, ai dati a valle e alle dipendenze, assegna un rischio a ciascun elemento e propone le suite da eseguire, le lacune di test e i processi SANP coinvolti (`app/services/ea/release_impact.py`).
- `POST /assistant/chat` con la conversazione e il contesto della vista: Pagoda usa Gemini o Groq con function calling e solo strumenti di lettura sul grafo (ricerca, scheda, impatto, lacune QA, impatto di una PR, evidenziazione). Cita i nodi come `[[id]]`. Il provider si sceglie a runtime nei Settings Pagoda; senza la sua chiave sul backend risponde 503. La tab Impatto non usa un LLM e non dipende da questa scelta.
- `GET /assistant/settings` e `PUT /assistant/settings`: configurazione globale per ambiente, persistita nella tabella singleton `ea_assistant_settings` (migration **0024**). Campi: `provider` (`gemini` o `groq`), `gemini_model`, `groq_model`, `fallback_enabled`. Il GET restituisce anche `configured`, con due booleani sulla presenza delle chiavi, mai le chiavi stesse. Il PUT rifiuta campi extra, modelli vuoti e provider attivi senza secret. Le richieste successive rileggono la configurazione dal DB; quelle in corso non cambiano motore.

**Configurazione iniziale e deploy**

- Senza una riga salvata nei Settings si usano `EA_ASSISTANT_PROVIDER` (default `gemini`), `GEMINI_MODEL` (default `gemini-3.8-flash`) e `GROQ_MODEL` (default `openai/gpt-oss-120b`). Il fallback tra fornitori parte sempre disattivato. Dopo il primo salvataggio, provider e modelli persistiti prevalgono sui default d'ambiente. Se il provider ritira un modello, aggiornare il suo identificativo nei Settings senza rilascio; `llama-3.3-70b-versatile` e stato ritirato per gli account free/developer il 16 agosto 2026 (https://console.groq.com/docs/deprecations).
- `GEMINI_API_KEY` e `GROQ_API_KEY` restano nei secret del backend, mai nel database o nel frontend. Nei valori Helm DEV/UAT/PROD Groq usa il secret Key Vault **`qa-centralhub-groq-api-key`**: va creato in ciascun ambiente prima di distribuire quei valori. L'aggiunta o rotazione di una chiave richiede il normale aggiornamento dei secret/processi; il cambio provider/modello no.
- Applicare la migration 0024 prima di avviare questa versione. Nessun dato di configurazione viene creato durante una semplice lettura.
- Il frontend limita la pagina ai superadmin, ma **le API non implementano ancora autenticazione/autorizzazione backend**, rinviate esplicitamente per questo lavoro. L'SSO Google del frontend non protegge le chiamate dirette al backend: non esporre questi endpoint a reti non fidate.

**Fallback opzionale**

Un 429 di Groq indica un limite di richieste o token: se il provider invia `Retry-After`,
la risposta di errore mostra il tempo di attesa in secondi, senza tentativi immediati.
Le quote esatte dipendono dall'organizzazione: https://console.groq.com/settings/limits.
Una singola domanda di Pagoda puo richiedere piu chiamate al modello, fino al limite di 8 passi,
e ogni passo reinvia prompt, strumenti e cronologia. Un 503/504 Gemini indica invece
indisponibilita o timeout, non prova che il modello non esista o sia utilizzabile in quel momento.

Groq riceve inoltre istruzioni specifiche per mantenere testo descrittivo e riferimenti
cliccabili insieme: `[[id]]` sostituisce solo il nome, non la spiegazione della relazione.
Una dipendenza e descritta come impatto potenziale, non come guasto certo.
Il prompt include esempi di riferimenti `[[id]]` corretti
e formati da evitare, a ogni passo del ciclo. Gli esempi non sono dati del grafo:
gli ID devono provenire dagli strumenti. Questo rafforza il prompt, ma non garantisce
la conformita del modello; i pulsanti degli elementi evidenziati restano una protezione UI.

Se abilitato, dopo errori temporanei (429, 500, 502, 503, 504 o problemi di trasporto) e tentativi limitati, si prova una sola volta l'altro provider. Richiede entrambe le chiavi. La nuova analisi riparte dai messaggi originali e dal contesto: non si trasferiscono chiamate interne o firme Gemini, né risultati parziali della prima esecuzione. Non si effettua fallback per errori di autenticazione/configurazione o risposte Groq non valide. Domande e risultati degli strumenti possono quindi essere inviati a entrambi i fornitori: verificare le policy aziendali prima dell'attivazione. Gemini mantiene anche il fallback fra modelli in `GEMINI_FALLBACK_MODELS`; questo avviene prima dell'eventuale passaggio a Groq.

Come risponde l'assistente (`chat()` in `assistant.py`):

1. il frontend invia gli ultimi messaggi (al massimo 12, `MAX_HISTORY`) e il contesto (`view`, `node_id`), che diventa una riga in coda al prompt di sistema;
2. il prompt di sistema fissa le regole: usare solo i dati degli strumenti, cercare l'id con `cerca_nodi` prima di parlare di un elemento, citare i nodi come `[[id]]`, chiamare `evidenzia` prima della risposta finale;
3. ciclo modello ↔ strumenti, al massimo 8 passi (`MAX_STEPS`): il modello restituisce una o più chiamate di funzione, il backend le esegue sul `GraphIndex` in memoria e rimanda il risultato in JSON; quando il modello non chiede più strumenti, il suo testo è la risposta;
4. controllo finale: ogni `[[id]]` citato viene cercato nel grafo; se esiste diventa un collegamento e si aggiunge ai nodi evidenziati, se non esiste resta testo semplice. La risposta contiene `answer`, `highlight` (al massimo 40 id), `labels`, `steps`, `provider`, `model` effettivo e `fallback_used`.

Il modello non ha accesso al database e non scrive nulla: vede solo i risultati degli strumenti. Non c'è memoria tra una conversazione e l'altra. Con Gemini i risultati delle funzioni sono inviati con ruolo `user` e il contenuto originale del modello viene ripassato per conservare le `thought_signature`; il modello che ha risposto per primo resta quello dei passi successivi della singola richiesta. Groq usa l'API Chat Completions compatibile OpenAI via `httpx`, con schemi JSON normalizzati e `tool_call_id` per ogni risultato, anche con chiamate multiple nello stesso turno. Il formato del ciclo resta neutro (`ChatModel`).

#### Live: esiti dei test sulla mappa

`GET /live?env=DEV&days=7` legge le run automatiche già salvate nel Hub (`qachub.test_runs`, NRT, TAS, CI…) e per ogni `test_object` restituisce l'ultimo esito, lo storico e il pass rate. Il `test_object` si collega ai componenti tramite i segnali QA della mappa (suite NRT e suite centrali, anche per suffisso: `ecommerce-cdc` → `api/ecommerce-cdc`), oppure al componente o prodotto con lo stesso nome; quelli non collegati sono restituiti in `unmapped` (`app/services/ea/live.py`). Per provarlo in locale:

```bash
uv run python -m scripts.ea_live_demo            # 7 giorni di run demo in DEV (test_origin=ea-live-demo)
uv run python -m scripts.ea_live_demo --clear    # le rimuove
```

#### Flussi di pagamento

`GET /flows` serve i flussi di pagamento passo per passo, dai casi d'uso delle SANP 3.12.0 (avviso presso PSP, frontend dell'Ente, Checkout, App IO, Rendicontazione) sui nodi reali della mappa. La sequenza dei passi non è nel codice: è una bozza curata dalle SANP in `app/services/ea/flows.py`, servita con `validated = false`. Per ogni passo l'API dice se la mappa ha già il legame usato (`on_map`, con tipo e origine), così flussi e mappa si verificano a vicenda.

#### Data Lake

La sync legge anche i data contract di [`pagopa/pdnd-data-contracts`](https://github.com/pagopa/pdnd-data-contracts) (solo `src/data/pagopa`, formato datacontract.com 0.9.3) e li porta nella mappa (`app/services/ea/contracts.py`):

- ogni contratto diventa un nodo `dataset` con id `dc:<urn senza "urn:">`; il livello è ricavato dal server (`kafka` → ingestion, file `s3`/`csv` → export) o dal nome della tabella (`silver_*`, `gold_*`);
- le tabelle citate dai contratti ma senza un contratto proprio diventano dataset non contrattualizzati (`dl:<db.tabella>`);
- i legami `derives` (lineage tra dataset, dalle descrizioni e da `quality.dataset`) e `feeds` (verso i consumer: BI per le gold, MEF e Banca d'Italia dove dichiarati) sono applicati in automatico con `origin = contract`;
- i legami `produces` (componente → ingestion) non sono nei contratti: la sync li propone in Revisione confrontando il topic Event Hub con i componenti (`PRODUCER_RULES`, `origin = contract_rule`, con confidenza);
- il repo viene riletto solo se ha un push più recente dell'ultimo snapshot; se non è leggibile la run è `partial` e i dataset restano com'erano;
- un contratto rimosso genera una proposta di archiviazione; i legami verificati non più dichiarati diventano conflitti.

Correzioni manuali sui dataset (`PATCH /nodes/{id}`, conservate dalle sync successive; `null` torna al valore del contratto): `layer` (→ `attrs.layer_override`), `doc_url` (→ `attrs.doc_url_override`), `slack_url` (→ `attrs.slack_url_override`). Non segnano il dataset come verificato, quindi nome e descrizione continuano ad arrivare dal contratto.

Copertura Data Quality (`app/services/ea/dq_overlay.py`): i controlli del DQ Framework (`dq_control_instances`, esclusi gli eliminati) sono collegati ai dataset tramite `table_ref`; i controlli sulle tabelle bronze vanno all'ingestion che le alimenta (stesso nome senza prefisso di livello). `GET /kb` espone per ogni dataset `dq` (controlli) e `dqActive` (attivi); `GET /nodes/{id}` aggiunge `dq` con il riepilogo per dimensione, l'allineamento con i check del contratto e l'elenco dei controlli.

Il `GITHUB_TOKEN` deve poter leggere `pagopa/pdnd-data-contracts`.

#### Infrastruttura

La stessa sync legge il Terraform di `pagopa/pagopa-infra`, `pagopa/pagopa-infra-core` e `pagopa/pagopa-aws-infra` (`INFRA_REPOS` in `app/services/ea/infra/config.py`) e popola `ea_infra_resources` e `ea_infra_links` (`app/services/ea/infra/`):

- `parser.py` legge `.tf` e `env/*/terraform.tfvars` per stack (cartella), classifica ogni `resource`/`module` in un tipo logico (`TYPE_RULES`), valuta per ambiente `var.*`, `local.*`, interpolazioni e `format()` per ricavare il nome Azure e il `count` (es. alert solo prod); i blocchi di contorno (secret, access policy, named value...) sono solo contati;
- `resolve.py` abbina ogni `data` alla risorsa con stesso tipo Azure e nome nello stesso ambiente, in qualunque stack o repo (anche se creata da un modulo, `MODULE_AZURE_TYPES`): il link risultante è `inferred`; i riferimenti diretti sono `explicit`; `resource_group_name`, `subnet_id`, `namespace`... diventano il contenitore (`parent_id`). Alert → risorsa monitorata (`monitors`) e alert → action group (`notifies`) compongono la catena di monitoring;
- risorse, link e riepiloghi per repository sono applicati subito; i collegamenti risorsa → nodo della mappa (namespace → `ns-*`, API/Function con nome uguale a un componente) sono proposte in Revisione (entità `infra`); approvate valorizzano `ea_node_id`. Le risorse sparite diventano `removed`; un repo non leggibile tiene l'ultima foto e rende la run `partial`, uno non visibile al token viene saltato;
- subscription, GUID e IP non sono salvati (i tfvars non si conservano, le stringhe sono mascherate). `EA_INFRA_INCLUDE_NETWORK_DETAILS=true` aggiunge gli address prefix di VNet e subnet: lasciarlo spento finché il backend non verifica il JWT, perché la vista è visibile come il resto della mappa.

Endpoint: `GET /api/v1/ea/infra?env=prod|uat|dev` (risorse, link, alert, action group e canali dell'ambiente), `GET /api/v1/ea/infra/resources/{id}` (dettaglio con link entranti e uscenti); `GET /nodes/{id}` include `infra`, le risorse collegate al nodo.

### Auth & RBAC

L'autenticazione (Google SSO) e l'enforcement dei permessi avvengono lato `qa-hub-frontend`; il backend espone i dati su cui si basa la RBAC e resta volutamente "open" (nessun middleware di auth sulle route API).

- **`roles`** (key, label, is_system, permissions JSON, updated_at) e **`users`** (con FK `role` → `roles.key`, default `"guest"`): tabelle introdotte dalla migration `0008`, che le seed con i 5 ruoli di sistema (`superadmin`, `qa_manager`, `qa_analyst`, `qa_engineer`, `guest`).
- **`app/core/permissions.py`**: `ACTION_CATALOG` definisce le 10 azioni RBAC (es. `view:bdd`, `manage:bdd`, `sync:trigger`, ...) raggruppate per categoria, `ACTION_KEYS` la lista delle chiavi, `EDITABLE_ROLES` i ruoli non di sistema editabili da `superadmin`, `compute_role_matrix` calcola la matrice ruolo→permessi.
- **`POST /api/v1/users/sync-login`**: chiamata dal frontend ad ogni login/refresh JWT — crea o aggiorna l'utente (email, nome, idp_sub) e restituisce ruolo e stato attivo.
- **`GET/PATCH /api/v1/users`**: lista utenti e aggiornamento ruolo/stato (`is_active`), usati dalla tab "Utenti" di `/settings/users`.
- **`GET /api/v1/roles`**: restituisce `RoleMatrixResponse` (catalogo azioni, ruoli, matrice permessi) usato da `usePermissions`/`useRoleMatrix` nel frontend.
- **`PATCH /api/v1/roles/{role}`**: aggiorna i permessi di un ruolo non di sistema (usato dalla tab "Ruoli & Permessi").

### Feature visibility

Il catalogo delle pagine controllabili vive in `app/core/features.py`; include le pagine operative, API Docs e Settings E2E/Gherkin. Gli override globali sono persistiti nella tabella `feature_visibility` introdotta dalla migration `0015`. Una feature senza override è abilitata per default, incluse le voci aggiunte in seguito al catalogo.

- `GET /api/v1/features`: restituisce catalogo, route, permesso RBAC associato e stato effettivo;
- `PATCH /api/v1/features/{feature_key}` con `{ "enabled": boolean }`: salva un override per l'ambiente corrente.

Il frontend richiede sia la feature abilitata sia il permesso RBAC del ruolo. In linea con l'architettura corrente, gli endpoint backend restano aperti; disabilitare API Docs nasconde il link dal portale, ma non blocca l'URL Swagger diretto.

### Docs HTML proxy

`GET /api/v1/docs/proxy?url=<url>` fetches the page server-side, converts GitHub blob URLs to raw.githubusercontent.com, injects `<base href>`, strips `X-Frame-Options`, and returns an `HTMLResponse`. Used to embed external HTML pages as iframes without CSP/CORS issues.

## Design reference

See `../design_handoff_qa_webportal/README.md` for the full spec and design system.
