# ruff: noqa: E501  (step descriptions are prose shown to people)
"""«Flussi»: how a payment moves across the platform, step by step.

The sequence of the steps is not in the code nor in the map: these flows follow the use
cases of the SANP 3.12.0 (Specifiche Attuative del Nodo dei Pagamenti, developer.pagopa.it)
and are tied to the real nodes of the map. Steps marked ``layer = "piattaforma"`` are
internal details of the platform that the SANP do not show. They are served with ``validated = False`` until the QA/architecture
team confirms them. For every step the API also says whether the map already has an edge
between the two nodes (``on_map``), so a step and the map can be checked against each other.
"""

from __future__ import annotations

from typing import Any

from app.services.ea.graph_index import CALL_EDGES, FLOW_EDGES, GraphIndex

NODO = "pagopa-nodo4-nodo-dei-pagamenti"

SANP = "https://developer.pagopa.it/it/pago-pa/guides/sanp/3.12.0/casi-duso"
GPD = "pagopa-gpd-payments"
TX = "pagopa-ecommerce-transactions-service"


def _s(
    frm: str,
    to: str,
    kind: str,
    primitive: str,
    text: str,
    *,
    optional: bool = False,
    layer: str = "sanp",
) -> dict[str, Any]:
    """One step. kind: "user" (a person acts), "sync" (request and answer), "async" (event),
    "self" (work inside one node). layer: "sanp" when the step is in the SANP use case,
    "piattaforma" when it is an internal detail of the platform that the SANP do not show."""
    return {
        "from": frm,
        "to": to,
        "kind": kind,
        "primitive": primitive,
        "text": text,
        "optional": optional,
        "layer": layer,
    }


