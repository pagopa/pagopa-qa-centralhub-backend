"""Write side of the EA Map: manual corrections made by people.

Every change made here is a human decision, so it is marked accordingly
(``origin = manual`` for new edges, ``status = verified`` for confirmed or edited ones,
``source = manual`` for new nodes) and recorded in ``ea_edit_log``. The periodic sync
never overwrites these records (see docs/ea-map/DESIGN.md §3.2).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import DATA_EDGE_TYPES, TECH_EDGE_TYPES, EaEdge, EaEditLog, EaNode, EaQaSignal
from app.services.ea.contracts import LAYERS

EDITABLE_EDGE_STATUSES = ("active", "verified")
# which node kinds may be the parent of which
_PARENT_KIND = {"component": "product", "product": "area"}
_CREATABLE_KINDS = ("component", "external", "datastore")
_QA_MANUAL_FIELDS = ("under_qa", "tas_ci_override", "qa_owner", "qa_notes")


class EaEditError(ValueError):
    """Invalid change requested by the user (mapped to 400/404/409 by the API)."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(
    db: AsyncSession,
    action: str,
    entity: str,
    target_id: str,
    actor: str | None,
    **detail: Any,
) -> None:
    db.add(
        EaEditLog(
            action=action,
            entity=entity,
            target_id=target_id,
            actor=actor,
            detail=_jsonable(detail),
        )
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime | uuid.UUID):
        return str(value)
    return value


def edge_payload(e: EaEdge) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "from": e.from_id,
        "to": e.to_id,
        "type": e.type,
        "label": e.label,
        "origin": e.origin,
        "status": e.status,
        "confidence": e.confidence,
        "note": e.note,
        "created_by": e.created_by,
        "verified_by": e.verified_by,
        "verified_at": e.verified_at,
    }


async def _node(db: AsyncSession, node_id: str) -> EaNode:
    node = await db.get(EaNode, node_id)
    if node is None:
        raise EaEditError(f"Nodo non trovato: {node_id}", 404)
    return node


async def _edge(db: AsyncSession, edge_id: uuid.UUID) -> EaEdge:
    edge = await db.get(EaEdge, edge_id)
    if edge is None:
        raise EaEditError("Legame non trovato", 404)
    return edge


def _check_type(edge_type: str) -> None:
    if edge_type not in TECH_EDGE_TYPES + DATA_EDGE_TYPES:
        raise EaEditError(f"Tipo di legame non valido: {edge_type}")


async def create_edge(
    db: AsyncSession,
    *,
    from_id: str,
    to_id: str,
    edge_type: str,
    label: str | None,
    note: str,
    actor: str | None,
) -> EaEdge:
    _check_type(edge_type)
    if from_id == to_id:
        raise EaEditError("Un legame deve collegare due nodi diversi")
    await _node(db, from_id)
    await _node(db, to_id)
    if not note.strip():
        raise EaEditError("La nota è obbligatoria: spiega da dove viene il legame")
    existing = await db.scalar(
        select(EaEdge).where(
            EaEdge.from_id == from_id, EaEdge.to_id == to_id, EaEdge.type == edge_type
        )
    )
    now = _now()
    if existing is not None:
        if existing.status in EDITABLE_EDGE_STATUSES:
            raise EaEditError("Questo legame esiste già", 409)
        before = edge_payload(existing)
        existing.status = "verified"
        existing.label = label or existing.label
        existing.note = note
        existing.verified_by = actor
        existing.verified_at = now
        existing.confidence = 100
        _audit(db, "ea.edge.restore", "edge", str(existing.id), actor, before=before)
        await db.commit()
        await db.refresh(existing)
        return existing
    edge = EaEdge(
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        label=label or None,
        origin="manual",
        status="verified",
        confidence=100,
        note=note,
        created_by=actor,
        verified_by=actor,
        verified_at=now,
        evidence={"source": "manual", "by": actor},
    )
    db.add(edge)
    await db.flush()
    _audit(db, "ea.edge.create", "edge", str(edge.id), actor, after=edge_payload(edge))
    await db.commit()
    await db.refresh(edge)
    return edge


