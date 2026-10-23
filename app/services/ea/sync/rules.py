# ruff: noqa: E501  (regex tables ported verbatim from the discovery scripts)
"""Rules used by the EA sync to turn repository and Confluence facts into the graph.

Ported verbatim from the discovery scripts (``catalog.py`` / ``build_kb.py``) that produced
the initial knowledge base, so the weekly sync derives exactly what the import loaded.
Changing a rule here changes what the next sync proposes (see docs/ea-map/DESIGN.md §5).
"""

from __future__ import annotations

# (regex on a Helm envConfig URL, target node id). First match wins.
URL_MAP: list[tuple[str, str]] = [
    ("api\\.tokenizer\\.pdv", "ext-pdv"),
    ("xpay\\.nexigroup", "ext-npg"),
    ("api\\.io\\.pagopa\\.it", "ext-io"),
    ("notifichedigitali", "ext-send"),
    ("bancaditalia", "ext-bdi"),
    ("oneid\\.pagopa", "ext-oneid"),
    ("selfcare\\.pagopa\\.it|api\\.selfcare", "ext-selfcare"),
    ("kusto\\.windows", "ext-kusto"),
    ("atlassian\\.net", "ext-jira"),
    ("/gpd/payments-receipts-service", "pagopa-gpd-payments"),
    ("/gpd/api", "pagopa-debt-position"),
    ("/api-config-cache", "pagopa-api-config-cache"),
    ("/apiconfig-selfcare-integration", "pagopa-api-config-selfcare-integration"),
    ("/apiconfig/auth", "pagopa-api-config"),
    ("/afm/calculator-service", "pagopa-afm-calculator"),
    ("/afm/marketplace-service|/afm/utils", "pagopa-afm-marketplace-be"),
    ("/ecommerce/notifications-service", "pagopa-notifications-service"),
    ("/ecommerce/user-stats-service", "pagopa-ecommerce-user-stats-service"),
    ("/ecommerce/transaction-auth-requests-service", "pagopa-ecommerce-transactions-service"),
    ("/ecommerce/payment-methods-handler", "pagopa-ecommerce-payment-methods-handler"),
    ("/ecommerce/payment-methods-service", "pagopa-ecommerce-payment-methods-service"),
    ("/ecommerce/helpdesk-service", "pagopa-ecommerce-helpdesk-service"),
    ("/ecommerce/npg/notifications", "pagopa-ecommerce-transactions-service"),
    (
        "/ecommerce/checkout/v1/carts|/checkout/carts-auth",
        "pagopa-ecommerce-payment-requests-service",
    ),
    ("/payment-wallet-for-ecommerce", "pagopa-wallet-service"),
    ("pagopa-jwt-issuer-service", "pagopa-jwt-issuer-service"),
    ("/nodo-auth/|/nodo-monitoring/", "pagopa-nodo4-nodo-dei-pagamenti"),
    ("pagopa-node-forwarder", "pagopa-node-forwarder"),
    ("/receipts/", "pagopa-receipt-pdf-service"),
    ("/fdr-nodo/", "pagopa-fdr-nodo-dei-pagamenti"),
    ("/fdr-internal/", "pagopa-fdr"),
    ("/anonymizer/", "pagopa-anonymizer"),
    ("pagopa-gps-mbd-service", "pagopa-gps-mbd-service"),
    ("pagopa-iuvgenerator", "pagopa-iuvgenerator"),
    ("/wisp-converter/", "pagopa-wisp-converter"),
    ("pdf-engine", "pagopa-pdf-engine"),
    ("print-payment-notice-generator", "pagopa-print-payment-notice-generator"),
    ("/stand-in-manager", "pagopa-stand-in-manager"),
    ("/technical-support/stand-in", "pagopa-stand-in-technical-support"),
    ("/technical-support/ndp", "pagopa-node-technical-support-worker"),
    ("/backoffice/helpdesk", "pagopa-selfcare-ms-backoffice-backend"),
    ("/taxonomy/", "pagopa-taxonomy"),
    ("checkout\\.pagopa\\.it", "pagopa-checkout-fe"),
]

# (regex on a Helm envConfig value, datastore node id, datastore name or None if not a node)
STORE_MAP: list[tuple[str, str, str | None]] = [
    ("bizevents-ds-cosmos", "ds-bizevents", "Cosmos Biz Events"),
    ("bizevents-neg-ds-cosmos", "ds-bizevents-neg", "Cosmos Negative Biz Events"),
    ("receipts-ds-cosmos", "ds-receipts", "Cosmos Receipts"),
    ("gps-cosmos", "ds-gps", "Cosmos GPS"),
    ("afm-marketplace-cosmos", "ds-afm", "Cosmos AFM Marketplace"),
    ("nodo-verifyko-cosmos", "ds-verifyko", "Cosmos Verify KO"),
    ("nodo-standin-cosmos", "ds-standin", "Cosmos Stand-In"),
    ("nodo-wispconv-cosmos", "ds-wisp", "Cosmos WISP Converter"),
    ("shared-auth-cosmos", "ds-auth", "Cosmos Authorizer"),
    ("pagopapdataexplorer", "ext-kusto", None),
]

