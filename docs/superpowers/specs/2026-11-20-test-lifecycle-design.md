# Test Lifecycle — Design

Data: 2026-11-20 · Stato: bozza da approvare · Repo coinvolti: `qa-hub-backend`, `qa-hub-frontend`, `pagopa/pagopa-platform-integration-test`

## 1. Perché

Oggi il ciclo di vita dei test (studio → progettazione → feature file → implementazione → esecuzione NRT) vive su Confluence, spazio QDP, alberatura "Testing Suites": ~40 pagine `FEAT_XXX_<Ambito>_<Descrizione>` in cartelle per ambito, più gli "Studio di contesto". Il team ci lavora male e non c'è standard:

- stati, naming, owner/reviewer e link Jira sono testo libero, spesso "TBD";
- i tag nei feature file non sono uniformi (`@Fdr3_001_01`, `@FEAT_002_Checkout`, `@CUP_001_01`), molti file non hanno l'id della pagina Confluence, ci sono cartelle duplicate (`fdr` / `fdr3`) e feature file anche in `docs/use-cases`;
- non c'è legame verificabile tra scenario progettato, file su Git e risultato NRT.

**Obiettivo:** QA Hub diventa l'unico sistema di riferimento per il ciclo di vita dei test e **Confluence viene dismesso** per questo uso. Il valore sta nei vincoli imposti dal sistema (naming, stati, campi obbligatori, identità degli scenari), nella chiarezza visiva, nell'integrità, nella manutenibilità e nello storico.

## 2. Obiettivi, non-obiettivi, anti-obiettivi

**Obiettivi**
- Gestire nell'hub Studi di contesto, Feature e Scenari con un workflow a stati vincolato.
- Generare il feature file dall'hub e aprire una PR sul repo dei test; dopo il merge Git è la fonte del testo, l'hub versiona e verifica.
- Creare dall'hub i task Jira PQ previsti dal processo e verificare i link di prodotto.
- Collegare ogni scenario alle sue esecuzioni NRT tramite tag stabile.
- Migrare una tantum le pagine FEAT esistenti e i feature file del repo.

**Non-obiettivi**
- Test manuali: il team non li fa. Il campo "Tipologia Test" sparisce, tutto è automatico.
- Task Jira "Esecuzione": non usato, eliminato dal processo.
- Editing degli step definition / codice Python dei test nell'hub.
- Riuso del modulo BDD "Gherkin Generator" (`bdd_*`, `/bdd`): è obsoleto. La sua rimozione è un lavoro separato.

**Anti-obiettivi** (sarebbe un fallimento anche se funziona)
- Un sistema così rigido che il team lo aggira scrivendo direttamente su Git o tornando su Confluence.
- Un secondo posto dove lo stato "vero" diverge: hub e Git non devono mai mostrare due verità diverse senza che l'hub lo segnali.
- Sincronizzazione bidirezionale automatica del testo (hub ↔ Git) con merge impliciti.

## 3. Decisioni prese (con l'utente)

| # | Decisione | Scelta |
|---|---|---|
| D1 | Ruolo dell'hub | Sistema di riferimento; Confluence dismesso |
| D2 | Chi possiede il Gherkin | **A** — si progetta nell'hub, l'hub genera il `.feature` e apre una PR |
| D3 | Modifiche dopo la generazione | **2** — Git può modificare; l'hub reimporta a ogni merge, versiona, mostra diff, segnala le modifiche strutturali; la CI del repo controlla solo le convenzioni |
| D4 | Jira | **b** — l'hub crea i task PQ (Progettazione, Implementazione suite) e verifica i link Epic/Story di prodotto (CHK, PAGOPA, …) |
| D5 | Test manuali | Fuori perimetro (non esistono) |
| D6 | Task Jira Esecuzione | Eliminato dal processo |
| D7 | Editor scenari | **Ibrido** — struttura (id, tag, titolo, tipo, header, Contesto) gestita dal sistema; step scritti in un mini-editor Gherkin con validazione e autocompletamento |

## 4. Approcci considerati per l'editor (D7)

