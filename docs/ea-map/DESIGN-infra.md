# EA Map – Vista Infrastruttura – Design tecnico

Tab "Infrastruttura" della EA Map: mappa dell'infrastruttura della piattaforma pagamenti
ricavata dal Terraform, con contenitori annidati, icone per vendor e tipo, frecce esplicite
o dedotte, fascia Monitoring & Alerting e collegamenti con i nodi della mappa.

## 1. Perimetro

Fonti (lista in `app/services/ea/infra/config.py`, estendibile):

| Repository | Contenuto |
|---|---|
| `pagopa/pagopa-infra` | domini applicativi (`src/domains/<dominio>-{app,common,secrets}`), `portal-pagopa` |
| `pagopa/pagopa-infra-core` | AKS (`aks-platform`, `aks-leonardo`), VNet hub-spoke, APIM (`next-core-common`), action group, Grafana, CDN/Front Door degli asset |
| `pagopa/pagopa-aws-infra` | SES, S3 rendicontazione PSP AFM, Lambda mock-EC, SNS allarmi |

Inventario del 22/10/2026: 91 stack, ~2.200 risorse gestite, 193 alert (154 solo prod),
19 action group (9 Opsgenie, 6 Slack via email del canale, 4 email), 9 CDN/Front Door.

## 2. Dove stanno i dati, e perché non in `ea_nodes`

`build_kb` carica tutti i nodi EA e quelli di tipo `infra` compongono il blueprint della mappa
principale (una dozzina di nodi curati). Duemila risorse Terraform come nodi appesantirebbero
mappa, assistente e analisi d'impatto. Le risorse stanno quindi in tabelle dedicate, scritte
**dallo stesso giro di sync** della mappa (stesso run, snapshot per repository, coda di
revisione): il resync completo aggiorna tutto insieme.

```
ea_infra_resources  id = "<repo>:<stack>:<address>"   (address = "module.apim", "azurerm_x.y", ...)
  repo, stack, address, tf_kind (resource|module), tf_type, ltype, vendor, label,
  envs text[]           ambienti in cui la risorsa esiste (count per env_short)
  names jsonb           {env: nome Azure risolto}
  parent_id             contenitore (resource group, VNet, subnet, cluster, namespace)
  attrs jsonb           per tipo: severità/frequenza alert, canali action group, location...
  ea_node_id            nodo EA collegato (componente o nodo infra del blueprint), se approvato
  status                active | removed

ea_infra_links      (from_id, to_id, kind) unico
  kind                  ref | monitors | notifies | routes | runs
  origin                explicit (riferimento Terraform) | inferred (dedotta per nome)
  envs text[], evidence jsonb
```

Ogni risorsa ha anche `blueprint_id`, la categoria del blueprint a cui appartiene (`in-apim`,
`in-cosmos`, `in-aws`...; `BLUEPRINT_CATEGORIES`), per il click-through dai nodi infra della mappa.

Il collegamento risorsa → nodo EA è una **proposta** (`ea_changes`, entità `infra`): una
persona la approva come per i produttori dei dataset; a quel punto `ea_node_id` è valorizzato
e la mappa offre il click-through nei due sensi.

## 3. Parsing

`app/services/ea/infra/parser.py`, per repository:

1. lettura di `*.tf` e `env/*/terraform.tfvars` (python-hcl2), raggruppati per stack (cartella);
2. classificazione `tf_type`/sorgente modulo → `ltype` + `vendor` (regole in `config.py`);
   i blocchi rumorosi (secret, access policy, named value, DNS TXT, tag_config...) sono contati
   ma non diventano risorse;
3. valutazione per ambiente (dev/uat/prod da `env/<loc>-<env>`): `var.*` dai tfvars e dai
   default, `local.*`, interpolazioni e `format()`. Serve a due cose: `count = var.env_short ==
   "p" ? 1 : 0` (risorsa solo in prod) e il nome Azure di risorse e `data`;
4. riferimenti intra-stack (`azurerm_x.y`, `module.z`, `data.t.n`) → link `explicit`;
   l'attributo determina il tipo: `resource_group_name` / `subnet_id` / `virtual_network_name`
   → contenitore, `data_source_id` / `scopes` degli alert → `monitors`, `action` → `notifies`.