FLOWS: list[dict[str, Any]] = [
    {
        "id": "modello-3",
        "name": "Pagamento di un avviso presso PSP",
        "subtitle": "SANP · caso d'uso · l'Ente usa GPD",
        "process": "pr-m3",
        "services": ["sv-avviso", "sv-ricevute"],
        "sanp_url": f"{SANP}/pagamento-di-un-avviso-presso-psp",
        "lanes": [
            "ext-citizen",
            "ext-psp",
            NODO,
            GPD,
            "pagopa-biz-events-datastore",
            "pagopa-receipt-pdf-generator",
            "ext-io",
        ],
        "steps": [
            _s(
                "ext-citizen",
                "ext-psp",
                "user",
                "Avviso di pagamento",
                "Il cittadino presenta l'avviso a un PSP: banca, sportello, ufficio postale, tabaccheria, app.",
            ),
            _s(
                "ext-psp",
                NODO,
                "sync",
                "verifyPaymentNotice",
                "Il PSP verifica l'avviso letto dal QR code o inserito a mano. Non cambia lo stato della posizione. Poste, con il Data Matrix, usa verificaBollettino.",
                optional=True,
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paVerifyPaymentNotice",
                "Il Nodo chiede all'Ente di verificare l'opzione di pagamento indicata dal numero avviso. Per gli Enti su GPD risponde GPD.",
            ),
            _s(
                "ext-psp",
                NODO,
                "sync",
                "verifyPaymentOptions",
                "In alternativa il PSP chiede le opzioni di pagamento disponibili: rate, maggiorazioni, riduzioni.",
                optional=True,
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paVerifyPaymentOptions",
                "Se la stazione dell'Ente ha il flag «new Verify», il Nodo gira la richiesta all'Ente; altrimenti il PSP torna alla verifyPaymentNotice.",
                optional=True,
            ),
            _s(
                "ext-citizen",
                "ext-psp",
                "user",
                "Conferma di voler pagare",
                "Il cittadino conferma l'importo e la volontà di pagare.",
            ),
            _s(
                "ext-psp",
                NODO,
                "sync",
                "activatePaymentNotice",
                "Fase obbligatoria: il PSP chiede al Nodo di attivare il pagamento. Il Nodo genera il token del pagamento.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paGetPayment",
                "L'attivazione arriva all'Ente, che restituisce i dati del pagamento: importi e conti di accredito. Il PSP ha ora tutto per incassare.",
            ),
            _s(
                "ext-citizen",
                "ext-psp",
                "user",
                "Pagamento",
                "Il cittadino paga sul touchpoint del PSP.",
            ),
            _s(
                "ext-psp",
                NODO,
                "sync",
                "sendPaymentOutcome",
                "Obbligatoria e in tempo reale, entro la scadenza del token: il PSP comunica l'esito, OK oppure KO.",
            ),
            _s(
                NODO,
                NODO,
                "self",
                "Generazione della ricevuta",
                "Solo con esito OK: la piattaforma genera la receipt, identificata dal token.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paSendRT",
                "La receipt va a ciascun Ente della lista dei trasferimenti. L'Ente chiude la posizione e la considera saldata.",
            ),
            _s(
                NODO,
                "pagopa-biz-events-datastore",
                "async",
                "Evento di pagamento",
                "Il pagamento diventa un evento di business, la base per ricevute, storico e dati.",
                layer="piattaforma",
            ),
            _s(
                "pagopa-biz-events-datastore",
                "pagopa-receipt-pdf-generator",
                "async",
                "Ricevuta PDF",
                "Dall'evento si genera la ricevuta PDF per il cittadino.",
                layer="piattaforma",
            ),
            _s(
                "pagopa-receipt-pdf-generator",
                "ext-io",
                "async",
                "Ricevuta in IO",
                "La ricevuta arriva al cittadino nell'App IO.",
                layer="piattaforma",
            ),
        ],
    },
    {
        "id": "frontend-ec",
        "name": "Pagamento presso il frontend dell'Ente",
        "subtitle": "SANP · caso d'uso · redirect al Checkout",
        "process": "pr-m1",
        "services": ["sv-checkout"],
        "sanp_url": f"{SANP}/pagamento-presso-frontend-dellec",
        "lanes": [
            "ext-citizen",
            "ext-ec",
            "pagopa-checkout-fe",
            TX,
            "pagopa-afm-calculator",
            NODO,
            GPD,
            "ext-psp",
        ],
        "steps": [
            _s(
                "ext-citizen",
                "ext-ec",
                "user",
                "Richiesta di pagamento",
                "Il cittadino chiede di pagare uno o più avvisi sul sito dell'Ente.",
            ),
            _s(
                "ext-ec",
                "pagopa-checkout-fe",
                "user",
                "Redirect al Checkout",
                "Il frontend dell'Ente rimanda il cittadino al Checkout, il frontend di PagoPA, con gli avvisi da pagare (al massimo 5).",
            ),
            _s(
                "pagopa-checkout-fe",
                TX,
                "sync",
                "Nuova transazione",
                "Il Checkout apre la transazione sulla piattaforma eCommerce.",
                layer="piattaforma",
            ),
            _s(
                TX,
                NODO,
                "sync",
                "activatePaymentNotice",
                "Il Checkout chiede al Nodo di attivare gli n pagamenti presso gli Enti. Le interfacce sono le stesse del pagamento presso PSP.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paGetPayment",
                "Ogni attivazione arriva al proprio Ente con una paGetPayment.",
            ),
            _s(
                TX,
                "pagopa-afm-calculator",
                "sync",
                "Scelta del PSP",
                "La piattaforma filtra i PSP che offrono lo strumento scelto e ne calcola le commissioni.",
                layer="piattaforma",
            ),
            _s(
                "ext-citizen",
                "pagopa-checkout-fe",
                "user",
                "Pagamento",
                "Il cittadino paga sul Checkout con lo strumento di un PSP aderente.",
            ),
            _s(
                TX,
                NODO,
                "sync",
                "closePayment",
                "Chiuse le operazioni di pagamento, la piattaforma invia gli esiti al Nodo.",
                layer="piattaforma",
            ),
            _s(
                "pagopa-checkout-fe",
                "ext-ec",
                "user",
                "Redirect al sito dell'Ente",
                "Il Checkout riporta il cittadino sul frontend dell'Ente.",
            ),
            _s(
                NODO,
                "ext-psp",
                "sync",
                "pspNotifyPayment v2",
                "Il Nodo notifica il pagamento al PSP. Se il PSP risponde KO il processo si interrompe e il pagamento va stornato.",
            ),
            _s(
                "ext-psp",
                NODO,
                "sync",
                "sendPaymentOutcome v2",
                "Accettata la notifica, il PSP invia entro 2 secondi un unico esito per tutti i token attivati.",
            ),
            _s(
                NODO,
                NODO,
                "self",
                "Generazione della ricevuta",
                "Solo se il pagamento è andato a buon fine, la piattaforma genera la receipt.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paSendRT",
                "La receipt va agli n Enti interessati, che chiudono le posizioni.",
            ),
        ],
    },
    {
        "id": "checkout",
        "name": "Pagamento di un avviso su Checkout",
        "subtitle": "SANP · touchpoint PagoPA",
        "process": "pr-m1",
        "services": ["sv-checkout"],
        "sanp_url": f"{SANP}/pagamento-da-touchpoint-pagopa/checkout",
        "lanes": [
            "ext-citizen",
            "pagopa-checkout-fe",
            "pagopa-ecommerce-payment-requests-service",
            TX,
            "pagopa-afm-calculator",
            NODO,
            GPD,
            "ext-psp",
        ],
        "steps": [
            _s(
                "ext-citizen",
                "pagopa-checkout-fe",
                "user",
                "Numero avviso",
                "Il cittadino inserisce sul Checkout il numero avviso e il codice fiscale dell'Ente.",
            ),
            _s(
                "pagopa-checkout-fe",
                "pagopa-ecommerce-payment-requests-service",
                "sync",
                "Verifica",
                "Il Checkout chiede alla piattaforma di verificare l'avviso.",
                layer="piattaforma",
            ),
            _s(
                "pagopa-ecommerce-payment-requests-service",
                NODO,
                "sync",
                "verify",
                "Fase di verifica: la richiesta arriva al Nodo.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paVerifyPaymentNotice",
                "Il Nodo chiede all'Ente di verificare l'opzione di pagamento indicata dal numero avviso.",
            ),
            _s(
                TX,
                NODO,
                "sync",
                "activatePaymentNotice",
                "Fase di attivazione: il Nodo genera il token del pagamento.",
            ),
            _s(NODO, GPD, "sync", "paGetPayment", "La richiesta di attivazione arriva all'Ente."),
            _s(
                TX,
                "pagopa-afm-calculator",
                "sync",
                "Scelta del PSP",
                "Il Checkout filtra i PSP e mostra le commissioni.",
            ),
            _s(
                "ext-citizen",
                "pagopa-checkout-fe",
                "user",
                "Pagamento",
                "Il cittadino paga, senza bisogno di registrarsi.",
            ),
            _s(
                TX,
                NODO,
                "sync",
                "closePayment",
                "La piattaforma comunica al Nodo la conclusione del pagamento.",
                layer="piattaforma",
            ),
            _s(
                NODO,
                "ext-psp",
                "sync",
                "pspNotifyPayment",
                "Il Nodo invia i dettagli al PSP. Se il PSP risponde KO il processo si interrompe e il pagamento va stornato.",
            ),
            _s(
                "ext-psp",
                NODO,
                "sync",
                "sendPaymentOutcome",
                "Il PSP conferma l'esito entro 2 secondi; dopo aver accettato la notifica non può più rispondere KO.",
            ),
            _s(
                NODO,
                NODO,
                "self",
                "Generazione della ricevuta",
                "La piattaforma genera la receipt.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paSendRT",
                "La receipt va a ciascun Ente, che chiude la posizione.",
            ),
        ],
    },
    {
        "id": "app-io",
        "name": "Pagamento di un avviso dall'App IO",
        "subtitle": "SANP · touchpoint PagoPA",
        "process": "pr-io",
        "services": ["sv-io"],
        "sanp_url": f"{SANP}/pagamento-da-touchpoint-pagopa/app-io",
        "lanes": [
            "ext-citizen",
            "ext-io",
            "pagopa-ecommerce-payment-requests-service",
            "pagopa-wallet-service",
            TX,
            "pagopa-afm-calculator",
            NODO,
            GPD,
            "ext-psp",
        ],
        "steps": [
            _s(
                "ext-citizen",
                "ext-io",
                "user",
                "Scansione del QR code",
                "Il cittadino inquadra con IO il QR code dell'avviso.",
            ),
            _s(
                "ext-io",
                "pagopa-ecommerce-payment-requests-service",
                "sync",
                "Verifica",
                "IO chiede alla piattaforma di verificare l'avviso.",
                layer="piattaforma",
            ),
            _s(
                "pagopa-ecommerce-payment-requests-service",
                NODO,
                "sync",
                "verify",
                "Fase di verifica: la richiesta arriva al Nodo.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paVerifyPaymentNotice",
                "Il Nodo chiede all'Ente di verificare l'opzione di pagamento indicata dal numero avviso.",
            ),
            _s(
                "ext-io",
                "pagopa-wallet-service",
                "sync",
                "Compatibilità del portafoglio",
                "IO controlla quali metodi salvati nel portafoglio si possono usare per questo pagamento.",
            ),
            _s(
                TX,
                NODO,
                "sync",
                "activatePaymentNotice",
                "Fase di attivazione: il Nodo genera il token del pagamento.",
            ),
            _s(NODO, GPD, "sync", "paGetPayment", "La richiesta di attivazione arriva all'Ente."),
            _s(
                TX,
                "pagopa-afm-calculator",
                "sync",
                "Scelta del PSP",
                "IO filtra i PSP e propone il più conveniente.",
            ),
            _s(
                "ext-citizen",
                "ext-io",
                "user",
                "Pagamento",
                "Il cittadino paga con un metodo salvato, una carta o un'app di terze parti.",
            ),
            _s(
                TX,
                NODO,
                "sync",
                "closePayment",
                "La piattaforma comunica al Nodo la conclusione del pagamento.",
                layer="piattaforma",
            ),
            _s(
                NODO,
                "ext-psp",
                "sync",
                "pspNotifyPayment",
                "Il Nodo invia i dettagli al PSP. Se il PSP risponde KO il processo si interrompe e il pagamento va stornato.",
            ),
            _s(
                "ext-psp",
                NODO,
                "sync",
                "sendPaymentOutcome",
                "Il PSP conferma l'esito entro 2 secondi.",
            ),
            _s(
                NODO,
                NODO,
                "self",
                "Generazione della ricevuta",
                "La piattaforma genera la receipt.",
            ),
            _s(
                NODO,
                GPD,
                "sync",
                "paSendRT",
                "La receipt va a ciascun Ente, che chiude la posizione. Il cittadino ritrova la ricevuta in IO.",
            ),
        ],
    },
    {
        "id": "fdr",
        "name": "Rendicontazione dei pagamenti",
        "subtitle": "SANP · Flussi di Rendicontazione (FdR)",
        "process": "pr-fdr",
        "services": ["sv-fdr"],
        "sanp_url": "https://developer.pagopa.it/it/pago-pa/guides/sanp/3.12.0/specifiche-attuative-del-nodo-dei-pagamenti-spc/funzionamento-generale/rendicontazione-e-cashflow",
        "lanes": [
            "ext-psp",
            "pagopa-fdr",
            "pagopa-fdr-json-to-xml",
            "pagopa-fdr-nodo-dei-pagamenti",
            "pagopa-fdr-2-event-hub",
            "ext-ec",
            "pagopa-gpd-reporting-analysis",
        ],
        "steps": [
            _s(
                "ext-psp",
                "pagopa-fdr",
                "sync",
                "Crea il flusso",
                "In D+2 il PSP rendiconta i riversamenti fatti in D+1 per i pagamenti del giorno D. Apre un flusso con nome univoco nell'anno, il totale rendicontato, il numero di pagamenti e l'Ente ricevente.",
            ),
            _s(
                "ext-psp",
                "pagopa-fdr",
                "sync",
                "Aggiunge i pagamenti",
                "Il PSP invia i pagamenti a pacchetti di al massimo 1000. Un pacchetto in errore si può reinviare senza conflitti.",
            ),
            _s(
                "ext-psp",
                "pagopa-fdr",
                "sync",
                "Pubblica il flusso",
                "Il PSP pubblica il flusso: da qui l'Ente lo può leggere.",
            ),
            _s(
                "pagopa-fdr",
                "pagopa-fdr-json-to-xml",
                "async",
                "Conversione per il SOAP",
                "Nella transizione dal SOAP al REST il flusso viene tradotto nel formato storico, con le date in UTC.",
                layer="piattaforma",
            ),
            _s(
                "pagopa-fdr-json-to-xml",
                "pagopa-fdr-nodo-dei-pagamenti",
                "sync",
                "FdR1 sul Nodo",
                "Gli Enti che usano ancora le primitive SOAP lo trovano sul Nodo.",
                layer="piattaforma",
            ),
            _s(
                "pagopa-fdr",
                "pagopa-fdr-2-event-hub",
                "async",
                "Evento FdR",
                "Il flusso pubblicato diventa un evento per gli indicatori di qualità e per i dati.",
                layer="piattaforma",
            ),
            _s(
                "ext-ec",
                "pagopa-fdr",
                "sync",
                "Legge i flussi pubblicati",
                "Con le API per gli Enti, l'Ente scarica l'elenco dei flussi pubblicati, il singolo flusso e i suoi pagamenti.",
            ),
            _s(
                "pagopa-gpd-reporting-analysis",
                "pagopa-fdr",
                "sync",
                "Riconciliazione",
                "Per gli Enti su GPD i flussi vengono incrociati con le posizioni pagate.",
                layer="piattaforma",
            ),
        ],
    },
]


