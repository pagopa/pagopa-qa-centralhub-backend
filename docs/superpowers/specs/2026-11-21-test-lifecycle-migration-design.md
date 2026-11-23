# Test Lifecycle — Fase 5: Migrazione da Confluence — Design

Data: 2026-11-21 · Stato: da approvare · Estende: `2026-11-20-test-lifecycle-design.md` §12

## 1. Perché

Il ciclo di vita dei test vive oggi su Confluence (spazio QDP, albero "Testing Suites") e in 61 feature file sul repo dei test che l'hub non gestisce (nessuno ha l'header `# qa-hub:`). Per dismettere Confluence bisogna portare nell'hub le feature esistenti, con i loro metadati e il collegamento a Git e agli NRT, senza perdere storico né rompere le run.

Fuori da questo documento: la creazione di feature a partire da file scritti direttamente su Git (flusso "Git-first" continuo), che l'utente descriverà dopo; gli Studi di contesto (fase 6).

## 2. Cosa c'è oggi (rilevato il 2026-11-21)

**Confluence** — 37 pagine FEAT sotto "Testing Suites" (id 2815950851): Checkout 15, FdR3 7, FdR1 3, CUP 1, WISP 7 (NodoInviaRPT 001–003, NodoInviaCarrelloRPT 004–007), più una pagina duplicata "Test FEAT_001_NodoInviaRPT…".
- Tabella "Informazioni generali": ID documento, Nome documento, Ambito, Stato del documento, Owner, Reviewer (spesso il team `qa_ts300`), Riferimenti.
- Tabella "Dettagli": Tipologia Test, Link Requisiti, Jira Progettazione (QA), Epic/Story Jira (Product), Feature File, Jira Implementazione Suite, Jira Esecuzione, Estimate.
- Scenari: sezione "Contesto" (lista di step) e, per ogni scenario, un titolo "Scenario NN: …" con una tabella Precondizioni (Given) / Step (When) / Esito atteso (Then), celle con step elencati ("- Dato …", "- E …").
- Stati in testo libero: "test suite implemented", "Approvato", "WIP", "IN PROGRESS".
- Le pagine vengono aggiornate da una GitHub Action del repo ("Updated feature file content via GitHub Action").

**Git** (`main`) — 61 `.feature`:
- tag identità eterogenei (`@FEAT_001_Checkout_Attivazione_Pagamento_001`, `@FEAT_008_Checkout`, `@FEAT_001_CUP_scenario_01`, `@eBollo`), header Confluence finti (`#0123456789`), nomi file con spazi, cartelle doppie `fdr` / `fdr3`, copie in `docs/use-cases/features`, fixture `tas_pass|tas_mixed|tas_fail`;
- doppioni di numero: due `FEAT_005_Checkout` su Confluence (Ordinamento PSP, Back Navigation Control) e `@FEAT_003_Checkout` usato da due file diversi.

**NRT** — le run selezionano gli scenari solo con tag di categoria (`.github/nrt-suites.json`: wisp `@runnable`, checkout `@e2e ~@skip`, fdr3 `@runnable`; idem `run_behave_tests.yml`, `wisp-tests.yml`, `checkout-e2e-tests.yml`). Rinominare i tag identità non rompe le run se i tag di categoria restano.

## 3. Obiettivi, non-obiettivi, anti-obiettivi

**Obiettivi**
- Portare nell'hub tutte le feature FEAT con metadati, scenari, stato e chiavi Jira.
- Collegare ogni feature migrata al suo file su Git e allineare i file allo standard dell'hub.
- Conservare lo storico NRT tramite alias dei vecchi tag.
- Marcare le pagine Confluence come migrate, con link all'hub.

**Non-obiettivi**
- Import continuo di file nuovi scritti su Git (discusso a parte).
- Studi di contesto.
- Migrare il campo "Tipologia Test" e il task "Jira Esecuzione" (eliminati dal processo).

**Anti-obiettivi**
- Una migrazione "magica" che decide da sola i casi ambigui: ogni dubbio va mostrato a una persona.
- Doppioni nell'hub se la migrazione viene rieseguita.
- Run NRT rotte o storico NRT perso dopo l'allineamento.

