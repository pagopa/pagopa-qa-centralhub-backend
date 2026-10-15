from __future__ import annotations

# ruff: noqa: S101
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import EaEdge, EaEditLog, EaQaSignal
from app.services.ea import edit
from app.services.ea.edit import EaEditError
from app.services.ea.importer import import_kb
from app.services.ea.kb import build_kb
from tests._db import TestSession
from tests.test_ea_service import _mini_kb, _wipe

ACTOR = "qa.manager@example.test"


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await _wipe(session)
        await import_kb(session, _mini_kb())
        yield session
        await session.rollback()
        await _wipe(session)


async def _edge(db: AsyncSession, src: str, dst: str) -> EaEdge:
    edge = await db.scalar(select(EaEdge).where(EaEdge.from_id == src, EaEdge.to_id == dst))
    assert edge is not None
    return edge


async def test_create_manual_edge_is_verified_and_audited(db: AsyncSession) -> None:
    edge = await edit.create_edge(
        db,
        from_id="svc-b",
        to_id="ext-psp",
        edge_type="event",
        label="notifica",
        note="Confluence: flusso X",
        actor=ACTOR,
    )
    assert (edge.origin, edge.status, edge.confidence) == ("manual", "verified", 100)
    assert edge.created_by == ACTOR
    kb = await build_kb(db)
    assert any(
        e["from"] == "svc-b" and e["to"] == "ext-psp" and e["origin"] == "manual"
        for e in kb["edges"]
    )
    log = await db.scalar(select(EaEditLog).where(EaEditLog.action == "ea.edge.create"))
    assert (
        log is not None
        and log.actor == ACTOR
        and log.detail["after"]["note"] == "Confluence: flusso X"
    )


async def test_create_rejects_duplicates_bad_types_and_missing_note(db: AsyncSession) -> None:
    with pytest.raises(EaEditError) as dup:
        await edit.create_edge(
            db, from_id="svc-a", to_id="svc-b", edge_type="api", label=None, note="x", actor=ACTOR
        )
    assert dup.value.status_code == 409
    with pytest.raises(EaEditError):
        await edit.create_edge(
            db,
            from_id="svc-a",
            to_id="svc-b",
            edge_type="serves",
            label=None,
            note="x",
            actor=ACTOR,
        )
    with pytest.raises(EaEditError):
        await edit.create_edge(
            db, from_id="svc-a", to_id="ds-x", edge_type="data", label=None, note="  ", actor=ACTOR
        )
    with pytest.raises(EaEditError) as missing:
        await edit.create_edge(
            db, from_id="svc-a", to_id="nope", edge_type="api", label=None, note="x", actor=ACTOR
        )
    assert missing.value.status_code == 404


async def test_remove_hides_edge_and_recreate_restores_it(db: AsyncSession) -> None:
    edge = await _edge(db, "svc-a", "svc-b")
    await edit.remove_edge(db, edge.id, note="non esiste più", actor=ACTOR)
    kb = await build_kb(db)
    assert not any(e["from"] == "svc-a" and e["to"] == "svc-b" for e in kb["edges"])
    with pytest.raises(EaEditError):
        await edit.update_edge(db, edge.id, changes={"label": "x"}, actor=ACTOR)
    restored = await edit.create_edge(
        db, from_id="svc-a", to_id="svc-b", edge_type="api", label=None, note="tornato", actor=ACTOR
    )
    assert restored.id == edge.id
    assert restored.status == "verified"


async def test_update_and_verify_edge(db: AsyncSession) -> None:
    edge = await _edge(db, "svc-a", "svc-b")
    assert edge.origin == "helm"
    updated = await edit.update_edge(
        db, edge.id, changes={"type": "event", "label": "via Event Hub"}, actor=ACTOR
    )
    assert (updated.type, updated.label, updated.status, updated.origin) == (
        "event",
        "via Event Hub",
        "verified",
        "helm",
    )
    other = await _edge(db, "ext-psp", "svc-a")
    verified = await edit.verify_edge(db, other.id, note="confermato dal team", actor=ACTOR)
    assert verified.verified_by == ACTOR and verified.note == "confermato dal team"