def _edge_on_map(g: GraphIndex, a: str, b: str) -> dict[str, Any] | None:
    """The map edge between the two nodes of a step, in either direction, if any."""
    for e in g.out.get(a, []):
        if e["to"] == b and e["type"] in CALL_EDGES | FLOW_EDGES:
            return {"type": e["type"], "origin": e.get("origin"), "reverse": False}
    for e in g.out.get(b, []):
        if e["to"] == a and e["type"] in CALL_EDGES | FLOW_EDGES:
            return {"type": e["type"], "origin": e.get("origin"), "reverse": True}
    return None


def flows_for_map(g: GraphIndex, flows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """The flows with node names, product, and the edge of the map behind every step.

    Lanes or steps naming nodes no longer on the map are dropped and listed in ``missing``,
    so a renamed component never breaks the player."""
    out: list[dict[str, Any]] = []
    for f in FLOWS if flows is None else flows:
        missing = [x for x in f["lanes"] if g.node(x) is None]
        lanes = []
        for x in f["lanes"]:
            n = g.node(x)
            if n is None:
                continue
            prod = g.product_of(x)
            lanes.append(
                {
                    "id": x,
                    "name": n["name"],
                    "type": n["type"],
                    "product": prod["name"] if prod and prod["id"] != x else None,
                }
            )
        ok = {lane["id"] for lane in lanes}
        steps = []
        for i, s in enumerate(f["steps"]):
            if s["from"] not in ok or s["to"] not in ok:
                continue
            edge = None if s["from"] == s["to"] else _edge_on_map(g, s["from"], s["to"])
            # a person acting, or a node working on its own, has no edge to check on the map
            on_map = (
                None if s["kind"] in ("user", "self") or s["from"] == s["to"] else edge is not None
            )
            steps.append({**s, "index": i, "on_map": on_map, "edge": edge})
        out.append(
            {
                "id": f["id"],
                "name": f["name"],
                "subtitle": f["subtitle"],
                "process": f["process"],
                "process_name": g.label(f["process"]),
                "services": [{"id": s, "name": g.label(s)} for s in f["services"]],
                "lanes": lanes,
                "steps": steps,
                "missing": missing,
                "validated": False,
                "sanp_url": f.get("sanp_url"),
                "source": "Casi d'uso SANP 3.12.0; i passi «piattaforma» sono dettagli interni da validare",
            }
        )
    return out
