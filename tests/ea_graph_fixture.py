"""A small EA knowledge base, in the shape of ``build_kb``, for the analysis tests."""

from __future__ import annotations

from typing import Any


def _qa(score: int = 0, **kw: Any) -> dict[str, Any]:
    base = {
        "unit": 0,
        "it_repo": 0,
        "it_wf": False,
        "perf_repo": False,
        "sonar": False,
        "central": [],
        "nrt": [],
        "tas": False,
        "tas_ci": False,
        "nodo_bdd": False,
        "under_qa": None,
        "score": score,
    }
    base.update(kw)
    return base


def _comp(cid: str, name: str, parent: str, kind: str = "be", **qa: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "component",
        "name": name,
        "desc": f"{name} desc",
        "parent": parent,
        "kind": kind,
        "kindLabel": "Microservizio",
        "repo": f"https://github.com/pagopa/{cid}",
        "private": False,
        "lang": ["Java"],
        "fw": ["Spring Boot"],
        "ns": "gps",
        "ingress": None,
        "owners": ["@pagopa/team"],
        "apis": [],
        "readme": "",
        "workflows": [],
        "confRepo": [],
        "qa": _qa(**qa),
    }


def kb() -> dict[str, Any]:
    nodes = [
        {"id": "area:gps", "type": "area", "name": "Gestione posizioni", "desc": ""},
        {"id": "area:pay", "type": "area", "name": "Pagamento", "desc": ""},
        {
            "id": "prod:gpd",
            "type": "product",
            "name": "GPD – Posizioni debitorie",
            "desc": "",
            "parent": "area:gps",
            "domain": "gps",
            "conf": [{"title": "[Gestione Posizioni Debitorie]", "url": "https://x/gpd"}],
        },
        {
            "id": "prod:wisp",
            "type": "product",
            "name": "WISP",
            "desc": "",
            "parent": "area:pay",
            "domain": "nodo",
            "conf": [],
        },
        {
            "id": "prod:checkout",
            "type": "product",
            "name": "Checkout",
            "desc": "",
            "parent": "area:pay",
            "domain": "checkout",
            "conf": [],
        },
        _comp(
            "pagopa-debt-position",
            "GPD Core",
            "prod:gpd",
            score=3,
            unit=40,
            it_repo=5,
            central=["integration/gpd"],
            nrt=[],
        ),
        _comp("pagopa-gpd-payments", "GPD Payments", "prod:gpd", score=1, unit=10),
        _comp(
            "pagopa-wisp-converter",
            "WISP Converter",
            "prod:wisp",
            score=4,
            unit=20,
            it_repo=3,
            central=["integration/wisp"],
            nrt=["wisp"],
            perf_repo=True,
        ),
        _comp("pagopa-checkout-fe", "Checkout FE", "prod:checkout", kind="fe", score=0),
        _comp("pagopa-gpd-lib", "GPD Lib", "prod:gpd", kind="lib", score=0),
        {"id": "ds-gpd", "type": "datastore", "name": "Cosmos GPD", "desc": ""},
        {"id": "dl:gpd-ingestion", "type": "dataset", "name": "gpd_ingestion", "desc": ""},
        {"id": "dl:gpd-gold", "type": "dataset", "name": "gpd_gold", "desc": ""},
    ]
    edges = [
        {
            "from": "pagopa-debt-position",
            "to": "pagopa-gpd-payments",
            "type": "event",
            "origin": "curated",
            "status": "active",
        },
        {
            "from": "pagopa-wisp-converter",
            "to": "pagopa-debt-position",
            "type": "api",
            "origin": "helm",
            "status": "active",
        },
        {
            "from": "pagopa-checkout-fe",
            "to": "pagopa-wisp-converter",
            "type": "api",
            "origin": "helm",
            "status": "active",
        },
        {
            "from": "pagopa-gpd-payments",
            "to": "pagopa-debt-position",
            "type": "api",
            "origin": "helm",
            "status": "active",
        },
        {
            "from": "pagopa-debt-position",
            "to": "ds-gpd",
            "type": "data",
            "origin": "helm",
            "status": "active",
        },
        {
            "from": "pagopa-debt-position",
            "to": "dl:gpd-ingestion",
            "type": "produces",
            "origin": "contract",
            "status": "active",
        },
        {
            "from": "dl:gpd-ingestion",
            "to": "dl:gpd-gold",
            "type": "derives",
            "origin": "contract",
            "status": "active",
        },
    ]
    blueprint = {
        "segments": [{"id": "seg-ec", "name": "Enti Creditori", "who": [], "actors": []}],
        "services": [
            {
                "id": "sv-gpd",
                "name": "Posizioni debitorie",
                "seg": ["seg-ec"],
                "proc": ["pr-gpd", "pr-m1"],
            }
        ],
        "functions": [{"id": "fn-pos", "name": "Gestione posizioni"}],
        "processes": [
            {
                "id": "pr-gpd",
                "name": "Creazione e pubblicazione posizioni",
                "fn": "fn-pos",
                "prod": ["gpd"],
                "ref": {
                    "space": "IQCGJ",
                    "title": "[Gestione Posizioni Debitorie]",
                    "url": "https://x/p",
                },
                "suites": [],
            },
            {
                "id": "pr-m1",
                "name": "Pagamento avviato dall'EC",
                "fn": "fn-pos",
                "prod": ["wisp", "checkout"],
                "ref": None,
                "suites": [],
            },
        ],
        "infra": [],
        "prodInfra": {},
        "risk": {},
        "riskSrc": None,
        "panels": {},
    }
    return {"meta": {}, "nodes": nodes, "edges": edges, "blueprint": blueprint}
