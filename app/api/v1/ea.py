from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, status

from app.deps import DbDep
from app.models.ea import EaNode
from app.schemas.ea import EaImpactItem, EaImpactResponse, EaKbResponse, EaNodeDetail
from app.services.ea import impact as impact_svc
from app.services.ea import kb as kb_svc

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
