# EA Map – Evoluzione Data Lake – Design tecnico

> Branch: `feature/ea-map-data-lake` (backend e frontend) · Stato: **approvato il 20/10/2026; PR1, PR2 e PR3 implementate** · Autore: Claude con Cristiano Sticca
>
> Obiettivo: portare nella EA Map il Data Lake aziendale, partendo dai data contract della piattaforma pagoPA (`pagopa/pdnd-data-contracts`, cartella `src/data/pagopa`), e metterli in relazione con le entità già presenti (componenti, prodotti, sistemi esterni) e con il Data Quality Framework del Hub.

---

## 1. Perimetro

| # | Capacità | Fase |
|---|---|---|
| 1 | Import dei data contract come nodi `dataset`, lineage interna (silver → gold) e consumer | PR1 |
| 2 | Nuova vista **Data Lake** nel menu della EA Map e scheda del dataset | PR1 |
| 3 | Collegamento produttore → dataset proposto dalla sync e approvato in coda di revisione; sezione "Dati nel Data Lake" nella scheda del componente; sync settimanale del repo dei contratti | PR2 |
| 4 | Overlay Data Quality: copertura dei check per dataset (contratto e DQF del Hub) | PR3 |

**Fuori perimetro per ora**

- Altri domini del repo (`data-lake/bi`, `cross`, `send`, `io`…): il modello li regge, ma si attivano in seguito.
- Esiti reali dei check DQ (tabelle `pagopa_qa.silver_dqf_*` su Impala): si valuta dopo quale strada usare (lettura diretta Impala, push dalla pipeline DQF verso il Hub, API SODA Cloud). Vedi §9.
- Un nodo per ogni topic Event Hub: il topic resta un attributo del dataset (decisione §10.1).

## 2. Cosa c'è nei data contract

