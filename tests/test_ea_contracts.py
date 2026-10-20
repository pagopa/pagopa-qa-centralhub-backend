from __future__ import annotations

# ruff: noqa: S101
from pathlib import Path

import pytest

from app.services.ea.contracts import (
    EXT_BDI,
    EXT_BI,
    EXT_MEF,
    ContractError,
    build_graph,
    normalize_table,
    parse_contract,
)

FIXTURES = Path(__file__).parent / "fixtures" / "data_contracts"


def contract_files() -> dict[str, str]:
    return {str(p.relative_to(FIXTURES)): p.read_text() for p in FIXTURES.rglob("*.yaml")}


def _edges(files: dict[str, str] | None = None) -> set[tuple[str, str, str]]:
    g = build_graph(files or contract_files())
    return {(e["from"], e["to"], e["type"]) for e in g.edges}


def test_parse_a_cdc_ingestion_contract() -> None:
    path = "src/data/pagopa/gpd/payment-position.yaml"
    c = parse_contract(path, contract_files()[path])
    assert c.node_id == "dc:pagopa:gpd:payment-position"
    assert c.layer == "ingestion" and c.domain == "gpd"
    assert c.topic == "gpd-ingestion.apd.payment_position" and c.table is None
    assert set(c.servers) == {"production", "uat"}
    # leaf fields inside the CDC before/after records, one of them personal data
    assert (c.fields, c.pii_fields) == (7, 1)
    assert c.contact["email"] == "owner@example.test" and c.contact["slack"].startswith("https://")
    assert c.sla == {"availability": "99.99%"}


def test_layers_are_classified_from_server_and_table() -> None:
    nodes = build_graph(contract_files()).nodes
    layers = {k: v["attrs"]["layer"] for k, v in nodes.items()}
    assert layers["dc:pagopa:biz:positive"] == "ingestion"
    assert layers["dc:pagopa:biz:positive-denorm-increment"] == "silver"
    assert layers["dc:pagopa:gold:payments"] == "gold"
    assert layers["dc:pagopa:mef:tari-tefa"] == "export"


def test_quality_checks_are_counted_from_the_sodacl_names() -> None:
    attrs = build_graph(contract_files()).nodes["dc:pagopa:gec:bundles"]["attrs"]
    assert attrs["quality"]["checks"] == 3
    assert attrs["quality"]["dataset"] == "pagopa.silver_bundles"
    assert attrs["quality"]["xref"] == ["pagopa.silver_paymenttypes"]
    assert attrs["check_names"][1] == "fld__cmp__id__not_null"


def test_lineage_comes_from_the_text_and_from_the_quality_dataset() -> None:
    edges = _edges()
    assert ("dc:pagopa:biz:positive", "dc:pagopa:biz:positive-denorm-increment", "derives") in edges
    assert (
        "dc:pagopa:biz:positive-denorm-increment",
        "dc:pagopa:gold:payments",
        "derives",
    ) in edges
    # "con join su biz_positive": the bronze model name of an ingestion contract
    assert ("dc:pagopa:biz:positive", "dc:pagopa:mef:tari-tefa", "derives") in edges
    # "Relazioni con altre tabelle: … gold_transfer" is not lineage
    assert not any(src.endswith("gold_transfer") for src, _dst, _t in edges)
    # an ingestion contract checked on a silver table feeds it
    assert ("dc:pagopa:gec:bundles", "dl:pagopa.silver_bundles", "derives") in edges


def test_cited_tables_without_contract_become_uncontracted_datasets() -> None:
    nodes = build_graph(contract_files()).nodes
    silver = nodes["dl:pagopa.silver_negative"]["attrs"]
    assert silver["contracted"] is False and silver["layer"] == "silver"
    assert silver["cited_by"] == ["dc:pagopa:gold:payments-negative"]
    assert ("dl:pagopa.silver_negative", "dc:pagopa:gold:payments-negative", "derives") in _edges()


