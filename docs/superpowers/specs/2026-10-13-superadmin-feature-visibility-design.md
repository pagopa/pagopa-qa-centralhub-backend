# Superadmin Feature Visibility — Design

## Obiettivo

Consentire al superadmin di rendere globalmente visibili o non disponibili singole pagine operative del QA Hub, senza modificare i permessi RBAC già configurati per i ruoli.

Una pagina è utilizzabile solo quando è abilitata globalmente e il ruolo dell'utente possiede il permesso RBAC richiesto. La visibilità globale è un filtro aggiuntivo, non una concessione di accesso.

## Ambito

I toggle coprono le pagine operative presenti nella sidebar e il link API Docs:

- Overview
- Gherkin Generator
- E2E
- SANP Health
- KPI Jira
- Metriche
- PSP Commissioni
- Posizioni Debitorie GPD
- Catalogo Controlli
- Controlli per Dominio
- Docs & Decks
- API Docs (Swagger link)
- Settings E2E
- Settings Gherkin

Coverage, Performance, Defect Tracker, Releases e Dashboards sono route placeholder non presenti nella sidebar e restano fuori da questa iterazione. Tra le impostazioni amministrative sono incluse solo Settings E2E e Settings Gherkin; Settings Utenti, Ruoli & Permessi, Risorse T&M e le azioni interne (per esempio sync) non hanno toggle.

## Esperienza superadmin

Aggiungere il tab **Funzionalità** alla pagina **Settings Utenti**, accanto a **Utenti** e **Ruoli & Permessi**. Il tab mostra una riga per ogni voce del catalogo e un interruttore abilitato/disabilitato.

Le modifiche vengono salvate singolarmente e persistono nel database dell'ambiente corrente. Lo stato iniziale di tutte le pagine elencate è abilitato; anche una nuova voce aggiunta al catalogo è abilitata per default. Gli aggiornamenti non vengono trasmessi in tempo reale alle schede già aperte: gli utenti li ricevono al successivo caricamento o aggiornamento della configurazione.

Il tab Funzionalità e le altre pagine amministrative restano disponibili anche quando tutte le pagine operative sono disabilitate.

## Navigazione e route

La sidebar mostra una pagina soltanto quando la feature globale è abilitata e l'utente supera il controllo RBAC esistente. La stessa condizione protegge la route: un URL diretto a una feature disabilitata mostra una schermata informativa neutra, invece di renderizzare la pagina o fare redirect alla Overview.

API Docs viene nascosta dalla sidebar quando disabilitata. L'URL diretto di Swagger/OpenAPI sul backend resta raggiungibile: bloccarlo lato server non fa parte di questa feature.

## Catalogo e persistenza

- Il catalogo delle pagine vive nel codice backend con chiave stabile, label, route, permesso RBAC associato e default `enabled=true`.
- La configurazione persistente conserva gli override globali per l'ambiente; una chiave assente dal catalogo o dagli override risulta abilitata per default.
- L'aggiunta futura di una pagina richiede una voce nel catalogo e il relativo gating di sidebar/route, ma non una migration per la nuova chiave.
- La configurazione globale è separata da `roles.permissions`: i toggle non riscrivono la matrice RBAC.

## Autorizzazione e limite accettato

Come per gli endpoint RBAC esistenti, la protezione superadmin è applicata nel frontend. Gli endpoint backend di lettura e modifica della visibilità restano aperti e non verificano l'identità del chiamante. Questo segue esplicitamente l'architettura attuale e non costituisce una garanzia di sicurezza contro chiamate API dirette.

## Repository e rollout

La feature coinvolge `qa-hub-backend` e `qa-hub-frontend`. Creare in entrambi un branch `feature/superadmin-feature-visibility` basato sul rispettivo `origin/main` e aprire due PR coordinate. La prima migration inizializza la configurazione senza override, lasciando tutte le pagine abilitate e preservando il comportamento corrente.

## Test

Backend:

- Catalogo e default abilitato per le feature note e nuove chiavi senza override.
- Lettura e aggiornamento degli override, con rifiuto di chiavi fuori dal catalogo.
- Stato iniziale senza override: tutte le pagine abilitate.

Frontend:

- Il tab Funzionalità è accessibile solo al superadmin nel portale.
- Il toggle salva e aggiorna lo stato visualizzato; errori e richieste pendenti sono rappresentati.
- Sidebar e route richiedono sia feature abilitata sia permesso RBAC.
- Metriche ha un toggle indipendente associato alla route `/metrics`.
- Una route disattivata non renderizza il contenuto e mostra la schermata informativa.
- Le impostazioni amministrative restano raggiungibili anche con tutte le pagine operative disabilitate.
- API Docs viene nascosta quando il relativo toggle è spento; non si introduce un blocco server-side Swagger.