## 4. Decisioni prese con l'utente

| # | Decisione | Scelta |
|---|---|---|
| M1 | Come si esegue | **Pagina "Migrazione" nell'hub**: analisi a vuoto, proposte da confermare/correggere riga per riga, poi "Esegui"; rieseguibile |
| M2 | File su Git | **PR di allineamento per ambito**: header `# qa-hub:`, tag identità `@<SCOPE>_<NNN>_<NN>`, tag di categoria mantenuti, vecchi tag come alias |
| M3 | Pagine Confluence | **Le aggiorna l'hub**: all'esecuzione di un ambito, avviso "Migrata su QA Hub" con link in cima a ogni pagina migrata |

## 5. Approcci considerati

- **Esecuzione** (M1): pagina nell'hub con prova a vuoto (scelto) vs script una tantum con file di correzioni. Il primo costa di più ma mette le decisioni davanti al team ed è rieseguibile.
- **File su Git** (M2): PR di allineamento completo (scelto) vs solo header con tag invariati (due standard per sempre) vs nessuna modifica e tracciamento per percorso (fragile ai rename, nessun controllo convenzioni).
- **Confluence** (M3): avviso scritto dall'hub (scelto) vs elenco da aggiornare a mano.

## 6. Flusso

### 6.1 Analisi (nessuna scrittura)
L'hub legge:
- le pagine figlie di "Testing Suites" il cui titolo inizia con `FEAT_` (le altre — guide, template, riepiloghi, studi di contesto — sono ignorate; la pagina "Test FEAT_…" viene mostrata come probabile doppione);
- tutti i `.feature` su `main` del repo dei test.

Per ogni pagina e ogni file produce una **riga di migrazione** con una proposta:
- **abbinamento pagina ↔ file**: per codice FEAT + ambito, poi per somiglianza del titolo; il link "Feature File" della pagina, se presente, vince;
- **ambito**: dal prefisso del codice (`FEAT_001_FdR3` → `FdR3`), creato se non esiste; area dall'"Ambito" della pagina (FdR, WISP, Checkout…); cartella del repository dalla cartella del file;
- **numero**: quello del codice FEAT; il contatore dell'ambito riparte dal massimo importato;
- **stato**: test suite implemented → IMPLEMENTED; Approvato → DESIGNED; WIP / IN PROGRESS / altro → DRAFT; senza pagina, file su `main` → IMPLEMENTED;
- **persone**: owner e reviewer abbinati agli utenti dell'hub per nome o email; un team (es. `qa_ts300`) o una persona non trovata → campo vuoto, segnalato;
- **Jira**: Jira Progettazione → `jira_design_key`, Jira Implementazione Suite → `jira_impl_key`, Epic/Story Product → `product_issue_key` della revisione; "TBD" o vuoto ignorati; chiavi verificate come nella fase 3;
- **altri campi**: Link Requisiti → `requirements_url`; descrizione dalla riga sotto "Dettagli"; stima "Test Automatici" → `impl_estimate_days` se numerica.

**Contenuto degli scenari**
- File presente: scenari, Contesto ed Esempi dal file (Git è la fonte del testo); dalla pagina solo i metadati.
- Solo pagina: scenari estratti dalle tabelle Given/When/Then (titolo da "Scenario NN: …", step dalle celle, prefissi "Dato/Quando/Allora/E" normalizzati); se l'estrazione non riesce, il testo originale va nella **nota di migrazione** della feature e lo scenario resta da scrivere.

**Problemi mostrati come righe da decidere**: numero già usato nello stesso ambito, file senza pagina, pagina senza file, più file per la stessa pagina, tag identità duplicati, file illeggibili, persone non trovate, tag sconosciuti (§6.4), fixture e copie (`tas_*`, `docs/use-cases`). Per ogni riga l'utente può **confermare**, **correggere** (ambito, numero, abbinamento, stato, persone) o **escludere**.

