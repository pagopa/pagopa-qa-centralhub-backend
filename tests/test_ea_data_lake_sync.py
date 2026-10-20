from __future__ import annotations

# ruff: noqa: S101
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import EaChange, EaEdge, EaMeta, EaNode, EaRepoSnapshot
from app.services.ea import edit
from app.services.ea.contracts import CONTRACTS_REPO, EXT_BI, EXT_MEF
from app.services.ea.impact import impact
from app.services.ea.importer import import_kb
from app.services.ea.kb import build_kb, get_node_detail
from app.services.ea.sync import review
from app.services.ea.sync.engine import create_run, edge_key, execute_run
from tests._db import TestSession
from tests.test_ea_contracts import contract_files
from tests.test_ea_service import _wipe
from tests.test_ea_sync import NOW, FakeConfluence, FakeGitHub, _comp, _kb, _repo

GOLD = "dc:pagopa:gold:payments"
SILVER = "dc:pagopa:biz:positive-denorm-increment"
BIZ = "dc:pagopa:biz:positive"


class ContractsGitHub(FakeGitHub):
    def __init__(self, files: dict[str, str] | None = None) -> None:
        super().__init__()
        self.listing.append(_repo(CONTRACTS_REPO))
        self.set_contracts(files if files is not None else contract_files())

    def set_contracts(self, files: dict[str, str], pushed: Any = None) -> None:
        self.trees[CONTRACTS_REPO] = [*files, "README.md"]
        self.files[CONTRACTS_REPO] = dict(files)
        self.listing = [r for r in self.listing if r.name != CONTRACTS_REPO]
        self.listing.append(_repo(CONTRACTS_REPO, pushed=pushed or NOW))


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await _wipe(session)
        kb = _kb()
        kb["nodes"].append(
            {"id": "ext-bdi", "type": "external", "name": "Banca d'Italia", "desc": ""}
        )
        kb["nodes"].append(
            _comp("pagopa-gpd-ingestion-manager", "prod:gpd", ["team-gps"])
            | {"name": "GPD Ingestion Manager"}
        )
        await import_kb(session, kb)
        yield session
        await session.rollback()
        await _wipe(session)


async def _sync(db: AsyncSession, gh: FakeGitHub) -> dict[str, Any]:
    run = await create_run(db, trigger="manual", actor="tester")
    return await execute_run(db, run.id, gh, FakeConfluence())


async def _edge(db: AsyncSession, src: str, dst: str, edge_type: str) -> EaEdge | None:
    return await db.scalar(
        select(EaEdge).where(EaEdge.from_id == src, EaEdge.to_id == dst, EaEdge.type == edge_type)
    )


async def test_first_sync_creates_datasets_lineage_and_consumers(db: AsyncSession) -> None:
    summary = await _sync(db, ContractsGitHub())
    assert summary["status"] == "success", summary
    assert summary["stats"]["datasets"] == 10 and summary["stats"]["data_contracts_read"]

    gold = await db.get(EaNode, GOLD)
    assert gold is not None and gold.kind == "dataset" and gold.source == "sync"
    assert gold.attrs["layer"] == "gold" and gold.attrs["table"] == "pagopa.gold_payments"
    for consumer in (EXT_BI, EXT_MEF):
        node = await db.get(EaNode, consumer)
        assert node is not None and node.kind == "external"

    lineage = await _edge(db, SILVER, GOLD, "derives")
    assert lineage is not None and lineage.status == "active" and lineage.origin == "contract"
    bi = await _edge(db, GOLD, EXT_BI, "feeds")
    assert bi is not None and bi.origin == "curated"
    assert await _edge(db, "dc:pagopa:bdi:ec", "ext-bdi", "feeds") is not None

    # contracts are facts: nothing goes to review in PR1
    pending = await db.scalars(select(EaChange).where(EaChange.status == "pending"))
    assert not [c for c in pending if c.target_id.startswith(("dc:", "dl:"))]
    meta = await db.get(EaMeta, "dq_result_tables")
    assert meta is not None and meta.value[0]["domain"] == "gec"
    assert summary["failures"] == {}
    snap = await db.get(EaRepoSnapshot, CONTRACTS_REPO)
    assert snap is not None and len(snap.extracted["nodes"]) == 10