Cross-stack (`app/services/ea/infra/resolve.py`): ogni `data` è abbinato alla risorsa gestita
con lo stesso `(ambiente, tipo Azure, nome)`, anche quando la risorsa è creata da un modulo
(`kubernetes_cluster` → `azurerm_kubernetes_cluster`, ...). Prototipo sul repo reale: AKS
123/123, APIM 75/75, subnet 178/178, Key Vault 240/243, Cosmos 42/42, action group 414/426.

Proposte di collegamento: namespace AKS → nodo `ns-<namespace>` del blueprint; API APIM,
Function, App Service e release Helm il cui nome Azure, tolti prefisso d'ambiente
(`pagopa-p-weu-`) e suffisso di tipo (`-api`, `-fn`...), coincide con un componente (i mock
sono esclusi). Sul Terraform reale del 22/10/2026: 20 namespace e 11 API/Function. I canali
degli action group: email, webhook (Opsgenie o Slack dall'URL), Slack anche via email del
canale (riconosciuto dal secret).

## 4. Dati sensibili

Subscription ID, tenant, GUID, IP e CIDR **non sono ingeriti**: i tfvars non vengono salvati,
dagli attributi si tengono solo nomi, tipi e riferimenti, e ogni stringa salvata passa da un
filtro che maschera GUID e indirizzi. `EA_INFRA_INCLUDE_NETWORK_DETAILS=true` aggiunge gli
address prefix di VNet e subnet: oggi sarebbero visibili a chiunque veda la mappa, guest compresi
(il backend non verifica ancora il JWT), quindi il flag resta spento.

## 5. Sync

Nel motore esistente (`engine.py`), come i data contract:

- *collect*: per ogni repository infra, se `pushed_at` è più recente dello snapshot si legge
  l'albero e i file `.tf`/tfvars, si parsa e si salva l'estratto in `ea_repo_snapshots`
  (`infra:<repo>`); altrimenti si riusa lo snapshot. Un errore lascia l'ultima foto e rende
  il run `partial`;
- *apply*: dagli estratti di tutti i repository si ricalcolano risorse e link (fatti del
  Terraform: `auto_applied`, una voce di riepilogo per stack); le risorse sparite diventano
  `removed`; i collegamenti risorsa → nodo EA nuovi vanno in revisione (`pending`), quelli
  rifiutati non vengono riproposti.

## 6. API

- `GET /api/v1/ea/infra?env=prod` → contenitori, risorse, link, monitoring (alert, action
  group, canali, dashboard) dell'ambiente. Stessa gestione permessi degli altri endpoint EA.
- `GET /api/v1/ea/infra/resources/{id}` → dettaglio (attributi, link entranti/uscenti, nodo EA).
- `GET /api/v1/ea/nodes/{id}` include le risorse infra collegate (click-through inverso).

## 7. Frontend

Due modi di guardarla. **Per livelli** (predefinita), dall'alto verso il basso: Frontend web
(Front Door/CDN che servono le SPA, con hostname pubblico e storage dei file statici), Ingresso
(DNS, Application Gateway, APIM), API, Calcolo, Messaggi ed eventi, Dati, Trasversali; i livelli
mostrano solo i componenti architetturali (`INFRA_LAYERS` in `infra.ts`). Un dominio (ricavato
dallo stack) mostra la sua fetta verticale più le risorse condivise che usa; una risorsa
selezionata evidenzia quelle collegate. I Front Door sono proposti come collegamento al
componente frontend dello stesso dominio (`pagopa-checkout-fe`...), da confermare in Revisione.
Il parser ha una versione (`PARSER_VERSION`): uno snapshot di un parser precedente viene riletto.

**Per stack Terraform**:

Tab "Infrastruttura" in `EaMapView`: selettore ambiente (prod default), contenitori annidati
(stack → resource group / VNet → subnet / cluster → namespace) con layout automatico, icone da
`infraIcons.ts` (tipo logico → icona, con icona generica di ripiego; il mapping è
configurazione riusabile), frecce piene per `explicit` e tratteggiate per `inferred`, fascia
Monitoring & Alerting (risorsa → regola → action group → canale), pannello dettaglio con link
al componente nella mappa e al file Terraform su GitHub.

## 8. Fasi

1. Modello, parser, resolver, integrazione nel sync, API (backend, migration 0022).
2. Tab frontend e guida (`EaGuide.tsx`).
3. Stato vivo degli alert e override manuale delle posizioni.
