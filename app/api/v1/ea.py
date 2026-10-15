from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import APIRouter, HTTPException, Query, status

from app.deps import DbDep
from app.models.ea import EaEdge, EaNode
from app.schemas.ea import (
    EaEdgeCreate,
    EaEdgeNote,
    EaEdgeOut,
    EaEdgeUpdate,
    EaHistoryItem,
    EaImpactItem,
    EaImpactResponse,
    EaKbResponse,
    EaNodeCreate,
    EaNodeDetail,
    EaNodeOut,
    EaNodeUpdate,
    EaQaOut,
    EaQaUpdate,
)
from app.services.ea import edit as edit_svc
from app.services.ea import impact as impact_svc
from app.services.ea import kb as kb_svc
from app.services.ea.edit import EaEditError

router = APIRouter()


@router.get("/kb", response_model=EaKbResponse)
async def get_kb(db: DbDep) -> EaKbResponse:
    return EaKbResponse(**await kb_svc.build_kb(db))


@router.get("/nodes/{node_id}", response_model=EaNodeDetail)
async def get_node(node_id: str, db: DbDep) -> EaNodeDetail:
    detail = await kb_svc.get_node_detail(db, node_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EA node not found")
    detail["in_"] = detail.pop("in")
    return EaNodeDetail(**detail)


@router.get("/nodes/{node_id}/impact", response_model=EaImpactResponse)
async def get_impact(
    node_id: str, db: DbDep, depth: int = Query(default=3, ge=1, le=6)
) -> EaImpactResponse:
    if await db.get(EaNode, node_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EA node not found")
    result = await impact_svc.impact(db, node_id, depth)
    return EaImpactResponse(
        node_id=node_id,
        depth=depth,
        upstream=[EaImpactItem(**i) for i in result["upstream"]],
        downstream=[EaImpactItem(**i) for i in result["downstream"]],
    )


def _raise(exc: EaEditError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def _edge_out(edge: EaEdge) -> EaEdgeOut:
    return EaEdgeOut(**edit_svc.edge_payload(edge))


def _node_out(node: EaNode) -> EaNodeOut:
    return EaNodeOut(
        id=node.id,
        kind=node.kind,
        parent_id=node.parent_id,
        name=node.name,
        description=node.description,
        source=node.source,
        verified_by=node.verified_by,
        verified_at=node.verified_at,
    )


@router.post("/edges", response_model=EaEdgeOut, status_code=status.HTTP_201_CREATED)
async def create_edge(body: EaEdgeCreate, db: DbDep) -> EaEdgeOut:
    try:
        edge = await edit_svc.create_edge(
            db,
            from_id=body.from_id,
            to_id=body.to_id,
            edge_type=body.type,
            label=body.label,
            note=body.note,
            actor=body.actor,
        )
    except EaEditError as exc:
        _raise(exc)
    return _edge_out(edge)


@router.patch("/edges/{edge_id}", response_model=EaEdgeOut)
async def update_edge(edge_id: uuid.UUID, body: EaEdgeUpdate, db: DbDep) -> EaEdgeOut:
    changes = body.model_dump(include=body.model_fields_set - {"actor"})
    try:
        edge = await edit_svc.update_edge(db, edge_id, changes=changes, actor=body.actor)
    except EaEditError as exc:
        _raise(exc)
    return _edge_out(edge)


@router.post("/edges/{edge_id}/verify", response_model=EaEdgeOut)
async def verify_edge(edge_id: uuid.UUID, body: EaEdgeNote, db: DbDep) -> EaEdgeOut:
    try:
        edge = await edit_svc.verify_edge(db, edge_id, note=body.note, actor=body.actor)
    except EaEditError as exc:
        _raise(exc)
    return _edge_out(edge)


@router.post("/edges/{edge_id}/remove", response_model=EaEdgeOut)
async def remove_edge(edge_id: uuid.UUID, body: EaEdgeNote, db: DbDep) -> EaEdgeOut:
    try:
        edge = await edit_svc.remove_edge(db, edge_id, note=body.note, actor=body.actor)
    except EaEditError as exc:
        _raise(exc)
    return _edge_out(edge)


@router.post("/nodes", response_model=EaNodeOut, status_code=status.HTTP_201_CREATED)
async def create_node(body: EaNodeCreate, db: DbDep) -> EaNodeOut:
    attrs: dict[str, object] = {}
    if body.kind == "component":
        attrs = {
            "ctype": body.ctype or "be",
            "repo": body.repo,
            "private": False,
            "lang": [],
            "fw": [],
            "owners": [],
            "apis": [],
            "workflows": [],
            "readme": "",
        }
    try:
        node = await edit_svc.create_node(
            db,
            node_id=body.id,
            kind=body.kind,
            name=body.name,
            description=body.description,
            parent_id=body.parent_id,
            attrs=attrs,
            actor=body.actor,
        )
    except EaEditError as exc:
        _raise(exc)
    return _node_out(node)


@router.patch("/nodes/{node_id}", response_model=EaNodeOut)
async def update_node(node_id: str, body: EaNodeUpdate, db: DbDep) -> EaNodeOut:
    changes = body.model_dump(include=body.model_fields_set - {"actor"})
    try:
        node = await edit_svc.update_node(db, node_id, changes=changes, actor=body.actor)
    except EaEditError as exc:
        _raise(exc)
    return _node_out(node)


@router.patch("/nodes/{node_id}/qa", response_model=EaQaOut)
async def update_qa(node_id: str, body: EaQaUpdate, db: DbDep) -> EaQaOut:
    changes = body.model_dump(include=body.model_fields_set - {"actor"})
    try:
        sig = await edit_svc.update_qa(db, node_id, changes=changes, actor=body.actor)
    except EaEditError as exc:
        _raise(exc)
    return EaQaOut(
        node_id=sig.node_id,
        under_qa=sig.under_qa,
        tas_ci_override=sig.tas_ci_override,
        qa_owner=sig.qa_owner,
        qa_notes=sig.qa_notes,
        manual_updated_by=sig.manual_updated_by,
        manual_updated_at=sig.manual_updated_at,
    )


@router.get("/nodes/{node_id}/history", response_model=list[EaHistoryItem])
async def node_history(node_id: str, db: DbDep) -> list[EaHistoryItem]:
    if await db.get(EaNode, node_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EA node not found")
    return [EaHistoryItem(**h) for h in await edit_svc.node_history(db, node_id)]