async def test_kb_detail_and_impact_include_the_data_lake(db: AsyncSession) -> None:
    await _sync(db, ContractsGitHub())
    kb = await build_kb(db)
    by_id = {n["id"]: n for n in kb["nodes"]}
    assert by_id[GOLD]["type"] == "dataset" and by_id[GOLD]["layer"] == "gold"
    assert by_id[GOLD]["checks"] == 2 and by_id[GOLD]["owner"] == "Data Lake Team"
    assert by_id["dl:pagopa.silver_negative"]["contracted"] is False
    assert by_id["dc:pagopa:gpd:payment-position"]["pii"] == 1
    assert any(
        e["from"] == SILVER and e["to"] == GOLD and e["type"] == "derives" for e in kb["edges"]
    )

    detail = await get_node_detail(db, GOLD)
    assert detail is not None and detail["attrs"]["contract"]["url"].endswith(
        "dc-gold-payments.yaml"
    )

    down = {n["id"] for n in (await impact(db, BIZ, depth=3))["downstream"]}
    assert {SILVER, GOLD, EXT_BI, "dc:pagopa:mef:tari-tefa", EXT_MEF} <= down


async def test_unchanged_repository_is_not_read_again(db: AsyncSession) -> None:
    gh = ContractsGitHub()
    gh.listing = [r for r in gh.listing if r.name != CONTRACTS_REPO]
    gh.listing.append(_repo(CONTRACTS_REPO, pushed=NOW - timedelta(days=1)))
    await _sync(db, gh)
    calls = gh.tree_calls.count(CONTRACTS_REPO)
    summary = await _sync(db, gh)
    assert gh.tree_calls.count(CONTRACTS_REPO) == calls
    assert summary["stats"]["auto_applied"] == 0 and summary["stats"]["datasets"] == 10


async def test_contract_changes_are_applied_and_people_keep_their_choices(db: AsyncSession) -> None:
    gh = ContractsGitHub()
    await _sync(db, gh)

    # a person corrects the layer, removes the BI edge and verifies a lineage edge
    gold = await db.get(EaNode, GOLD)
    assert gold is not None
    gold.attrs = {**gold.attrs, "layer_override": "silver"}
    await db.commit()
    bi = await _edge(db, GOLD, EXT_BI, "feeds")
    assert bi is not None
    await edit.remove_edge(db, bi.id, note="non usata dalla BI", actor="qa")
    lineage = await _edge(db, SILVER, GOLD, "derives")
    assert lineage is not None
    await edit.verify_edge(db, lineage.id, note="confermato", actor="qa")

    files = contract_files()
    path = "src/data/pagopa/biz/dc-gold-payments.yaml"
    files[path] = (
        files[path]
        .replace("version: 0.0.1", "version: 0.0.2")
        .replace("derivata da silver_positive_denorm_increment", "aggregata")
        .replace("derivata dalla tabella silver_positive_denorm_increment", "consolidata")
    )
    del files["src/data/pagopa/biz/dc-gold-payments-negative.yaml"]
    gh.set_contracts(files, pushed=NOW + timedelta(minutes=5))
    summary = await _sync(db, gh)
    assert summary["status"] == "success"

    gold = await db.get(EaNode, GOLD)
    assert gold is not None
    await db.refresh(gold)
    assert gold.attrs["contract"]["version"] == "0.0.2"
    assert gold.attrs["layer_override"] == "silver"  # kept
    kb = await build_kb(db)
    assert next(n for n in kb["nodes"] if n["id"] == GOLD)["layer"] == "silver"

    bi = await _edge(db, GOLD, EXT_BI, "feeds")
    assert bi is not None and bi.status == "removed"  # not recreated by the sync

    open_items = {
        (c.entity, c.op, c.target_id): c.status
        for c in await db.scalars(
            select(EaChange).where(EaChange.status.in_(("pending", "conflict")))
        )
    }
    # verified lineage no longer declared → a person decides
    assert open_items[("edge", "remove", edge_key(SILVER, GOLD, "derives"))] == "conflict"
    # contract removed from the repository → archive proposal
    assert open_items[("node", "update", "dc:pagopa:gold:payments-negative")] == "pending"


async def test_unreadable_or_invisible_repository_keeps_the_datasets(db: AsyncSession) -> None:
    gh = ContractsGitHub()
    await _sync(db, gh)

    gh.set_contracts(contract_files(), pushed=NOW + timedelta(minutes=5))
    gh.fail.add(CONTRACTS_REPO)
    summary = await _sync(db, gh)
    assert summary["status"] == "partial" and "Data contract" in (summary["error"] or "")
    assert await db.get(EaNode, GOLD) is not None

    gh.fail.clear()
    gh.listing = [r for r in gh.listing if r.name != CONTRACTS_REPO]
    summary = await _sync(db, gh)
    assert summary["status"] == "success"  # not visible to the token: noted, not a failure
    node = await db.get(EaNode, GOLD)
    assert node is not None and node.status == "active"