Formato [datacontract.com](https://datacontract.com/) `0.9.3` più l'estensione pagoPA (`docs/pagopa-spec.md`: formato CSV, growth, updates, deletes, tag `nullValue`, `dlInputPartitionKey`, `dateFormat`). Ogni file ha:

| Sezione | Cosa usiamo |
|---|---|
| `id` | `urn:pagopa:<dominio>:<entità>`: identificativo stabile del dataset |
| `info` | titolo, descrizione, versione, owner, contatto (nome, email, pagina Confluence, canale Slack) |
| `servers` | per ambiente: `kafka` (namespace Event Hub + topic), `impala` (host + `database`/`table`), `s3` (location + formato) |
| `terms` | uso, limitazioni, preavviso; spesso contiene la lineage a parole ("derivata da `silver_positive_denorm_increment`") |
| `models` | campi con tipo, obbligatorietà, enum, flag `pii` |
| `servicelevels` | disponibilità, retention, frequenza |
| `quality` | `type: SodaCL`, `dataset`, `xref-dataset` e i check, con la stessa convenzione di nomi del DQF (`fld__cmp__id__not_null`) |
| `links` | documentazione aggiuntiva |

Inventario attuale di `src/data/pagopa` (39 file):

| Dominio | Ingestion (kafka) | Silver / Gold (impala) | Export |
|---|---|---|---|
| `biz` | `biz:positive`, `biz:negative`, `biz:awakable` | `positive-denorm-increment` (silver), `positive-denorm`, `gold:payments`, `gold:transfer`, `gold:transactions`, `gold:invoicing-payments`, `gold:payments-negative/unfinished/awakable`, `gold:transfer-negative/unfinished/awakable` | `mef:tari-tefa` (CSV su S3) |
| `gpd` | `payment-position`, `payment-option`, `transfer` (CDC) | – | – |
| `fdr` | `fdr:payments` | `fdr:overwrites` (silver) | – |
| `gec` | `bundles`, `cibundles`, `paymenttypes`, `touchpoints` | – | – |
| `wallet` | `ecommerce:wallet` | – | – |
| `observability` | `psp-kpi`, `bdi` (KPI Banca d'Italia) | `psp-kpi-gold`, `bdi:ec`, `bdi:psp` | – |
| `payments` | – | `all-transaction`, `enabled-services` | – |
| `forecast` | – | `average-amount-forecast`, `positive-transactions-forecast` | – |
| `qa/gec` | – | risultati e KPI del DQF per GEC | – |

Tabelle citate ma senza contratto proprio: `silver_negative`, `silver_awakable`, `silver_bundles`, `silver_paymenttypes`, `silver_touchpoints` (e simili): diventano nodi "non contrattualizzati".

## 3. Modello dei dati

Si riusano `ea_nodes`, `ea_edges`, `ea_changes`, `ea_repo_snapshots`: **nessuna tabella nuova**. La migrazione `0021_ea_data_lake` estende solo i vincoli.

### 3.1 Nodo `dataset`

`ea_nodes.kind` ammette il nuovo valore `dataset`.

| Campo | Valore |
|---|---|
| `id` | `dc:` + URN senza prefisso, es. `dc:pagopa:gpd:payment-position`. Per le tabelle senza contratto: `dl:<database>.<tabella>`, es. `dl:pagopa.silver_negative` |
| `name` | `info.title` (o nome tabella) |
| `description` | `info.description` |
| `parent_id` | null (i dataset non entrano nella gerarchia area → prodotto → componente) |
| `source` | `sync` (dal contratto) |
| `attrs` | vedi sotto |

`attrs`:

```jsonc
{
  "layer": "ingestion | silver | gold | export",      // derivato, vedi §3.3
  "domain": "biz | gpd | fdr | gec | wallet | observability | payments | forecast",
  "contracted": true,                                   // false per le tabelle solo citate
  "contract": {
    "path": "src/data/pagopa/gpd/payment-position.yaml",
    "url": "https://github.com/pagopa/pdnd-data-contracts/blob/main/…",
    "version": "0.0.2", "spec": "0.9.3",
    "owner": "pagoPA QA-Team",
    "contact": { "name": "…", "email": "…", "url": "…(Confluence)", "slack": "…" }
  },
  "servers": {                                          // per ambiente
    "production": { "type": "kafka", "host": "sb://…-observ-gpd-evh…", "topic": "gpd-ingestion.apd.payment_position", "format": "json" },
    "uat": { … }
  },
  "table": "pagopa.gold_payments",                      // se impala
  "fields": 26, "pii_fields": 0,
  "sla": { "availability": "99.99%", "retention": "…", "frequency": "…" },
  "quality": { "type": "SodaCL", "checks": 61, "dataset": "pagopa.silver_bundles", "xref": ["silver_paymenttypes"] },
  "derived_from_text": "derivata da silver_positive_denorm_increment"
}
```

Lo schema completo (campi e tipi) **non** si copia nel DB: la scheda mostra conteggi e link al contratto. Se servirà la ricerca per campo, si aggiunge in seguito.

### 3.2 Nuovi legami

| Tipo | Da → A | Origine | Come nasce | Stato iniziale |
|---|---|---|---|---|
| `produces` | componente (o prodotto) → dataset di ingestion | `contract_rule` | Proposto dalla sync confrontando topic/namespace con i componenti (§5.2) | `proposed`, in coda di revisione |
| `derives` | dataset → dataset (silver → gold, ingestion → silver) | `contract` | Dal contratto: `quality.dataset`/`xref-dataset` e testo "derivata da …" in `info`/`terms` | `active` (applicato subito, rifiutabile) |
| `feeds` | dataset → consumer (`external`) | `contract` o `curated` | MEF e Banca d'Italia dal contratto; **BI** curato su tutte le gold | `active` |

`EDGE_TYPES` aggiunge `produces`, `derives`, `feeds` (nuovo gruppo `DATA_EDGE_TYPES`); `EDGE_ORIGINS` aggiunge `contract` e `contract_rule`. Le precedenze del §3.2 del design principale valgono invariate: un legame manuale o verificato non viene toccato dalla sync, uno rifiutato non viene riproposto.

### 3.2.1 Regole della lineage (verificate sui 39 contratti attuali)

- Si leggono descrizione e `terms`, divisi in frasi (anche sugli elenchi " - " dei blocchi YAML ripiegati).
- Una frase conta se dice da dove arriva il dato ("derivata da", "provengono da", "con join su", "denormalizzazione della tabella", "elaborati dalla tabella"…) e **non** se è un consiglio d'uso ("consultare", "utilizzare la tabella", "effettuare una join con", "relazioni con altre tabelle", "foreign key").
- Nomi di tabella: token `bronze_*`, `silver_*`, `gold_*` (con o senza `pagopa.`); i nomi dei modelli di ingestion valgono solo se distintivi (con `_`, es. `biz_positive`), non parole comuni come `transfer` o `wallet`.
- Nei contratti di ingestion le tabelle `bronze_*` citate sono la loro stessa tabella di atterraggio, non una sorgente.
- Un contratto di ingestion con `quality.dataset` su una tabella silver alimenta quella tabella.

Esito sui contratti attuali: 49 dataset (35 con contratto, 14 tabelle silver solo citate), 51 legami (16 lineage dal testo, 13 da `quality.dataset`, 22 verso consumer).

### 3.3 Classificazione del livello

In ordine:

1. server `kafka` → `ingestion` (bronze nel Data Lake);
2. server `s3` con formato `csv` e consumer esterno → `export`;
3. tabella `impala` con prefisso `gold_` → `gold`; `silver_` → `silver`;
4. altrimenti dal titolo ("Bronze", "Silver", "Gold").

Il livello calcolato si corregge a mano dalla scheda (attributo `layer_override`, che la sync non tocca mai). Allo stesso modo si correggono il link alla documentazione (`doc_url_override`, al posto di `info.contact.url`) e il canale Slack (`slack_url_override`, al posto di `info.contact.slack-channel`): nei contratti questi campi puntano spesso alla home di uno spazio Confluence o a un canale generico. Le correzioni non segnano il dataset come verificato e si annullano con "Torna al contratto".

### 3.4 Consumer

Nuovi nodi `external`: `ext-bi` (Business Intelligence) e `ext-mef` (MEF). `ext-bdi` (Banca d'Italia) esiste già. I legami `feeds` verso BI sono curati: tutte le gold li ricevono all'import, e si possono rimuovere o aggiungere dalla scheda.

### 3.5 Metadati di qualità (`qa/gec`)

I contratti in `src/data/pagopa/qa/**` (risultati e KPI del DQF) **non** diventano dataset di business: la sync li legge e li salva come metadato in `ea_meta` (`dq_result_tables`), con dominio, tabella e campi. Servono per la fase "esiti reali" (§9) e sono collegati al componente `pagopa-platform-data-quality-framework`.

## 4. API

Nessun endpoint nuovo in PR1: la vista legge da `GET /ea/kb`.

- `build_kb` include i nodi `dataset` (`_GRAPH_KINDS` + `dataset`) con un payload compatto (`layer`, `domain`, `contracted`, `table`, `topic`, `owner`, `fields`, `pii_fields`, `quality.checks`, `url`) e i legami `produces`/`derives`/`feeds` tra i tecnici.
- `GET /ea/nodes/{id}` restituisce per i dataset gli `attrs` completi (contratto, server, SLA) e, da PR3, la copertura DQ.
- `GET /ea/nodes/{id}/impact` attraversa anche i nuovi legami: cambiando un componente si vedono dataset e consumer a valle.
- Scrittura: gli endpoint esistenti `POST/PATCH/DELETE /edges` e `/verify` funzionano anche per i nuovi tipi (es. aggiungere a mano un `produces` o un `feeds`).

Viewer e Architettura esistenti ignorano i nodi `dataset` (filtrano per tipo), quindi non cambiano.

## 5. Sync

### 5.1 Lettura dei contratti

Nuovo passo nel motore della sync (`app/services/ea/sync/contracts.py`), dopo GitHub e prima di Confluence:

| Passo | Cosa fa |
|---|---|
| 1 | Legge `head_sha` di `pagopa/pdnd-data-contracts` (configurabile: `ea_contracts_repo`, `ea_contracts_path = src/data/pagopa`). Se è uguale allo snapshot in `ea_repo_snapshots`, salta |
| 2 | Albero del repo e contenuto dei soli `*.yaml` sotto il percorso (una chiamata albero + una GraphQL per i file), con il token GitHub già configurato |
| 3 | Parsing YAML (`pyyaml`, già tra le dipendenze) in funzioni pure: dataset, livello, server, conteggi, lineage, consumer |
| 4 | Diff con i nodi `dataset` e i legami `derives`/`feeds`/`produces` esistenti |

Regole di applicazione:

- **Dataset nuovo o aggiornato** (versione, owner, server, campi, check) → applicato subito (`auto_applied`): è un fatto osservato del contratto. Nel diff si vede cosa è cambiato, es. "v0.0.1 → v0.0.2, campi 24 → 26, topic cambiato".
- **Contratto rimosso** → modifica strutturale in coda (`pending`): decide una persona se archiviare il dataset.
- **`derives`/`feeds` dal contratto** → applicati subito; se il contratto non li dichiara più e sono manuali o verificati → `conflict`.
- **`produces`** → sempre in coda (§5.2).

Esiti: se il repo dei contratti non è leggibile la run è `partial` e i dataset restano invariati.

### 5.2 Proposta produttore → dataset

Per ogni dataset di ingestion, la sync calcola un punteggio tra il topic (e il namespace Event Hub) e i componenti EA (id repo, nome, namespace Helm) e propone il migliore sopra soglia. Regole esplicite in `rules.py` per i casi noti, così non dipende solo dalla somiglianza:

| Topic (contratto) | Proposta | Confidenza |
|---|---|---|
| `gpd-ingestion.apd.*` | `pagopa-gpd-ingestion-manager` | 90 |
| `nodo-dei-pagamenti-fdr` | `pagopa-fdr-2-event-hub` | 85 |
| `nodo-dei-pagamenti-biz-evt-enrich` | `pagopa-biz-events-service` | 70 |
| `nodo-dei-pagamenti-negative-*-biz-evt` | `pagopa-negative-biz-events-datastore` | 60 |
| `gec-ingestion-*` | `pagopa-afm-marketplace-be` | 60 |
| `payment-wallet-ingestion-dl` | `pagopa-payment-wallet-cdc-service` | 60 |
| `bdi-kpi-ingestion-dl` | `pagopa-observability-bdi` | 60 |
| `quality-improvement-psp-kpi` | prodotto `prod:qi` | 50 |

Le proposte finiscono nella tab **Revisione** come "Nuovo legame: X produce Y (topic …)". Approvate una volta, restano; rifiutate, non tornano.

### 5.3 Import iniziale

Non serve un seed: la prima sync dopo il deploy crea i dataset. Per lo sviluppo e i test, una copia ridotta di alcuni contratti in `tests/fixtures/data_contracts/`.

## 6. Overlay Data Quality (PR3)

Per ogni dataset, calcolato in lettura (nessuna tabella nuova):

- **Check nel contratto**: numero di check SodaCL e divisione per dimensione ricavata dal prefisso del nome (`fld__cmp` completezza, `fld__val` validità…).
- **Controlli nel DQF del Hub**: istanze `dq_control_instances` del dominio corrispondente (BIZ, GPD, FDR, GEC, Wallet) la cui `table_ref` coincide con la tabella del dataset; totale, attivi e per dimensione.
- **Allineamento**: check presenti nel contratto ma non censiti nel DQF (e viceversa), confrontando i nomi.

**Come si collegano i controlli alle tabelle** (verificato sui CSV del DQF): i controlli del DQF puntano alle tabelle **bronze** di atterraggio (`pagopa.bronze_gpd_payment_position`), mentre il contratto di ingestion nomina la tabella silver che alimenta (`quality.dataset: pagopa.silver_gpd_payment_position`). Il collegamento passa quindi dal nome base della tabella senza prefisso di livello; le tabelle silver e gold si collegano per nome esatto; un controllo cross-entità su più tabelle conta per ciascun dataset. Sui CSV attuali tutti i controlli con una tabella si collegano al dataset giusto (GPD 78, GEC 73, FdR 18, Wallet 17). Implementazione in `app/services/ea/dq_overlay.py`, calcolata in lettura (nessuna tabella nuova).

Corrispondenza domini contratto → domini DQ: `biz → BIZ`, `gpd → GPD`, `fdr → FDR`, `gec → GEC`, `wallet → Wallet`. Configurabile se i nomi dei domini DQ cambiano.

## 7. Frontend

Nuova tab **Data Lake** tra Architettura e Matrice QA (`view = "datalake"`, deep link `#datalake` e `#dataset/<id>`).

```
src/components/ea-map/
  EaDataLake.tsx        colonne Sorgenti | Ingestion | Silver | Gold | Consumer, filtri, evidenziazione lineage
  EaDatasetDrawer.tsx   scheda del dataset
src/lib/ea-map/
  datalake.ts           calcoli puri: colonne, ordinamento, catena a monte/valle, filtri (con test)
```

**Vista.** Cinque colonne da sinistra a destra; le frecce sono i legami `produces`, `derives`, `feeds`. SVG disegnato a mano come le altre viste, nessuna libreria nuova.

- Clic su un box → evidenzia la catena a monte e a valle (stessa interazione del Blueprint); secondo clic deseleziona.
- Clic su una sorgente → apre la scheda del componente in Architettura.
- Badge: livello, numero di check DQ, icona dati personali (`pii_fields > 0`), "non contrattualizzato" (⚠).
- Filtri: dominio; "solo non contrattualizzati"; "senza produttore"; da PR3 "senza controlli DQ".

**Scheda del dataset.** Descrizione, livello e dominio; server per ambiente (topic o tabella); owner e contatti (Confluence, Slack); versione; campi e campi PII; SLA; "derivata da" e "alimenta"; produttore con stato del legame (proposto/verificato); copertura DQ (PR3); link al contratto su GitHub. Azioni (con `edit:ea_map`): aggiungi/rimuovi `produces` e `feeds`, correggi il livello.

**Scheda del componente** (Architettura). Nuova sezione "Dati nel Data Lake" con i dataset che il componente produce e il loro stato.

**Guida.** Nuova sezione: cos'è la vista, da dove vengono i dati, come leggere badge e colori, come approvare le proposte.

## 8. Test

- **Backend**: parsing dei contratti (fixture reali ridotte: un kafka CDC, una gold impala con lineage nel testo, un export S3, uno senza `quality`); classificazione del livello; estrazione della lineage; regole topic → componente; diff e precedenze (manuale/verificato non toccati, rifiutati non riproposti, contratto rimosso in coda); `build_kb` con dataset; impatto attraverso i nuovi legami; migrazione.
- **Frontend**: `datalake.ts` (colonne, catena, filtri), rendering della vista e della scheda.
- **Verifica su dati reali**: esecuzione della sync in `--dry-run` sui 39 contratti attuali con resoconto di dataset creati, legami e proposte.

## 9. Esiti DQ reali (fase successiva)

Gli esiti sono nelle tabelle Iceberg `pagopa_qa.silver_dqf_<dominio>_results` e `…_kpis_daily_table` (per giorno e dataset: `pass_rate_table`, `quality_score_table`, check passati/warning/falliti); oggi esistono per GEC. Strade possibili, da decidere:

1. **Lettura diretta** da Impala (`impala-proxy…cloudera.site:443`): serve un'utenza di servizio sul Data Lake e la raggiungibilità di rete dal Hub.
2. **Push dalla pipeline DQF** verso un endpoint del Hub con il riepilogo giornaliero per dataset: niente credenziali Data Lake nel Hub, ma va toccata la pipeline.
3. **API SODA Cloud**, se i check vi sono pubblicati.

## 10. Decisioni (20/10/2026)

| # | Tema | Decisione |
|---|---|---|
| 1 | Topic Event Hub | Attributo del dataset; il legame produttore → dataset è proposto dalla sync e approvato in coda |
| 2 | Silver senza contratto | Nodi `dataset` con `contracted = false`, segnalati nella vista |
| 3 | Ambito | Solo `src/data/pagopa`; `qa/**` come metadato di qualità |
| 4 | Consumer | Nodo generico **BI** sulle gold; MEF e Banca d'Italia dai contratti |
| 5 | Esiti DQ | Per ora solo copertura; esiti reali in una fase successiva (§9) |
| 6 | Accesso al repo dei contratti | Token GitHub attuale del Hub; verifica alla prima sync |
| 7 | Vista | Nuova tab **Data Lake** nella EA Map, più il legame nella scheda del dataset e del componente |

## 11. Piano di consegna

| PR | Backend | Frontend |
|---|---|---|
| **PR1 – Dataset e vista** | Migrazione `0021_ea_data_lake`; parser dei contratti (funzioni pure); import da sync per dataset, `derives`, `feeds`; nodi `ext-bi`, `ext-mef`; `build_kb` e impatto estesi | Tab Data Lake, scheda dataset, filtri, Guida |
| **PR2 – Collegamento con l'EA** | Regole e punteggio topic → componente, proposte `produces` in coda, snapshot del repo dei contratti nella sync settimanale | Sezione "Dati nel Data Lake" nella scheda componente, proposte in Revisione, azioni sui legami dal dataset |
| **PR3 – Overlay DQ** | Copertura per dataset (contratto + DQF del Hub), allineamento dei nomi dei check | Badge e sezione DQ nella scheda, filtro "senza controlli DQ" |

Ogni PR passa da te prima di commit e push.
