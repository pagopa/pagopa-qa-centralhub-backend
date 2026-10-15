"""Review queue of the EA sync: people approve or reject structural proposals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import EaChange, EaDocLink, EaEdge, EaEditLog, EaNode, EaQaSignal
from app.services.ea.edit import EaEditError

REVIEWABLE = ("pending", "conflict")
_CTYPES = ("be", "fn", "fe", "lib", "infra", "test", "mock", "tool", "doc")
_QA_FIELDS = (
    "unit_files",
    "it_features",
    "it_workflow",
    "perf",
    "e2e",
    "sonar",
    "central_suites",
    "nrt_suites",
    "nodo_bdd",
    "tas_ci_observed",
)


def _now() -> datetime:
    return datetime.now(UTC)


def change_payload(c: EaChange) -> dict[str, Any]:
    return {
        "id": str(c.id),
        "run_id": str(c.run_id) if c.run_id else None,
        "entity": c.entity,
        "op": c.op,
        "target_id": c.target_id,
        "before": c.before,
        "after": c.after,
        "structural": c.structural,
        "status": c.status,
        "reviewed_by": c.reviewed_by,
        "reviewed_at": c.reviewed_at,
        "review_note": c.review_note,
        "created_at": c.created_at,
    }


async def list_changes(
    db: AsyncSession,
    *,
    statuses: tuple[str, ...] = REVIEWABLE,
    run_id: uuid.UUID | None = None,
    limit: int = 500,
) -> list[EaChange]:
    q = select(EaChange).where(EaChange.status.in_(statuses))
    if run_id is not None:
        q = q.where(EaChange.run_id == run_id)
    return list(await db.scalars(q.order_by(EaChange.created_at.desc()).limit(limit)))


async def count_open(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count()).select_from(EaChange).where(EaChange.status.in_(REVIEWABLE))
        )
        or 0
    )


def _log(
    db: AsyncSession, action: str, entity: str, target: str, actor: str | None, **detail: Any
) -> None:
    db.add(
        EaEditLog(action=action, entity=entity, target_id=target[:300], actor=actor, detail=detail)
    )


async def _open_change(db: AsyncSession, change_id: uuid.UUID) -> EaChange:
    change = await db.get(EaChange, change_id)
    if change is None:
        raise EaEditError("Proposta non trovata", 404)
    if change.status not in REVIEWABLE:
        raise EaEditError("La proposta è già stata decisa", 409)
    return change


async def _approve_node_add(
    db: AsyncSession, change: EaChange, overrides: dict[str, Any], actor: str | None, now: datetime
) -> tuple[str, str]:
    after = dict(change.after or {})
    node_id = change.target_id
    if await db.get(EaNode, node_id) is not None:
        raise EaEditError("Il componente esiste già nella mappa", 409)
    parent_id = overrides.get("parent_id") or after.get("parent_id")
    if not parent_id:
        raise EaEditError("Scegli il prodotto a cui appartiene il nuovo componente")
    parent = await db.get(EaNode, parent_id)
    if parent is None or parent.kind != "product":
        raise EaEditError("Il genitore di un componente deve essere un prodotto")
    ctype = overrides.get("ctype") or after.get("ctype") or "be"
    if ctype not in _CTYPES:
        raise EaEditError(f"Tipo di componente non valido: {ctype}")
    name = (overrides.get("name") or after.get("name") or node_id).strip()
    attrs = {
        **(after.get("attrs") or {}),
        "ctype": ctype,
        "repo": f"https://github.com/pagopa/{node_id}",
        "private": bool(after.get("private")),
    }
    db.add(
        EaNode(
            id=node_id,
            kind="component",
            parent_id=parent_id,
            name=name,
            description=after.get("description"),
            attrs=attrs,
            source="sync",
            status="active",
            verified_by=actor,
            verified_at=now,
            sort_order=10_000,
        )
    )
    await db.flush()
    qa = after.get("qa") or {}
    db.add(
        EaQaSignal(
            node_id=node_id,
            **{k: qa[k] for k in _QA_FIELDS if k in qa},
            observed_at=now,
        )
    )
    for i, url in enumerate(after.get("readme_links") or []):
        db.add(
            EaDocLink(
                node_id=node_id,
                url=url,
                title=url.removeprefix("https://pagopa.atlassian.net/wiki/")[:500],
                pages=1,
                source="readme",
                sort_order=i,
            )
        )
    return "node", node_id


async def _edge_from_target(db: AsyncSession, target: str) -> tuple[str, str, str, EaEdge | None]:
    from_id, rest = target.split(">", 1)
    to_id, edge_type = rest.rsplit(":", 1)
    edge = await db.scalar(
        select(EaEdge).where(
            EaEdge.from_id == from_id, EaEdge.to_id == to_id, EaEdge.type == edge_type
        )
    )
    return from_id, to_id, edge_type, edge


async def _apply(
    db: AsyncSession, change: EaChange, overrides: dict[str, Any], actor: str | None, now: datetime
) -> tuple[str, str]:
    """Make the proposal real. Returns the (entity, id) to log against."""
    if change.entity == "node" and change.op == "add":
        return await _approve_node_add(db, change, overrides, actor, now)
    if change.entity == "node" and change.op == "update":
        node = await db.get(EaNode, change.target_id)
        if node is None:
            raise EaEditError("Il componente non esiste più", 409)
        node.status = str((change.after or {}).get("status") or "archived")
        node.verified_by, node.verified_at = actor, now
        return "node", node.id
    if change.entity == "edge":
        from_id, to_id, edge_type, edge = await _edge_from_target(db, change.target_id)
        if change.op == "add":
            if await db.get(EaNode, from_id) is None or await db.get(EaNode, to_id) is None:
                raise EaEditError("Uno dei due nodi non esiste più", 409)
            if edge is None:
                edge = EaEdge(from_id=from_id, to_id=to_id, type=edge_type, origin="helm")
                db.add(edge)
            edge.status = "verified"
            edge.confidence = 100
            edge.evidence = (change.after or {}).get("evidence") or {}
            edge.verified_by, edge.verified_at = actor, now
            await db.flush()
            return "edge", str(edge.id)
        if change.op == "remove":
            if edge is None:
                raise EaEditError("Il legame non esiste più", 409)
            edge.status = "removed"
            edge.verified_by, edge.verified_at = actor, now
            return "edge", str(edge.id)
    raise EaEditError(f"Proposta non gestita: {change.entity}/{change.op}")


async def _apply_rejection(
    db: AsyncSession, change: EaChange, actor: str | None, now: datetime
) -> tuple[str, str]:
    """Rejecting keeps a trace that stops the sync from proposing the same thing again."""
    if change.entity == "edge":
        from_id, to_id, edge_type, edge = await _edge_from_target(db, change.target_id)
        if change.op == "add" and edge is None:
            if (
                await db.get(EaNode, from_id) is not None
                and await db.get(EaNode, to_id) is not None
            ):
                edge = EaEdge(
                    from_id=from_id,
                    to_id=to_id,
                    type=edge_type,
                    origin="helm",
                    status="rejected",
                    evidence=(change.after or {}).get("evidence") or {},
                    verified_by=actor,
                    verified_at=now,
                )
                db.add(edge)
                await db.flush()
        elif change.op == "remove" and edge is not None and edge.status == "active":
            # a person says the dependency still exists: it is now a verified fact
            edge.status = "verified"
            edge.confidence = 100
            edge.verified_by, edge.verified_at = actor, now
        return "edge", str(edge.id) if edge is not None else change.target_id
    return "node", change.target_id


async def decide(
    db: AsyncSession,
    change_id: uuid.UUID,
    *,
    approve: bool,
    actor: str | None,
    note: str | None = None,
    overrides: dict[str, Any] | None = None,
) -> EaChange:
    change = await _open_change(db, change_id)
    now = _now()
    if approve:
        entity, target = await _apply(db, change, overrides or {}, actor, now)
    else:
        entity, target = await _apply_rejection(db, change, actor, now)
    change.status = "approved" if approve else "rejected"
    change.reviewed_by, change.reviewed_at = actor, now
    change.review_note = (note or "").strip() or None
    _log(
        db,
        "ea.change.approve" if approve else "ea.change.reject",
        entity,
        target,
        actor,
        change={"entity": change.entity, "op": change.op, "target": change.target_id},
        note=change.review_note,
        overrides=overrides or None,
    )
    await db.commit()
    await db.refresh(change)
    return change


async def decide_many(
    db: AsyncSession, ids: list[uuid.UUID], *, approve: bool, actor: str | None, note: str | None
) -> dict[str, str]:
    """Decide several proposals; each one succeeds or fails on its own."""
    results: dict[str, str] = {}
    for change_id in ids:
        try:
            change = await decide(db, change_id, approve=approve, actor=actor, note=note)
            results[str(change_id)] = change.status
        except EaEditError as exc:
            await db.rollback()
            results[str(change_id)] = f"error: {exc}"
    return results