1. **Editor Gherkin libero** (intero file): veloce, diff semplice, ma header/tag/id restano a mano e lo standard torna a dipendere dalla disciplina.
2. **Editor a moduli** (una riga per step, griglia esempi): sintassi sempre corretta, ma lento e fragile nel reimportare modifiche fatte su Git.
3. **Ibrido** — scelto. Vincoli rigidi esattamente su ciò che lega feature ↔ Git ↔ NRT, libertà sugli step; il diff con Git avviene per scenario, quindi le modifiche strutturali si rilevano facilmente. L'autocompletamento dagli step già esistenti nel repo spinge al riuso.

## 5. Modello di dominio

Tutte le tabelle nuove stanno nello schema `qachub`, accanto alle tabelle NRT.

- **Ambito** (`tl_scopes`): anagrafica gestita. Campi:
  - **codice** breve (es. `Checkout`, `CUP`, `FdR3`, `FdR1`, `NodoInviaRPT`): usato sia nel codice FEAT (`FEAT_001_FdR3`) sia, in maiuscolo, come prefisso del tag (`@FDR3_001_01`). È immutabile dopo la creazione della prima Feature;
  - nome visualizzato (modificabile);
  - area opzionale per raggruppare (es. FdR1 e FdR3 sotto "FdR", NodoInviaRPT e NodoInviaCarrelloRPT sotto "WISP"), come le cartelle Confluence di oggi;
  - cartella nel repo (es. `src/integration/fdr/features`), team owner, collegamento a una o più `test_suites` NRT (`test_object`).
  *Scelta mia:* gli "ambiti" delle pagine attuali sono in realtà sotto-ambiti (FdR1/FdR3), da cui il livello "area".