### 6.2 Esecuzione per ambito
"Esegui" lavora su un ambito alla volta (le righe confermate di quell'ambito):
- crea ambito e feature con revisione pubblicata (IMPLEMENTED/DESIGNED con file) o di lavoro (DRAFT), scenari con tag nuovi `@<SCOPE>_<NNN>_<NN>`, alias dei vecchi tag identità, collegamento al percorso del file;
- registra nello storico un evento `tl.feature.migrated` con l'origine (id pagina, percorso, commit);
- **idempotenza**: ogni riga ricorda l'id pagina / percorso migrato; una riesecuzione salta ciò che è già nell'hub e aggiorna solo le righe nuove o corrette ancora non eseguite;
- scrive l'avviso sulle pagine Confluence dell'ambito (§6.5);
- apre la PR di allineamento dell'ambito (§6.3).

### 6.3 PR di allineamento (una per ambito)
Per ogni file migrato dell'ambito l'hub rigenera il contenuto nel suo standard, **mantenendo il percorso** del file (nessuno spostamento o rinomina in questa fase):
- header `# qa-hub: <codice>` al posto di eventuali header Confluence;
- tag identità di feature e scenari nel formato dell'hub;
- tag di categoria mantenuti (§6.4);
- step, titoli, Esempi e Contesto invariati nel significato (la rigenerazione segue il formato dell'hub).

Branch `qa-hub/migration-<scope>`, PR verso `main` con l'elenco dei tag rinominati. Al merge, il sync importa i file come per qualsiasi PR dell'hub: le feature risultano gestite e lo storico NRT resta collegato tramite gli alias. Fino al merge le feature migrate mostrano "allineamento in attesa" e il sync non le tocca.

### 6.4 Tag non identità
I tag che non identificano feature o scenari (`@runnable`, `@e2e`, `@skip`, `@ui`, ma anche `@regression`, `@checkout`, `@auth`, `@positivo`, `@eBollo`, …) non vanno persi: l'analisi li elenca e per ognuno l'utente sceglie **mantieni come tag di categoria** (aggiunto alla lista controllata dell'hub) o **rimuovi**. Default: mantieni.

### 6.5 Confluence
All'esecuzione di un ambito, per ogni pagina migrata l'hub aggiunge in cima un pannello "Migrata su QA Hub — questa pagina non è più aggiornata" con il link alla feature. Usa le credenziali Atlassian già configurate; scrive solo sulle pagine migrate dello spazio QDP. A fine migrazione (fuori dall'hub, a cura del team): spegnimento della GitHub Action che aggiorna Confluence, sola lettura sull'albero, sostituzione della "Guida alla Creazione della Pagina Testing Suite" con la guida dell'hub.

## 7. Vincoli

- Stessi vincoli della spec principale (§14); permesso `manage:tl_migration`.
- Nessuna scrittura su Confluence fuori dalle pagine migrate; nessuna scrittura Jira (le chiavi si verificano soltanto).
- La migrazione non scrive mai su `main` direttamente: solo PR, con review di un CODEOWNER.
- Test automatici senza chiamate reali a Confluence, GitHub o Jira; in locale la modalità Git locale vale anche per le PR di allineamento.

## 8. Calls mie (da verificare leggendo)

- Esecuzione per ambito, non globale.
- Percorsi dei file mantenuti (nessuna rinomina in migrazione).
- Stato dei file senza pagina: IMPLEMENTED.
- Tag non identità mantenuti per default.
- Team come reviewer (`qa_ts300`) → reviewer vuoto.
- Pagina "Test FEAT_…" proposta come doppione da escludere.
- La rigenerazione dei file perde i commenti interni (es. separatori `# Happy Path …`): l'hub non li conserva. La PR li elenca per file, così il team li vede prima del merge.
- "Esegui" rilegge pagine e file prima di scrivere: se qualcosa è cambiato dopo l'analisi, la riga torna "da confermare".

## 9. Lasciato al builder

- Struttura delle tabelle di migrazione (righe, decisioni, esiti) e delle API.
- Algoritmo di somiglianza dei titoli e soglia.
- Parsing delle tabelle Confluence (formato storage o ADF).
- Layout della pagina "Migrazione".