def test_consumers() -> None:
    edges = _edges()
    assert ("dc:pagopa:mef:tari-tefa", EXT_MEF, "feeds") in edges
    assert ("dc:pagopa:bdi:ec", EXT_BDI, "feeds") in edges
    gold_to_bi = {src for src, dst, t in edges if dst == EXT_BI and t == "feeds"}
    assert gold_to_bi == {
        "dc:pagopa:gold:payments",
        "dc:pagopa:gold:payments-negative",
        "dc:pagopa:bdi:ec",
    }


def test_quality_metadata_and_invalid_files_are_kept_apart() -> None:
    g = build_graph(contract_files())
    assert not any(k.startswith("dc:pagopa:dqf") for k in g.nodes)
    assert g.quality_tables[0]["table"] == "pagopa_qa.silver_dqf_gec_results"
    assert g.quality_tables[0]["domain"] == "gec"
    assert list(g.errors) == ["src/data/pagopa/broken.yaml"]


def test_duplicated_ids_and_bad_yaml() -> None:
    files = contract_files()
    files["src/data/pagopa/gpd/copy.yaml"] = files["src/data/pagopa/gpd/payment-position.yaml"]
    g = build_graph(files)
    assert "già usato" in g.errors["src/data/pagopa/gpd/payment-position.yaml"]
    with pytest.raises(ContractError):
        parse_contract("x.yaml", "id: [unclosed")


def test_normalize_table() -> None:
    assert normalize_table("silver_x") == "pagopa.silver_x"
    assert normalize_table("silver_x", "pagopa_qa") == "pagopa_qa.silver_x"
    assert normalize_table("PagoPA.Gold_Y") == "pagopa.gold_y"
    assert normalize_table("") is None


def _gold(slug: str, text: str) -> tuple[str, str]:
    path = f"src/data/pagopa/biz/{slug}.yaml"
    return path, (
        "dataContractSpecification: 0.9.3\n"
        f"id: urn:pagopa:gold:{slug}\n"
        f"info:\n  title: {slug}\n  description: x\n"
        f"servers:\n  production:\n    type: impala\n    table: pagopa.gold_{slug}\n"
        f"terms:\n  usage: >\n    {text}\n"
    )


def test_real_phrasings_that_are_not_lineage() -> None:
    files = contract_files()
    files["src/data/pagopa/gpd/transfer.yaml"] = (
        files["src/data/pagopa/gpd/payment-position.yaml"]
        .replace("gpd:payment-position", "gpd:transfer")
        .replace("payment-position:", "transfer:")
    )
    for path, text in (
        _gold(
            "t1",
            "- Per l'universo completo dei pagamenti consultare gold_payments - Derivata dalla "
            "tabella silver_positive_denorm_increment con la lista transfer esplosa",
        ),
        _gold("t2", "Per identificare i record effettuare una join con la tabella gold_payments"),
        _gold(
            "t3",
            "È la denormalizzazione incrementale della tabella pagopa.silver_positive",
        ),
    ):
        files[path] = text
    edges = _edges(files)

    def into(slug: str) -> set[str]:
        target = f"dc:pagopa:gold:{slug}"
        return {src for src, dst, kind in edges if dst == target and kind == "derives"}

    # not gold_payments (advice), not the GPD "transfer" model (an ordinary word)
    assert into("t1") == {"dc:pagopa:biz:positive-denorm-increment"}
    assert into("t2") == set()
    assert into("t3") == {"dl:pagopa.silver_positive"}


def test_bronze_tables_named_by_an_ingestion_contract_are_its_own() -> None:
    files = contract_files()
    path = "src/data/pagopa/biz/dc-biz-positive.yaml"
    files[path] = files[path].replace(
        "models:\n  biz_positive",
        "terms:\n  usage: >\n    I dati calcolati sono scritti in bronze_biz_positive\n"
        "models:\n  biz_positive",
        1,
    )
    assert not any("bronze" in src for src, _dst, _t in _edges(files))


def test_a_malformed_models_section_does_not_drop_the_contract() -> None:
    c = parse_contract("src/data/pagopa/x/y.yaml", "id: urn:pagopa:x:y\nmodels:\n  - a\n  - b\n")
    assert c.node_id == "dc:pagopa:x:y" and c.fields == 0
