from __future__ import annotations

# ruff: noqa: S101
import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import (
    EaChange,
    EaDocLink,
    EaEdge,
    EaEditLog,
    EaMeta,
    EaNode,
    EaQaSignal,
    EaRepoSnapshot,
    EaSyncRun,
    EaSyncStatus,
)
from app.services.ea.impact import impact
from app.services.ea.importer import EaImportError, import_kb
from app.services.ea.kb import build_kb, get_node_detail
from app.services.ea.scoring import QaFacts, maturity_score
from tests._db import TestSession

SEED = Path(__file__).resolve().parents[1] / "scripts" / "data" / "ea_kb_seed.json"


async def _wipe(session: AsyncSession) -> None:
    for model in (
        EaEditLog,
        EaChange,
        EaSyncStatus,
        EaSyncRun,
        EaRepoSnapshot,
        EaDocLink,
        EaQaSignal,
        EaEdge,
    ):
        await session.execute(delete(model))
    await session.execute(delete(EaNode).where(EaNode.parent_id.is_not(None)))
    await session.execute(delete(EaNode))
    await session.execute(delete(EaMeta))
    await session.commit()


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await _wipe(session)
        yield session
        await session.rollback()
        await _wipe(session)


def _mini_kb() -> dict[str, Any]:
    """Smallest KB covering every node and edge kind."""
    return {
        "meta": {"generated": "2026-10-15", "sources": {"confluence_pages": 10}},
        "nodes": [
            {"id": "area:core", "type": "area", "name": "Nodo", "short": "Core", "desc": "core"},
            {
                "id": "prod:nodo",
                "type": "product",
                "parent": "area:core",
                "name": "NdP",
                "desc": "",
                "domain": "nodo",
                "conf": [
                    {
                        "space": "PAG",
                        "id": "1",
                        "title": "Spec",
                        "pages": 3,
                        "url": "https://example.test/1",
                    }
                ],
            },
            {
                "id": "svc-a",
                "type": "component",
                "parent": "prod:nodo",
                "name": "Svc A",
                "desc": "a",
                "kind": "be",
                "repo": "https://github.com/pagopa/svc-a",
                "private": False,
                "lang": ["Java"],
                "fw": [],
                "ns": "nodo",
                "ingress": None,
                "owners": ["team"],
                "apis": [{"file": "openapi/a.json", "title": "A API"}],
                "readme": "",
                "workflows": [],
                "confRepo": ["https://pagopa.atlassian.net/wiki/x/abc"],
                "qa": {
                    "unit": 4,
                    "it_repo": 2,
                    "it_wf": False,
                    "perf_repo": True,
                    "e2e_repo": False,
                    "sonar": True,
                    "central": ["integration/a"],
                    "nrt": ["a"],
                    "tas": True,
                    "tas_ci": False,
                    "nodo_bdd": False,
                    "under_qa": None,
                    "score": 5,
                },
            },
            {
                "id": "svc-b",
                "type": "component",
                "parent": "prod:nodo",
                "name": "Svc B",
                "desc": "b",
                "kind": "fn",
                "repo": "https://github.com/pagopa/svc-b",
                "private": False,
                "lang": [],
                "fw": [],
                "ns": None,
                "ingress": None,
                "owners": [],
                "apis": [],
                "readme": "",
                "workflows": [],
                "confRepo": [],
                "qa": {"unit": 0},
            },
            {"id": "ext-psp", "type": "external", "name": "PSP", "desc": "psp"},
            {"id": "ds-x", "type": "datastore", "name": "Cosmos X", "desc": ""},
        ],
        "edges": [
            {"from": "svc-a", "to": "svc-b", "type": "api", "origin": "helm"},
            {"from": "svc-b", "to": "ds-x", "type": "data", "origin": "helm"},
            {
                "from": "ext-psp",
                "to": "svc-a",
                "type": "soap",
                "origin": "curated",
                "label": "primitive",
            },
        ],
        "blueprint": {
            "segments": [
                {"id": "seg-psp", "name": "PSP", "who": ["Banche"], "actors": ["ext-psp"]}
            ],
            "services": [
                {"id": "sv-pay", "name": "Pagamento", "seg": ["seg-psp"], "proc": ["pr-m3"]}
            ],
            "functions": [{"id": "fn-att", "name": "Attivazione"}],
            "processes": [
                {
                    "id": "pr-m3",
                    "name": "Modello 3",
                    "fn": "fn-att",
                    "prod": ["nodo"],
                    "ref": None,
                    "suites": ["Nodo BDD"],
                }
            ],
            "infra": [
                {"id": "in-apim", "name": "APIM", "group": "edge", "desc": ""},
                {"id": "ns-nodo", "name": "nodo", "group": "aks", "desc": ""},
            ],
            "prodInfra": {"nodo": ["in-apim", "ns-nodo"]},
            "risk": {"nodo": "HIGH", "in-apim": "HIGH"},
            "riskSrc": "https://example.test/risk",
            "panels": {"mission": "m"},
        },
    }