# central suite in pagopa-platform-integration-test -> components it exercises
CENTRAL_SUITES: dict[str, list[str]] = {
    "api/auth-service": ["pagopa-checkout-auth-service"],
    "api/cart": ["pagopa-ecommerce-payment-requests-service"],
    "api/checkout-npg": [
        "pagopa-ecommerce-transactions-service",
        "pagopa-ecommerce-payment-methods-service",
        "pagopa-ecommerce-payment-requests-service",
    ],
    "api/ecommerce-cdc": ["pagopa-ecommerce-cdc-service"],
    "e2e/checkout": [
        "pagopa-checkout-fe",
        "pagopa-checkout-auth-service",
        "pagopa-ecommerce-transactions-service",
    ],
    "integration/cup": ["pagopa-canone-unico"],
    "integration/ebollo": ["pagopa-mbd-service", "pagopa-gps-mbd-service", "pagopa-mbd"],
    "integration/fdr": [
        "pagopa-fdr",
        "pagopa-fdr-nodo-dei-pagamenti",
        "pagopa-fdr-xml-to-json",
        "pagopa-fdr-json-to-xml",
    ],
    "integration/fdr3": ["pagopa-fdr"],
    "integration/gpd": ["pagopa-debt-position", "pagopa-gpd-payments"],
    "integration/wisp": ["pagopa-wisp-converter"],
}

# NRT suite -> central suite it runs
NRT_SUITES: dict[str, str] = {
    "wisp": "integration/wisp",
    "checkout": "e2e/checkout",
    "fdr3": "integration/fdr3",
}

NODO_BDD: frozenset[str] = frozenset(["pagopa-nodo4-nodo-dei-pagamenti"])

