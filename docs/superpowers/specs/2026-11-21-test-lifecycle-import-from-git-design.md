# Test Lifecycle — Importa da Git — Design breve

Data: 2026-11-21 · Stato: da approvare · Estende: `2026-11-20-test-lifecycle-design.md` (§7 Git)

## Perché

Capita che un feature file venga scritto direttamente sul repo dei test, su un branch, prima di passare dall'hub (es. `PQ-827-fdr-e2e-test-design` → `src/integration/fdr3/feature/FEAT_011_FdR3_Recupero_FdR_Pubblicati_Organizzazione.feature`). Riscriverlo a mano nell'hub è lavoro inutile e fonte di errori. Si vuole: scheletro della feature nell'hub, poi "Importa da Git" con il link del file e la bozza si riempie da sola; da lì il percorso normale (review, approvazione, PR dell'hub).

## Decisioni (con l'utente)

| # | Decisione | Scelta |
|---|---|---|
| I1 | Chi porta il test su `main` | **La PR dell'hub**. Il file sul branch è solo un punto di partenza. |
| I2 | Percorso del file | L'hub **adotta il percorso** del file importato (resta nella cartella eseguita dalle run). |
| I3 | Il file sul branch di partenza | All'**approvazione** l'hub apre, insieme alla sua PR, una **PR verso il branch di partenza che rimuove quel file**. Mai scritture dirette sul branch. |
| I4 | Promemoria | La feature mostra la provenienza; la PR dell'hub la riporta nella descrizione. |

## Approcci considerati

- **L'hub porta il test su `main`** (scelto) vs **la PR del branch porta il test e l'hub si aggancia** (flusso "Git-first" puro: niente seconda copia, ma il test non passa da review e approvazione dell'hub).
- **PR di pulizia all'approvazione** (scelto) vs **subito all'importazione** (più immediato, ma se la feature resta in bozza o viene abbandonata il test sparisce dal branch).

## Comportamento

**Importazione** (pulsante "Importa da Git" nel tab Scenari; solo revisione di lavoro in bozza, permesso `edit:tl_feature`):
- Link accettato: `https://github.com/<tl_repo>/blob/<branch>/<percorso>.feature` (anche branch con `/`: l'hub prova le divisioni possibili finché il branch esiste). Repo diverso da `tl_repo`, file inesistente, non `.feature` o illeggibile → errore chiaro, nulla cambia.
- Contenuto: Contesto, descrizione e scenari (titolo, tipo, step, Esempi, link di dettaglio) dal file. Il **titolo della feature resta quello della bozza**. Polarità dal tag `@positivo/@negativo`, altrimenti positivo.
- Tag: identità nuove dell'hub (`@<SCOPE>_<NNN>_<NN>` in ordine); i vecchi tag identità dello scenario diventano **alias** (storico NRT); i tag di categoria noti restano (`@runnable`…); i tag a livello di feature non identità vengono copiati sugli scenari; gli altri tag sconosciuti si elencano nella risposta come "non importati".
- Bozza con scenari già presenti → l'interfaccia chiede conferma e **sostituisce** gli scenari (nessuna fusione).
- Si salva la **provenienza** (link, branch, percorso, blob, data) sulla feature; nessun `git_path` finché non c'è la PR (così il sync non tocca la bozza).
- Evento di storico `tl.feature.imported`.

**Approvazione:**
- La PR dell'hub scrive il file nel **percorso importato** (sostituendo quello su `main`, se il file viene da `main`); la descrizione cita il branch di partenza.
- Se il branch di partenza non è `main` e il file c'è ancora: branch `qa-hub/cleanup-<codice>` dal branch di partenza, commit che rimuove il file, PR verso il branch di partenza ("Rimuovi <codice>: ora gestito da QA Hub"). Branch o file spariti → nota "nessuna pulizia necessaria". Errori → salvati e pulsante "Riprova PR di pulizia" nel tab Git.
- Tab Git: link alla PR dell'hub e alla PR di pulizia con il loro stato.

## Vincoli

- Nessuna scrittura diretta su branch esistenti: solo branch nuovi `qa-hub/…` e PR.
- In locale funziona con la modalità Git locale (anche per la PR di pulizia).
- Test senza GitHub reale.

## Lasciato al builder

- Struttura dei campi di provenienza e della PR di pulizia (sulla feature o sulla revisione).
- Cancellazione di un file nei client Git (GitHub `DELETE /contents`, clone locale).
- Layout del dialog di importazione.
