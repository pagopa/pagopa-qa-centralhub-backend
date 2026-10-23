from __future__ import annotations

import uuid
from time import monotonic
from typing import NoReturn

import structlog
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, status
from sqlalchemy import select

from app.config import settings
from app.deps import DbDep
from app.models.ea import EaEdge, EaNode, EaSyncRun, EaSyncStatus
from app.schemas.ea import (
    EaAssistantOut,
    EaAssistantRequest,
    EaChangeBulk,
    EaChangeDecision,
    EaChangeOut,
    EaEdgeCreate,
    EaEdgeNote,
    EaEdgeOut,
    EaEdgeUpdate,
    EaFlowOut,
    EaHistoryItem,
    EaImpactItem,
    EaImpactResponse,
    EaInfraResourceDetail,
    EaInfraViewOut,
    EaKbResponse,
    EaLiveOut,
    EaNodeCreate,
    EaNodeDetail,
    EaNodeOut,
    EaNodeUpdate,
    EaQaOut,
    EaQaUpdate,
    EaReleaseImpactOut,
    EaReleaseImpactRequest,
    EaSyncRequest,
    EaSyncRunOut,
    EaSyncStatusOut,
)
from app.services.ea import assistant as assistant_svc
from app.services.ea import edit as edit_svc
from app.services.ea import flows as flows_svc
from app.services.ea import impact as impact_svc
from app.services.ea import kb as kb_svc
from app.services.ea import live as live_svc
from app.services.ea import release_impact as release_svc
from app.services.ea.edit import EaEditError
from app.services.ea.graph_index import GraphIndex
from app.services.ea.infra import view as infra_view_svc
from app.services.ea.sync import engine as sync_engine
from app.services.ea.sync import review as review_svc
from app.services.ea.sync.sources import GitHubClient
from app.services.ea.sync.tracing import error_fields

router = APIRouter()
logger = structlog.get_logger(__name__)


@router.get("/kb", response_model=EaKbResponse)
async def get_kb(db: DbDep) -> EaKbResponse:
    return EaKbResponse(**await kb_svc.build_kb(db))


@router.get("/nodes/{node_id}", response_model=EaNodeDetail)
async def get_node(node_id: str, db: DbDep) -> EaNodeDetail:
    detail = await kb_svc.get_node_detail(db, node_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="EA node not found")
    detail["in_"] = detail.pop("in")
    detail["infra"] = await infra_view_svc.resources_of_node(db, node_id)
    return EaNodeDetail(**detail)


@router.get("/infra", response_model=EaInfraViewOut, response_model_by_alias=True)
async def get_infra(
    db: DbDep, env: str = Query(default="prod", pattern="^(prod|uat|dev)$")
) -> EaInfraViewOut:
    """Infrastructure of one environment, from the Terraform of the platform."""
    return EaInfraViewOut(**await infra_view_svc.infra_view(db, env))


@router.get(
    "/infra/resources/{resource_id:path}",
    response_model=EaInfraResourceDetail,
    response_model_by_alias=True,
)
async def get_infra_resource(resource_id: str, db: DbDep) -> EaInfraResourceDetail:
    detail = await infra_view_svc.resource_detail(db, resource_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    return EaInfraResourceDetail(**detail)


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


@router.get("/live", response_model=EaLiveOut)
async def get_live(
    db: DbDep,
    env: str = Query(default="DEV", pattern="^(DEV|UAT|PROD)$"),
    days: int = Query(default=7, ge=1, le=30),
) -> EaLiveOut:
    """Outcomes of the test runs of the last ``days`` in ``env``, on the map nodes."""
    graph = GraphIndex(await kb_svc.build_kb(db))
    return EaLiveOut(**await live_svc.live_status(db, graph, env, days))


@router.get("/flows", response_model=list[EaFlowOut])
async def list_flows(db: DbDep) -> list[EaFlowOut]:
    """Curated payment flows (SANP), step by step, on the nodes of the map."""
    graph = GraphIndex(await kb_svc.build_kb(db))
    return [EaFlowOut(**f) for f in flows_svc.flows_for_map(graph)]


# ── Release impact and map assistant ─────────────────────────────────────────


def _github() -> GitHubClient | None:
    """GitHub client for the analyses, or None when the Hub has no token."""
    return GitHubClient(settings.github_token) if settings.github_token else None


def _chat_model() -> assistant_svc.ChatModel:
    fallbacks = [m.strip() for m in settings.gemini_fallback_models.split(",") if m.strip()]
    return assistant_svc.GeminiModel(settings.gemini_api_key, settings.gemini_model, fallbacks)


@router.post("/impact/release", response_model=EaReleaseImpactOut)
async def release_impact(body: EaReleaseImpactRequest, db: DbDep) -> EaReleaseImpactOut:
    try:
        req = release_svc.parse_request(body.url, body.repo, body.base, body.head)
    except release_svc.ImpactError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    gh = _github()
    if gh is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GITHUB_TOKEN non configurato: non posso leggere la pull request",
        )
    try:
        graph = GraphIndex(await kb_svc.build_kb(db))
        result = await release_svc.release_impact(graph, release_svc.GitHubChanges(gh), req)
    except release_svc.ImpactError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    finally:
        await gh.aclose()
    logger.info(
        "ea_release_impact", repo=req.repo, risk=result["risk"], impacted=len(result["impacted"])
    )
    return EaReleaseImpactOut(**result)