# product short id -> [(space key, regex on the title of a level-1/2 page)]
CONF_KW: dict[str, list[tuple[str, str]]] = {
    "nodo": [
        (
            "PAG",
            "^(Specifiche funzionali|Analisi Tecnica|Analisi Funzionale|Componenti piattaforma "
            "pagoPA|Nodo dei Pagamenti 4\\.0|Nodo dei Pagamenti 5\\.0)$",
        ),
        ("IQCGJ", "^\\[Nodo & AUX\\]$|^\\[Nodo5 - RE\\]$|^\\[Nodo5 - API Assistenza\\]$"),
        ("PN5", "^\\[Nodo PagoPA\\]$|^Censimento repository$"),
        ("PPR", "^\\[NPG - NDP\\]$"),
    ],
    "wisp": [
        ("IQCGJ", "^\\[Dismissione WISP\\]$|^\\[Dismissione WISP - API Assistenza\\]$"),
        ("PPR", "^\\[Modello Unico\\]$"),
    ],
    "standin": [
        ("IQCGJ", "^\\[Stand-In Manager\\]$|^\\[Flusso di Pagamento in Stand-In\\]$"),
        ("PAG", "^\\[Stand in\\]$"),
    ],
    "nodosupport": [
        (
            "IQCGJ",
            "^\\[Nuova Connettività\\]$|^\\[Nodo5 - Verifiche KO\\]$|^Disaccoppiatore "
            "\\[Nodo & AUX\\]$",
        ),
        ("PAG", "^Migrazione chiamate su APIM$"),
    ],
    "gpd": [
        (
            "IQCGJ",
            "^\\[Gestione Posizioni Debitorie\\]$|^\\[Pagamenti Pull\\]$|^\\[DataPlan - Mappatura "
            "dati piattaforma pagoPA\\]$|^\\[Lista Avvisi Pagati\\]$",
        )
    ],
    "aca": [("IQCGJ", "^\\[ACA - Archivio Centralizzato Avvisi\\]$")],
    "cup": [("IQCGJ", "^\\[Canone Unico Patrimoniale\\]$"), ("PAG", "^Soluzione Canone Unico$")],
    "spontaneous": [("PAG", "^Tassonomia dei servizi di Incasso$")],
    "ebollo": [("IQCGJ", "^\\[@e\\.Bollo 2\\.0\\]$|^\\[Rendicontazione MBD\\]$")],
    "payopt": [("IQCGJ", "^\\[Opzioni di Pagamento\\]$")],
    "checkout": [
        (
            "I",
            "^RFCs - Autenticazione in Checkout$|^Design Review - Autenticazione in "
            "Checkout$|^PMO - Checkout Redirect$|^Engineering - Redirect$",
        )
    ],
    "ecommerce": [
        (
            "I",
            "^Documentazione$|^Engineering$|^Engineering - NPG$|^I primi mesi in produzione di "
            "eCommerce$|^Testing$|^CI/CD$",
        )
    ],
    "wallet": [("I", "^\\[Wallet pagoPA\\]$|^\\[NW\\] - Vista dinamica delle componenti$")],
    "posgw": [
        (
            "I",
            "^Design Review \\[PFM-1 Integrazione POS Fisici in PagoPA\\]$|^RFC \\[PFM-1 "
            "Integrazione POS Fisici in PagoPA\\]$",
        )
    ],
    "arc": [("PPR", "PFM-9 Portale Interno Area Riservata")],
    "pm": [("PAG", "^\\[Payment Manager\\] - interazioni con il Nodo$"), ("I", "^Archivio$")],
    "fdr": [
        (
            "IQCGJ",
            "^\\[FdR - Flussi di Rendicontazione\\]$|^\\[FdR - Riversamenti\\]$|^\\[FdR - Controlli "
            "sui Flussi\\]$",
        )
    ],
    "afm": [("IQCGJ", "^\\[Gestione evoluta delle commissioni\\]$")],
    "bizevents": [("IQCGJ", "^\\[Evento di pagamento\\]$|^\\[Evento transazione non conclusa\\]$")],
    "receipts": [("IQCGJ", "^\\[Ricevute PDF\\]$")],
    "printit": [("IQCGJ", "^\\[Stampa Avvisi Pagamento\\]$")],
    "pdfengine": [("IQCGJ", "^\\[Stampa Avvisi Pagamento\\]$")],
    "apiconfig": [("IQCGJ", "^\\[API Config - Nodo\\]$")],
    "backoffice": [("I", "^\\[BackOffice\\]$")],
    "authorizer": [("IQCGJ", "^\\[Autorizzatore\\]$")],
    "taxonomy": [("IQCGJ", "^\\[Tassonomia\\]$")],
    "srtp": [("IQCGJ", "^\\[IPG-142 RTP\\] - Request To Pay$")],
    "qi": [
        ("I", "^\\[QI\\] - KPI FdR$"),
        ("IQCGJ", "^\\[Banca d\\'Italia\\] - Sorveglianza$|^\\[Quality Improvement\\]$"),
        ("QDP", "^KPI Results$|^\\[KPI pagoPA\\] Analisi$"),
    ],
    "cruscotto": [("IQCGJ", "^Cruscotto - aka crus8$")],
    "observ": [
        ("I", "^Monitoring$|^PMO - \\[Status Page Improvement\\]$"),
        ("PPR", "^\\[Reperibilità\\]$"),
    ],
    "dataq": [("QDP", "^Data Quality & Strategy$"), ("IQCGJ", "^Anonimizzatore$")],
    "tas": [
        (
            "QDP",
            "^Testing Suites$|^Processo di Quality Assurance$|^QA Engineering$|^NRT Analisi$|^Linee "
            "Guida$",
        ),
        ("PAG", "^Test Automation$"),
    ],
    "qahub": [("QDP", "^Dashboard KPI$|^Struttura organizzativa del Centro di Competenza QA")],
    "mock": [("IQCGJ", "^\\[Mocker\\]$")],
    "infra": [
        ("IQCGJ", "^Infrastructure$"),
        ("PPR", "^Infra - pagoPA platform overview$"),
        ("PAG", "^Nuova architettura pagoPA$"),
    ],
    "templates": [("PAGTECH", "^How To$|^Proof of Concept$")],
    "docs": [("PAG", "^Procedure operative$"), ("PPR", "^Linee Guida e processi$")],
}

# product short id -> [(space key, page id, title)] always linked
EXTRA_CONF: dict[str, list[tuple[str, str, str]]] = {
    "nodo": [("PPR", "822378593", "Componenti e rischio")],
    "ecommerce": [("I", "661357093", "Touchpoint API")],
    "tas": [
        ("QDP", "2747335110", "Modello Operativo QA"),
        ("QDP", "2937028730", "Riepilogo Feature Files"),
    ],
    "infra": [("IQCGJ", "1317404828", "Architettura PagoPA-Core")],
}

# ---- sync-only settings -------------------------------------------------------------

GITHUB_ORG = "pagopa"
CONFLUENCE_SPACES: tuple[str, ...] = ("I", "PAG", "PPR", "PN5", "PAGTECH", "IQCGJ", "QDP")
CONFLUENCE_BASE = "https://pagopa.atlassian.net/wiki/spaces"

# Perimeter of the map (decided on 22/10/2026): only the repositories whose name contains
# "pagopa". A repository of the org becomes a *candidate* component when it is in the
# perimeter, it is not a fork nor archived, it was pushed recently and its CODEOWNERS mention
# at least one team that already owns a component of the map. Components outside the
# perimeter are proposed for archiving.
REPO_PERIMETER = "pagopa"


def in_perimeter(repo: str) -> bool:
    return REPO_PERIMETER in repo.lower()


CANDIDATE_MAX_AGE_DAYS = 365

TAS_REPO = "pagopa-platform-integration-test"
# A workflow that mentions one of these strings calls the Test Automation Service
# (ignored in the TAS repository itself, whose workflows obviously mention it).
TAS_MARKERS: tuple[str, ...] = (
    "pagopa-platform-integration-test",
    "test-automation-service",
)