- **Studio di contesto** (`tl_context_studies`): documento per Ambito (o per gruppo di feature) con sezioni fisse dal template attuale: obiettivo e scope, contesto (attori, dipendenze), modello a stati, mappa API, macro-scenari, note tecniche, rischi. Contenuto rich-text/markdown per sezione. Stato: bozza / in review / approvato. Una Feature può riferire uno Studio.
- **Feature** (`tl_features`): l'attuale FEAT. Campi:
  - codice `FEAT_<NNN>_<Ambito>` assegnato dal sistema (progressivo per Ambito, immutabile);
  - titolo e descrizione (`Funzionalità:` + narrativa);
  - stato (§6);
  - owner design (QA Analyst), reviewer, owner implementazione (QA Engineer): utenti dell'hub;
  - link requisiti (URL), Epic/Story prodotto (chiave Jira verificata, oppure flag "reverse engineering");
  - task Jira Progettazione e Implementazione (chiavi create dall'hub);
  - stima progettazione e implementazione (G/U);
  - Contesto Gherkin (step `Dato`/`E` comuni);
  - percorso del file su Git, ultimo commit sincronizzato, PR aperta.
- **Scenario** (`tl_scenarios`): figlio della Feature. Campi: numero progressivo (`01`, `02`, … immutabile, mai riusato), tag identità `@<PREFISSO>_<NNN>_<NN>`, titolo, tipo (`Scenario` / `Schema dello scenario`), polarità (positivo/negativo), tag aggiuntivi da lista controllata (es. `@runnable`), step (testo Gherkin), tabella Esempi, link di dettaglio a requisito/anchor, stato logico (attivo / rimosso). Gli scenari rimossi restano per lo storico.
- **Revisioni** (`tl_feature_revisions`), *scelta mia*: il contenuto (metadati di progettazione, Contesto, scenari) appartiene a una revisione, non direttamente alla Feature. Una Feature ha:
  - al massimo una **revisione pubblicata**: l'ultima approvata e allineata a Git;
  - al massimo una **revisione di lavoro** aperta: bozza, in review o approvata con PR aperta.
  Non possono esistere due revisioni di lavoro aperte nello stesso momento.
- **Versioni** (`tl_feature_versions`): snapshot immutabile a ogni approvazione, generazione e import da Git. Origine: `hub` | `git`, autore, commit SHA, diff rispetto alla versione precedente. Le revisioni sono il "lavoro", le versioni sono lo "storico".
- **Alias scenario** (`tl_scenario_aliases`), *scelta mia*: tag e nomi scenario precedenti (pre-normalizzazione o rinomine) mappati sullo Scenario. Servono a non perdere lo storico NRT (pass rate, flaky) quando i tag cambiano.
- **Review** (`tl_reviews`, `tl_comments`): commenti per scenario o per feature, esito del reviewer (approvato / modifiche richieste).
- **Anomalie** (`tl_issues`): segnalazioni aperte dal sistema (drift strutturale da Git, tag mancanti, file orfani, errori di migrazione) con stato aperta / riconosciuta / risolta.
- **Storico eventi**: transizioni di stato, approvazioni, PR, creazioni Jira. Si riusa `audit_logs` con `resource_type = "tl_feature"`, oppure una tabella dedicata se servono query di timeline più ricche (scelta del builder).

## 6. Workflow a stati della Feature

| Stato | Condizioni per entrarci | Effetti automatici |
|---|---|---|
| **DRAFT** | creata con Ambito, titolo, owner design, Epic/Story prodotto o flag reverse engineering | assegnazione codice FEAT |
| **IN REVIEW** | ≥1 scenario, Gherkin valido in tutti gli scenari, reviewer assegnato (≠ owner), task Jira Progettazione esistente | notifica al reviewer |
| **DESIGNED** | reviewer approva | snapshot versione; generazione `.feature` + PR sul repo; creazione task Jira "Implementazione suite"; assegnazione owner implementazione |
| **IMPLEMENTED** | PR mergiata su `main` e file su Git coerente (nessuna anomalia strutturale aperta) | import da Git come nuova versione |
| **RUNNING** | **automatico**: almeno uno scenario della feature compare in una run NRT (match per tag identità) | — |

Regole:
- "Modifiche richieste" in review riporta a DRAFT.
- **Due livelli di stato** (*scelta mia*, conseguenza del modello a revisioni del §5):
  - lo **stato della Feature** (DRAFT → … → RUNNING) descrive la revisione pubblicata;
  - la **revisione di lavoro** ha un suo stato di progettazione: BOZZA → IN REVIEW → APPROVATA (PR aperta) → MERGIATA. Con il merge diventa la nuova revisione pubblicata.
  - Per una Feature nuova i due coincidono finché non c'è il primo merge.
  - Riaprire una Feature già DESIGNED/IMPLEMENTED/RUNNING crea una revisione di lavoro. La Feature resta nel suo stato (per esempio RUNNING) e la UI mostra un badge "revisione in corso".
- Le transizioni manuali sono permesse solo ai ruoli previsti (§10).

## 7. Integrazione Git (`pagopa/pagopa-platform-integration-test`)

**Generazione del file (hub → Git)**
- Formato generato, deterministico:
  ```
  # qa-hub: FEAT_001_FdR3
  # language: it
  @FDR3_001
  Funzionalità: <titolo>
    <descrizione>

    Contesto:
      Dati ...

    @runnable @positivo
    @FDR3_001_01
    Scenario: <titolo>
      Dato ...
  ```
  La prima riga sostituisce l'attuale `#<idPaginaConfluence>`.
- L'hub crea un branch `qa-hub/FEAT_001_FdR3-r<n>`, fa il commit del file nella cartella dell'Ambito e apre una PR verso `main` con il link alla Feature sull'hub. Per le feature già esistenti modifica il file al suo percorso attuale.
- Serve un token con permesso di scrittura su contents e pull request del repo. Oggi `github_token` è read-only e `tas_github_token` serve al dispatch dei workflow TAS sullo stesso repo: si valuta se estenderlo o aggiungerne uno dedicato (scelta del builder con chi gestisce i token).

**Import (Git → hub)**
- Trigger: webhook GitHub su push a `main` sui path `**/*.feature`, oppure un job Celery periodico di fallback che confronta l'ultimo SHA.
- Il file viene parsato con un parser Gherkin vero (libreria `gherkin-official` o equivalente), non con regex.
- Il match file ↔ Feature avviene tramite l'header `# qa-hub:`; il match scenario ↔ Scenario tramite il tag identità.
- Classificazione delle differenze:
  - **testuali** (step, titoli, esempi di scenari esistenti): accettate, nuova versione con origine `git` e diff visibile;
  - **strutturali** (scenario aggiunto senza tag, tag identità cambiato/duplicato/rimosso, scenario eliminato, header mancante): nuova versione + anomalia aperta da far riconoscere all'owner. Uno scenario aggiunto su Git con un tag nuovo e valido viene proposto come nuovo Scenario da confermare.

**Modifiche concorrenti hub ↔ Git** (*scelta mia*)
- L'import da Git non si blocca mai: aggiorna sempre la revisione pubblicata, perché Git è la verità su ciò che è eseguito.
- Se nel frattempo esiste una revisione di lavoro, questa viene marcata **"base superata"**. L'hub mostra per ogni scenario il confronto a tre: base della revisione, Git attuale, bozza.
- Finché l'owner non riallinea la bozza (accettando o scartando le modifiche arrivate da Git, scenario per scenario), la revisione non può essere approvata.
- Se la PR dell'hub è già aperta e la base cambia, la PR viene segnata come superata. Dopo il riallineamento, l'hub aggiorna lo stesso branch della PR.
- Nessun merge automatico del testo.

**Controllo convenzioni in CI (nel repo dei test)**
- Una GitHub Action sulle PR che toccano `*.feature` controlla: header `# qa-hub:` presente, `# language: it`, tag identità presenti, univoci e con il prefisso dell'Ambito della cartella, nessun riuso di numeri. Opzionalmente chiede all'hub (API) se il codice FEAT esiste.
- Non controlla il contenuto degli step (D3).
- La Action `syncFeatureFile.yaml` (scrittura su Confluence) va disattivata a migrazione conclusa.

**Autocompletamento step**
- L'hub indicizza le frasi degli step esistenti dai feature file del repo (ed eventualmente dai decoratori `@given/@when/@then` delle step definition) e le propone nell'editor, segnalando quelle già implementate.

## 8. Integrazione Jira

- Riuso di `JiraClient` e del token già configurato; va aggiunta la creazione di issue (oggi il client è solo in lettura).
- **Task Progettazione**: creato dall'hub su richiesta (pulsante) da DRAFT, oppure collegato se esiste già. È obbligatorio per andare in IN REVIEW.
- **Task Implementazione suite**: creato automaticamente al passaggio a DESIGNED.
- Contenuto: progetto PQ, label `testing`, titolo standard `[FEAT_001_FdR3] Progettazione — <titolo>` / `[…] Implementazione suite — <titolo>`, descrizione con il link alla Feature sull'hub, assegnatario uguale all'owner della fase.
- **Epic/Story prodotto**: solo link, con verifica che la chiave esista.
- Lo stato dei task Jira collegati viene letto e mostrato, ma non guida lo stato della Feature.

## 9. Collegamento con gli NRT

- `qachub.test_executions` oggi ha `scenario_name` e `allure_report` (JSONB), ma non un tag esplicito. **Da verificare** se `allure_report` contiene già i tag behave come label. Se non li contiene, si aggiunge una colonna `scenario_tag` (o `tags` JSONB) alimentata dal reporter nel repo dei test.
- Match: tag identità dello Scenario ↔ tag dell'esecuzione, più gli alias (`tl_scenario_aliases`) per le esecuzioni storiche con tag o nomi precedenti. Il fallback per nome scenario serve solo in migrazione.
- I workflow di esecuzione del repo (`run_behave_tests.yml`, `wisp-tests.yml`, `checkout-e2e-tests.yml`, TAS) selezionano gli scenari con tag di categoria (`@runnable`, `@e2e`, `~@skip`), non con i tag identità. Quindi rinominare i tag identità non rompe le run, a patto di mantenere i tag di categoria (lista controllata, §5). **Da verificare prima delle PR di riallineamento:** quali tag passa la matrice di `nrt-main-dispatch-tests.yml` (`matrix.tags`).
- La pagina Feature mostra, per scenario: esito dell'ultima run per ambiente, pass rate sulle ultime N run, flag flaky (da `flaky_scenarios`), link al report Allure. Dalla run NRT si arriva alla Feature e viceversa.
- La transizione a RUNNING è calcolata quando arriva la prima esecuzione che fa match.

## 10. Ruoli e permessi

Si estende il catalogo azioni esistente (`app/core/permissions.py`, ruoli con permessi JSONB) con azioni dedicate, per esempio `view:test_lifecycle`, `edit:tl_feature`, `review:tl_feature`, `manage:tl_scopes`, `manage:tl_migration`. La mappatura sui ruoli QA Analyst / QA Engineer / admin si configura dalla UI ruoli esistente. Feature flag `test_lifecycle` in `feature_visibility`.

## 11. Esperienza utente (frontend)

Nuova voce di sidebar, gruppo "Test Design" o simile:
- **Panoramica per Ambito**: per ogni Ambito il conteggio delle feature per stato, gli scenari totali, la quota in RUNNING e il pass rate NRT aggregato. Sostituisce il "Riepilogo Feature Files" di Confluence.
- **Lista Feature**: filtri per Ambito, stato, owner, reviewer, anomalie aperte.
- **Dettaglio Feature**:
  - intestazione con codice, stato e pulsanti di transizione (abilitati solo se le condizioni sono soddisfatte, con il motivo quando non lo sono);
  - pannello metadati e link Jira con stato live;
  - schede scenario (id e tag in sola lettura, step nell'editor Gherkin con validazione e autocompletamento, griglia Esempi per gli outline);
  - tab Review/commenti, tab Storico (timeline + diff fra versioni), tab Git (PR, ultimo SHA, anomalie), tab NRT.
- **Studio di contesto**: editor a sezioni fisse.
- **Anomalie**: coda globale delle anomalie da gestire.

## 12. Migrazione

Strumento una tantum, rieseguibile in modalità prova:
1. Import dei feature file da `src/**` (e `docs/use-cases/features/**`) con il parser Gherkin. Dove la prima riga contiene un id Confluence, la pagina FEAT corrispondente viene letta via API Confluence per recuperare i metadati (owner, reviewer, link Jira, stato, stima).
2. Pagine FEAT senza feature file: importate come Feature in DRAFT/IN REVIEW. Gli scenari si estraggono dalle tabelle Given/When/Then dove possibile; altrimenti il contenuto originale va in un campo "note di migrazione" da sistemare a mano.
3. **Numerazione** (*scelta mia*): si mantengono i numeri FEAT esistenti, che su Confluence sono già progressivi per Ambito (esiste un FEAT_001 per Checkout, CUP, FdR3, NodoInviaRPT…). Le collisioni, per esempio i due `FEAT_005_Checkout` (Ordinamento PSP e Back Navigation Control), diventano anomalie: a una delle due si assegna il primo numero libero. Il contatore di ogni Ambito riparte dal massimo importato.
4. Normalizzazione dei tag verso `@<PREFISSO>_<NNN>_<NN>`: l'hub propone il mapping, un umano lo conferma, poi si genera una PR di riallineamento per Ambito. I vecchi tag e nomi vengono salvati come alias.
5. Tutto ciò che non combacia (cartelle duplicate `fdr`/`fdr3`, file senza pagina, pagine senza file, tag duplicati) diventa un'anomalia.
6. A fine migrazione: si disattiva la Action Confluence, le pagine Confluence vengono marcate come sola lettura con un banner/link all'hub, e la guida "Guida alla Creazione della Pagina Testing Suite" viene sostituita da una guida all'hub.

## 13. Fasi suggerite

1. **Fondamenta**: Ambiti, Feature, Scenari, workflow, editor ibrido, storico, review. Si usa senza Git né Jira.
2. **Git**: generazione e PR, import via webhook, diff/anomalie, Action di controllo convenzioni.
3. **Jira**: creazione task e verifica link.
4. **NRT**: tag nelle esecuzioni, vista per scenario, RUNNING automatico.
5. **Migrazione** e dismissione di Confluence.
6. Studio di contesto (può anche anticipare, è indipendente).

## 14. Vincoli

- Stack esistente: FastAPI + SQLAlchemy async + Alembic + Celery (backend), Next.js (frontend), Postgres con schema `qachub` per i dati di test.
- Lingua dei feature file: italiano (`# language: it`), come oggi.
- Il repo dei test resta la fonte eseguibile; l'hub non esegue test.
- Nessuna scrittura su progetti Jira diversi da PQ.
- Il repo dei test è del team QA (CODEOWNERS: `pagopa-team-qa` e membri QA). Le modifiche previste (Action convenzioni, reporter tag, spegnimento `syncFeatureFile.yaml`) restano interne al team. Le PR generate dall'hub richiedono comunque la review di un CODEOWNER, ed è voluto. Prima della fase Git va verificata la branch protection di `main` per l'account del token.

## 15. Lasciato al builder

- Scelta della libreria di parsing Gherkin e dell'editor frontend (es. CodeMirror/Monaco con modalità Gherkin).
- Webhook o polling (o entrambi) per l'import da Git.
- `audit_logs` o tabella eventi dedicata per lo storico.
- Struttura esatta delle tabelle e delle API, coerente con i pattern esistenti (`services/` + `api/v1/` + `schemas/`).
- Formato di archiviazione del contenuto degli Studi di contesto.