@router.post("/assistant/chat", response_model=EaAssistantOut)
async def assistant_chat(body: EaAssistantRequest, db: DbDep) -> EaAssistantOut:
    try:
        model = _chat_model()
    except assistant_svc.AssistantUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    gh = _github()
    started = monotonic()
    try:
        graph = GraphIndex(await kb_svc.build_kb(db))
        result = await assistant_svc.chat(
            graph,
            model,
            [m.model_dump() for m in body.messages],
            changes=release_svc.GitHubChanges(gh) if gh else None,
            context=body.context,
        )
    except assistant_svc.AssistantUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    finally:
        if gh is not None:
            await gh.aclose()
    logger.info(
        "ea_assistant_chat",
        steps=[s["tool"] for s in result["steps"]],
        highlight=len(result["highlight"]),
        duration_ms=round((monotonic() - started) * 1000),
    )
    return EaAssistantOut(**result)


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


# ---- sync and review queue -------------------------------------------------------------


def _run_out(run: EaSyncRun | None) -> EaSyncRunOut | None:
    if run is None:
        return None
    return EaSyncRunOut(
        id=str(run.id),
        trigger=run.trigger,
        triggered_by=run.triggered_by,
        started_at=run.started_at,
        finished_at=run.finished_at,
        status=run.status,
        stats=run.stats or {},
        error=run.error,
    )


def _dispatch(run_id: str, background: BackgroundTasks) -> None:
    """Hand the run to the Celery worker; without a broker (local dev) run it in-process."""
    started = monotonic()
    log = logger.bind(run_id=run_id)
    log.info("ea_sync_dispatch_started")
    try:
        from app.tasks.sync_ea import sync_ea

        task = sync_ea.apply_async(args=[run_id], retry=False)
        log.info(
            "ea_sync_dispatched",
            task_id=task.id,
            duration_ms=round((monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - broker down: still let the user sync
        log.warning(
            "ea_sync_dispatch_inprocess",
            duration_ms=round((monotonic() - started) * 1000),
            **error_fields(exc),
        )
        from app.services.ea.sync.runner import run_with_default_sources

        background.add_task(run_with_default_sources, uuid.UUID(run_id))


@router.post("/sync", response_model=EaSyncRunOut, status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(body: EaSyncRequest, db: DbDep, background: BackgroundTasks) -> EaSyncRunOut:
    try:
        run = await sync_engine.create_run(db, trigger="manual", actor=body.actor)
    except sync_engine.EaSyncBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    logger.info("ea_sync_run_created", run_id=str(run.id), trigger="manual")
    _dispatch(str(run.id), background)
    out = _run_out(run)
    assert out is not None  # noqa: S101
    return out


@router.get("/sync-status", response_model=EaSyncStatusOut)
async def get_sync_status(db: DbDep) -> EaSyncStatusOut:
    sync_status = await db.get(EaSyncStatus, 1)
    last = await db.scalar(
        select(EaSyncRun)
        .where(EaSyncRun.status != "running", EaSyncRun.trigger != "import")
        .order_by(EaSyncRun.started_at.desc())
        .limit(1)
    )
    return EaSyncStatusOut(
        running=_run_out(await sync_engine.running_run(db)),
        last=_run_out(last),
        last_success_at=sync_status.last_success_at if sync_status else None,
        last_error=sync_status.last_error if sync_status else None,
        open_changes=await review_svc.count_open(db),
    )


@router.get("/sync-runs", response_model=list[EaSyncRunOut])
async def list_sync_runs(db: DbDep, limit: int = Query(10, ge=1, le=100)) -> list[EaSyncRunOut]:
    runs = await db.scalars(select(EaSyncRun).order_by(EaSyncRun.started_at.desc()).limit(limit))
    return [r for r in (_run_out(run) for run in runs) if r is not None]


_CHANGE_STATUSES = {"pending", "conflict", "approved", "rejected", "auto_applied", "superseded"}


@router.get("/changes", response_model=list[EaChangeOut])
async def list_changes(
    db: DbDep,
    status_filter: str = Query("pending,conflict", alias="status"),
    run_id: uuid.UUID | None = None,
    limit: int = Query(500, ge=1, le=2000),
) -> list[EaChangeOut]:
    statuses = tuple(s for s in status_filter.split(",") if s in _CHANGE_STATUSES)
    if not statuses:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Stato non valido"
        )
    changes = await review_svc.list_changes(db, statuses=statuses, run_id=run_id, limit=limit)
    return [EaChangeOut(**review_svc.change_payload(c)) for c in changes]


async def _decide(
    change_id: uuid.UUID, body: EaChangeDecision, db: DbDep, *, approve: bool
) -> EaChangeOut:
    overrides = body.model_dump(include={"parent_id", "ctype", "name"}, exclude_none=True)
    try:
        change = await review_svc.decide(
            db, change_id, approve=approve, actor=body.actor, note=body.note, overrides=overrides
        )
    except EaEditError as exc:
        await db.rollback()
        _raise(exc)
    return EaChangeOut(**review_svc.change_payload(change))


@router.post("/changes/{change_id}/approve", response_model=EaChangeOut)
async def approve_change(change_id: uuid.UUID, body: EaChangeDecision, db: DbDep) -> EaChangeOut:
    return await _decide(change_id, body, db, approve=True)


@router.post("/changes/{change_id}/reject", response_model=EaChangeOut)
async def reject_change(change_id: uuid.UUID, body: EaChangeDecision, db: DbDep) -> EaChangeOut:
    return await _decide(change_id, body, db, approve=False)


@router.post("/changes/bulk", response_model=dict[str, str])
async def bulk_decide(body: EaChangeBulk, db: DbDep) -> dict[str, str]:
    try:
        ids = [uuid.UUID(i) for i in body.ids]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Id non valido"
        ) from exc
    return await review_svc.decide_many(
        db, ids, approve=body.decision == "approve", actor=body.actor, note=body.note
    )