async def update_edge(
    db: AsyncSession, edge_id: uuid.UUID, *, changes: dict[str, Any], actor: str | None
) -> EaEdge:
    edge = await _edge(db, edge_id)
    if edge.status not in EDITABLE_EDGE_STATUSES:
        raise EaEditError("Il legame è stato rimosso: ricrealo per riattivarlo", 409)
    before = edge_payload(edge)
    new_type = changes.get("type", edge.type)
    _check_type(new_type)
    if new_type != edge.type:
        clash = await db.scalar(
            select(EaEdge).where(
                EaEdge.from_id == edge.from_id,
                EaEdge.to_id == edge.to_id,
                EaEdge.type == new_type,
                EaEdge.id != edge.id,
            )
        )
        if clash is not None:
            raise EaEditError("Esiste già un legame di questo tipo tra i due nodi", 409)
        edge.type = new_type
    if "label" in changes:
        edge.label = changes["label"] or None
    if "note" in changes:
        edge.note = changes["note"] or None
    edge.status = "verified"
    edge.confidence = 100
    edge.verified_by = actor
    edge.verified_at = _now()
    _audit(
        db,
        "ea.edge.update",
        "edge",
        str(edge.id),
        actor,
        before=before,
        after=edge_payload(edge),
    )
    await db.commit()
    await db.refresh(edge)
    return edge


async def verify_edge(
    db: AsyncSession, edge_id: uuid.UUID, *, note: str | None, actor: str | None
) -> EaEdge:
    edge = await _edge(db, edge_id)
    if edge.status not in EDITABLE_EDGE_STATUSES:
        raise EaEditError("Il legame è stato rimosso", 409)
    edge.status = "verified"
    edge.confidence = 100
    edge.verified_by = actor
    edge.verified_at = _now()
    if note:
        edge.note = note
    _audit(db, "ea.edge.verify", "edge", str(edge.id), actor, edge=edge_payload(edge))
    await db.commit()
    await db.refresh(edge)
    return edge


async def remove_edge(
    db: AsyncSession, edge_id: uuid.UUID, *, note: str | None, actor: str | None
) -> EaEdge:
    """Logical delete: the edge stays in the table so the sync will not propose it again."""
    edge = await _edge(db, edge_id)
    if edge.status == "removed":
        return edge
    before = edge_payload(edge)
    edge.status = "removed"
    edge.verified_by = actor
    edge.verified_at = _now()
    if note:
        edge.note = note
    _audit(db, "ea.edge.remove", "edge", str(edge.id), actor, before=before)
    await db.commit()
    await db.refresh(edge)
    return edge


async def update_node(
    db: AsyncSession, node_id: str, *, changes: dict[str, Any], actor: str | None
) -> EaNode:
    node = await _node(db, node_id)
    before = {
        "name": node.name,
        "description": node.description,
        "parent_id": node.parent_id,
        "attrs": dict(node.attrs or {}),
    }
    if "name" in changes:
        name = (changes["name"] or "").strip()
        if not name:
            raise EaEditError("Il nome non può essere vuoto")
        node.name = name
    if "description" in changes:
        node.description = changes["description"] or None
    if "parent_id" in changes and changes["parent_id"] != node.parent_id:
        expected = _PARENT_KIND.get(node.kind)
        if expected is None:
            raise EaEditError(f"Un nodo di tipo {node.kind} non ha un genitore modificabile")
        parent = await _node(db, changes["parent_id"])
        if parent.kind != expected:
            raise EaEditError(f"Il genitore di un {node.kind} deve essere un {expected}")
        node.parent_id = parent.id
    if "ctype" in changes and node.kind == "component":
        node.attrs = {**(node.attrs or {}), "ctype": changes["ctype"]}
    if "layer" in changes:
        if node.kind != "dataset":
            raise EaEditError("Il livello si può correggere solo sui dataset del Data Lake")
        layer = changes["layer"]
        if layer is not None and layer not in LAYERS:
            raise EaEditError(f"Livello non valido: {layer}")
        attrs = {k: v for k, v in (node.attrs or {}).items() if k != "layer_override"}
        node.attrs = {**attrs, "layer_override": layer} if layer else attrs
    if node.kind != "dataset" or set(changes) - {"layer"}:
        # a dataset's name and description come from its contract: correcting only the
        # layer must not freeze them
        node.verified_by = actor
        node.verified_at = _now()
    _audit(
        db,
        "ea.node.update",
        "node",
        node.id,
        actor,
        before=before,
        after={
            "name": node.name,
            "description": node.description,
            "parent_id": node.parent_id,
            "attrs": node.attrs,
        },
    )
    await db.commit()
    await db.refresh(node)
    return node