def test_maturity_score_counts_each_level() -> None:
    assert maturity_score(QaFacts()) == 0
    assert maturity_score(QaFacts(unit_files=1)) == 1
    assert maturity_score(QaFacts(nodo_bdd=True)) == 2
    full = QaFacts(unit_files=3, it_features=1, perf=True, central_suites=("x",), nrt_suites=("x",))
    assert maturity_score(full) == 5


async def test_import_and_build_roundtrip(db: AsyncSession) -> None:
    stats = await import_kb(db, _mini_kb())
    assert stats["nodes"] == 6 + 1 + 1 + 1 + 1 + 2
    # 3 technical + serves + realizes + groups + uses + 2 runs_on
    assert stats["edges"] == 3 + 4 + 2

    kb = await build_kb(db)
    comp = next(n for n in kb["nodes"] if n["id"] == "svc-a")
    assert comp["qa"]["score"] == 5
    assert comp["kindLabel"] == "Microservizio"
    assert comp["confRepo"] == ["https://pagopa.atlassian.net/wiki/x/abc"]
    assert comp["apis"] == [{"f": "openapi/a.json", "t": "A API"}]
    prod = next(n for n in kb["nodes"] if n["id"] == "prod:nodo")
    assert prod["conf"][0]["title"] == "Spec"
    assert {e["origin"] for e in kb["edges"]} == {"helm", "curated"}
    bp = kb["blueprint"]
    assert bp["services"][0]["seg"] == ["seg-psp"]
    assert bp["processes"][0]["fn"] == "fn-att"
    assert bp["processes"][0]["prod"] == ["nodo"]
    assert bp["prodInfra"] == {"nodo": ["in-apim", "ns-nodo"]}
    assert bp["risk"]["nodo"] == "HIGH"
    assert bp["panels"] == {"mission": "m"}

    runs_on = list(await db.scalars(select(EaEdge).where(EaEdge.type == "runs_on")))
    assert {e.origin for e in runs_on} == {"infra_rule"}


async def test_import_refuses_non_empty_without_replace(db: AsyncSession) -> None:
    await import_kb(db, _mini_kb())
    with pytest.raises(EaImportError):
        await import_kb(db, _mini_kb())
    stats = await import_kb(db, _mini_kb(), replace=True)
    assert stats["nodes"] == 12
    assert await db.scalar(select(func.count()).select_from(EaNode)) == 12


async def test_import_rejects_dangling_references(db: AsyncSession) -> None:
    kb = copy.deepcopy(_mini_kb())
    kb["edges"].append({"from": "svc-a", "to": "missing", "type": "api", "origin": "helm"})
    with pytest.raises(EaImportError, match="missing"):
        await import_kb(db, kb)


async def test_hidden_edges_are_not_served(db: AsyncSession) -> None:
    await import_kb(db, _mini_kb())
    edge = await db.scalar(select(EaEdge).where(EaEdge.from_id == "svc-a", EaEdge.to_id == "svc-b"))
    assert edge is not None
    edge.status = "removed"
    await db.commit()
    kb = await build_kb(db)
    assert not any(e["from"] == "svc-a" and e["to"] == "svc-b" for e in kb["edges"])


async def test_impact_walks_both_directions(db: AsyncSession) -> None:
    await import_kb(db, _mini_kb())
    result = await impact(db, "svc-a", depth=3)
    down = {i["id"]: i["depth"] for i in result["downstream"]}
    up = {i["id"]: i["depth"] for i in result["upstream"]}
    assert down == {"svc-b": 1, "ds-x": 2}
    assert up == {"ext-psp": 1}


async def test_node_detail_lists_edges(db: AsyncSession) -> None:
    await import_kb(db, _mini_kb())
    detail = await get_node_detail(db, "svc-a")
    assert detail is not None
    assert [e["to"] for e in detail["out"]] == ["svc-b"]
    assert [e["from"] for e in detail["in"]] == ["ext-psp"]
    assert detail["qa"]["nrt"] == ["a"]
    assert await get_node_detail(db, "nope") is None


async def test_seed_file_imports_cleanly(db: AsyncSession) -> None:
    kb = json.loads(SEED.read_text(encoding="utf-8"))
    stats = await import_kb(db, kb)
    assert stats["nodes"] > 250
    served = await build_kb(db)
    seed_components = [n for n in kb["nodes"] if n["type"] == "component"]
    served_components = {n["id"]: n for n in served["nodes"] if n["type"] == "component"}
    assert len(served_components) == len(seed_components)
    # scores computed server-side match the offline pipeline
    for n in seed_components:
        assert served_components[n["id"]]["qa"]["score"] == n["qa"]["score"], n["id"]
    assert len(served["edges"]) == len(kb["edges"])
    expected_infra = {k: v for k, v in kb["blueprint"]["prodInfra"].items() if v}
    assert served["blueprint"]["prodInfra"] == expected_infra