async def test_update_node_validates_parent_kind(db: AsyncSession) -> None:
    node = await edit.update_node(
        db, "svc-a", changes={"name": "Servizio A", "description": "nuova"}, actor=ACTOR
    )
    assert node.name == "Servizio A" and node.verified_by == ACTOR
    with pytest.raises(EaEditError):
        await edit.update_node(db, "svc-a", changes={"parent_id": "area:core"}, actor=ACTOR)
    with pytest.raises(EaEditError):
        await edit.update_node(db, "svc-a", changes={"name": " "}, actor=ACTOR)


async def test_create_manual_component_appears_in_kb(db: AsyncSession) -> None:
    await edit.create_node(
        db,
        node_id="svc-new",
        kind="component",
        name="Nuovo",
        description="manuale",
        parent_id="prod:nodo",
        attrs={"ctype": "be", "repo": None},
        actor=ACTOR,
    )
    kb = await build_kb(db)
    comp = next(n for n in kb["nodes"] if n["id"] == "svc-new")
    assert comp["source"] == "manual" and comp["qa"]["score"] == 0
    with pytest.raises(EaEditError):
        await edit.create_node(
            db,
            node_id="svc-new",
            kind="component",
            name="Dup",
            description=None,
            parent_id="prod:nodo",
            attrs={},
            actor=ACTOR,
        )
    with pytest.raises(EaEditError):
        await edit.create_node(
            db,
            node_id="bad id",
            kind="external",
            name="X",
            description=None,
            parent_id=None,
            attrs={},
            actor=ACTOR,
        )


async def test_qa_manual_fields_can_be_set_and_cleared(db: AsyncSession) -> None:
    sig = await edit.update_qa(
        db, "svc-a", changes={"under_qa": True, "qa_owner": "Team QA"}, actor=ACTOR
    )
    assert sig.under_qa is True and sig.qa_owner == "Team QA"
    sig = await edit.update_qa(db, "svc-a", changes={"under_qa": None}, actor=ACTOR)
    assert sig.under_qa is None and sig.qa_owner == "Team QA"
    stored = await db.get(EaQaSignal, "svc-a")
    assert stored is not None and stored.unit_files == 4  # observed signals untouched
    with pytest.raises(EaEditError):
        await edit.update_qa(db, "prod:nodo", changes={"under_qa": True}, actor=ACTOR)


async def test_history_collects_node_and_edge_events(db: AsyncSession) -> None:
    edge = await _edge(db, "svc-a", "svc-b")
    await edit.verify_edge(db, edge.id, note=None, actor=ACTOR)
    await edit.update_qa(db, "svc-a", changes={"under_qa": True}, actor=ACTOR)
    history = await edit.node_history(db, "svc-a")
    assert {h["action"] for h in history} == {"ea.edge.verify", "ea.qa.update"}
    assert all(h["actor"] == ACTOR for h in history)


@pytest.mark.anyio
async def test_api_maps_edit_errors(client: AsyncClient) -> None:
    async def boom(*_args: object, **_kwargs: object) -> None:
        raise EaEditError("Questo legame esiste già", 409)

    from unittest.mock import patch

    with patch("app.services.ea.edit.create_edge", side_effect=boom):
        response = await client.post(
            "/api/v1/ea/edges",
            json={"from_id": "a", "to_id": "b", "type": "api", "note": "x", "actor": ACTOR},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "Questo legame esiste già"


async def test_api_detail_and_history_after_manual_edge(
    db: AsyncSession, client: AsyncClient
) -> None:
    created = await client.post(
        "/api/v1/ea/edges",
        json={
            "from_id": "svc-b",
            "to_id": "ext-psp",
            "type": "api",
            "note": "da Confluence",
            "actor": ACTOR,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["from"] == "svc-b" and created.json()["origin"] == "manual"
    detail = (await client.get("/api/v1/ea/nodes/ext-psp")).json()
    incoming = [e for e in detail["in"] if e["from"] == "svc-b"]
    assert (
        incoming and incoming[0]["verified_by"] == ACTOR and incoming[0]["note"] == "da Confluence"
    )
    history = (await client.get("/api/v1/ea/nodes/svc-b/history")).json()
    assert [h["action"] for h in history] == ["ea.edge.create"]
    assert history[0]["actor"] == ACTOR