async def test_data_edges_can_be_created_by_hand(db: AsyncSession) -> None:
    await _sync(db, ContractsGitHub())
    e = await edit.create_edge(
        db,
        from_id="pagopa-debt-position",
        to_id="dc:pagopa:gpd:payment-position",
        edge_type="produces",
        label=None,
        note="CDC della tabella payment_position",
        actor="qa",
    )
    assert e.type == "produces" and e.origin == "manual"


GPD_IN = "dc:pagopa:gpd:payment-position"
GPD_MGR = "pagopa-gpd-ingestion-manager"


async def test_producers_are_proposed_and_approved_in_review(db: AsyncSession) -> None:
    await _sync(db, ContractsGitHub())
    target = edge_key(GPD_MGR, GPD_IN, "produces")
    change = await db.scalar(select(EaChange).where(EaChange.target_id == target))
    assert change is not None and change.status == "pending" and change.structural
    assert change.after is not None and change.after["origin"] == "contract_rule"
    assert change.after["evidence"]["topic"] == "gpd-ingestion.apd.payment_position"
    assert change.after["confidence"] == 90
    # not in the map until a person decides
    assert await _edge(db, GPD_MGR, GPD_IN, "produces") is None

    await review.decide(db, change.id, approve=True, actor="qa")
    edge = await _edge(db, GPD_MGR, GPD_IN, "produces")
    assert edge is not None and edge.status == "verified" and edge.origin == "contract_rule"
    kb = await build_kb(db)
    assert any(e["from"] == GPD_MGR and e["type"] == "produces" for e in kb["edges"])

    # approved: the next sync does not propose it again
    gh = ContractsGitHub()
    gh.set_contracts(contract_files(), pushed=NOW + timedelta(minutes=5))
    await _sync(db, gh)
    again = await db.scalars(select(EaChange).where(EaChange.target_id == target))
    assert [c.status for c in again] == ["approved"]


async def test_rejected_or_hand_made_producers_stop_the_proposals(db: AsyncSession) -> None:
    await _sync(db, ContractsGitHub())
    change = await db.scalar(
        select(EaChange).where(EaChange.target_id == edge_key(GPD_MGR, GPD_IN, "produces"))
    )
    assert change is not None
    await review.decide(db, change.id, approve=False, note="non è lui", actor="qa")
    rejected = await _edge(db, GPD_MGR, GPD_IN, "produces")
    assert rejected is not None and rejected.status == "rejected"
    assert rejected.origin == "contract_rule"

    other = "dc:pagopa:biz:positive"
    await edit.create_edge(
        db,
        from_id="pagopa-gpd-payments",
        to_id=other,
        edge_type="produces",
        label=None,
        note="prova",
        actor="qa",
    )
    gh = ContractsGitHub()
    gh.set_contracts(contract_files(), pushed=NOW + timedelta(minutes=5))
    summary = await _sync(db, gh)
    assert summary["status"] == "success"
    open_targets = {
        c.target_id for c in await db.scalars(select(EaChange).where(EaChange.status == "pending"))
    }
    assert not any(t.endswith((f">{GPD_IN}:produces", f">{other}:produces")) for t in open_targets)


async def test_layer_can_be_corrected_without_freezing_the_contract(db: AsyncSession) -> None:
    await _sync(db, ContractsGitHub())
    node = await edit.update_node(db, GOLD, changes={"layer": "silver"}, actor="qa")
    assert node.attrs["layer_override"] == "silver" and node.verified_at is None
    kb = await build_kb(db)
    assert next(n for n in kb["nodes"] if n["id"] == GOLD)["layer"] == "silver"
    node = await edit.update_node(db, GOLD, changes={"layer": None}, actor="qa")
    assert "layer_override" not in node.attrs
    with pytest.raises(edit.EaEditError):
        await edit.update_node(db, GOLD, changes={"layer": "platinum"}, actor="qa")
    with pytest.raises(edit.EaEditError):
        await edit.update_node(db, "pagopa-debt-position", changes={"layer": "gold"}, actor="qa")
