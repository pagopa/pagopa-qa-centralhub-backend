from __future__ import annotations

# ruff: noqa: S101
from app.services.ea.graph_index import GraphIndex
from tests.ea_graph_fixture import kb


def test_resolve_by_id_repo_and_name() -> None:
    g = GraphIndex(kb())
    assert g.resolve("pagopa-debt-position") == "pagopa-debt-position"
    assert g.resolve("PAGOPA-DEBT-POSITION") == "pagopa-debt-position"
    assert g.resolve("gpd") == "prod:gpd"
    assert g.resolve("WISP Converter") == "pagopa-wisp-converter"
    assert g.resolve("nope") is None


def test_search_ranks_exact_and_includes_blueprint() -> None:
    g = GraphIndex(kb())
    hits = g.search("gpd")
    assert hits[0]["id"] == "prod:gpd"
    assert {"pagopa-debt-position", "pagopa-gpd-payments"} <= {h["id"] for h in hits}
    assert g.search("posizioni debitorie")[0]["id"] in {"sv-gpd", "prod:gpd"}
    assert g.search("   ") == []


def test_callers_follow_incoming_call_edges_with_paths() -> None:
    g = GraphIndex(kb())
    hops = {h.id: h for h in g.callers("pagopa-debt-position", depth=2)}
    assert set(hops) == {"pagopa-wisp-converter", "pagopa-gpd-payments", "pagopa-checkout-fe"}
    assert hops["pagopa-checkout-fe"].depth == 2
    assert hops["pagopa-checkout-fe"].path == (
        "pagopa-debt-position",
        "pagopa-wisp-converter",
        "pagopa-checkout-fe",
    )
    assert [h.id for h in g.callers("pagopa-debt-position", depth=1)] == [
        "pagopa-gpd-payments",
        "pagopa-wisp-converter",
    ]


def test_dependencies_and_downstream_data() -> None:
    g = GraphIndex(kb())
    assert [h.id for h in g.dependencies("pagopa-debt-position")] == [
        "ds-gpd",
        "pagopa-gpd-payments",
    ]
    assert [h.id for h in g.downstream_data("pagopa-debt-position")] == [
        "dl:gpd-ingestion",
        "dl:gpd-gold",
    ]


def test_summary_of_component_product_and_process() -> None:
    g = GraphIndex(kb())
    c = g.summary("pagopa-debt-position")
    assert c["prodotto"]["id"] == "prod:gpd" and c["area"]["id"] == "area:gps"
    assert {x["id"] for x in c["chiamato_da"]} == {"pagopa-wisp-converter", "pagopa-gpd-payments"}
    assert c["qa"]["suite_centrali"] == ["integration/gpd"]
    p = g.summary("prod:gpd")
    assert {x["id"] for x in p["componenti"]} == {
        "pagopa-debt-position",
        "pagopa-gpd-payments",
        "pagopa-gpd-lib",
    }
    assert p["processi"] == [{"id": "pr-gpd", "nome": "Creazione e pubblicazione posizioni"}]
    pr = g.summary("pr-gpd")
    assert pr["servizi"] == ["Posizioni debitorie"]
    assert pr["pagina_confluence"]["url"] == "https://x/p"
    assert g.summary("missing") is None