async def create_node(
    db: AsyncSession,
    *,
    node_id: str,
    kind: str,
    name: str,
    description: str | None,
    parent_id: str | None,
    attrs: dict[str, Any],
    actor: str | None,
) -> EaNode:
    if kind not in _CREATABLE_KINDS:
        raise EaEditError(f"Si possono creare solo nodi di tipo {', '.join(_CREATABLE_KINDS)}")
    node_id = node_id.strip()
    if not node_id or any(c.isspace() for c in node_id) or ":" in node_id:
        raise EaEditError("Identificativo non valido: niente spazi né ':'")
    if await db.get(EaNode, node_id) is not None:
        raise EaEditError("Esiste già un nodo con questo identificativo", 409)
    if kind == "component":
        if not parent_id:
            raise EaEditError("Un componente deve appartenere a un prodotto")
        parent = await _node(db, parent_id)
        if parent.kind != "product":
            raise EaEditError("Il genitore di un componente deve essere un prodotto")
    else:
        parent_id = None
    now = _now()
    node = EaNode(
        id=node_id,
        kind=kind,
        name=name.strip(),
        description=description or None,
        parent_id=parent_id,
        attrs=attrs,
        source="manual",
        status="active",
        verified_by=actor,
        verified_at=now,
        sort_order=10_000,
    )
    db.add(node)
    if kind == "component":
        db.add(EaQaSignal(node_id=node_id, central_suites=[], nrt_suites=[]))
    await db.flush()
    _audit(
        db,
        "ea.node.create",
        "node",
        node_id,
        actor,
        after={"kind": kind, "name": node.name, "parent_id": parent_id, "attrs": attrs},
    )
    await db.commit()
    await db.refresh(node)
    return node


async def update_qa(
    db: AsyncSession, node_id: str, *, changes: dict[str, Any], actor: str | None
) -> EaQaSignal:
    node = await _node(db, node_id)
    if node.kind != "component":
        raise EaEditError("I dati QA si gestiscono solo sui componenti")
    sig = await db.get(EaQaSignal, node_id)
    if sig is None:
        sig = EaQaSignal(node_id=node_id, central_suites=[], nrt_suites=[])
        db.add(sig)
    before = {f: getattr(sig, f) for f in _QA_MANUAL_FIELDS}
    for field in _QA_MANUAL_FIELDS:
        if field in changes:
            value = changes[field]
            setattr(sig, field, value.strip() or None if isinstance(value, str) else value)
    sig.manual_updated_by = actor
    sig.manual_updated_at = _now()
    _audit(
        db,
        "ea.qa.update",
        "node",
        node_id,
        actor,
        before=before,
        after={f: getattr(sig, f) for f in _QA_MANUAL_FIELDS},
    )
    await db.commit()
    await db.refresh(sig)
    return sig


async def node_history(db: AsyncSession, node_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Audit entries about the node itself and about edges touching it."""
    edge_ids = [
        str(i)
        for i in await db.scalars(
            select(EaEdge.id).where((EaEdge.from_id == node_id) | (EaEdge.to_id == node_id))
        )
    ]
    rows = await db.scalars(
        select(EaEditLog)
        .where(
            ((EaEditLog.entity == "node") & (EaEditLog.target_id == node_id))
            | ((EaEditLog.entity == "edge") & (EaEditLog.target_id.in_(edge_ids or [""])))
        )
        .order_by(EaEditLog.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "action": r.action,
            "resource_type": r.entity,
            "resource_id": r.target_id,
            "actor": r.actor,
            "at": r.created_at,
            "detail": r.detail,
        }
        for r in rows
    ]
