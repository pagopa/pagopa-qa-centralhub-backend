from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.models.test_lifecycle import TlFeatureStatus
from app.schemas.test_lifecycle import (
    TlFeatureCreate,
    TlFeatureDetail,
    TlFeatureListItem,
    TlFeatureUpdate,
    TlRevisionOut,
    TlRevisionUpdate,
    TlScenarioCreate,
    TlScenarioOut,
    TlScenarioUpdate,
    TlScopeCreate,
    TlScopeOut,
    TlScopeUpdate,
)
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import scopes as scopes_svc
from app.services.test_lifecycle.actors import UnknownActorError

router = APIRouter()

# Nessun endpoint di cancellazione per gli ambiti: le FK delle feature sono ON DELETE CASCADE.


@router.get("/scopes", response_model=list[TlScopeOut])
async def list_scopes(db: DbDep) -> list[TlScopeOut]:
    return await scopes_svc.list_scopes(db)


@router.post("/scopes", response_model=TlScopeOut, status_code=status.HTTP_201_CREATED)
async def create_scope(body: TlScopeCreate, db: DbDep) -> TlScopeOut:
    try:
        scope = await scopes_svc.create_scope(db, body)
    except scopes_svc.ScopeCodeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scope_code_conflict",
                "message": "Esiste già un ambito con questo codice",
            },
        ) from exc
    return await scopes_svc.get_scope_out(db, scope)


@router.patch("/scopes/{scope_id}", response_model=TlScopeOut)
async def update_scope(scope_id: uuid.UUID, body: TlScopeUpdate, db: DbDep) -> TlScopeOut:
    try:
        scope = await scopes_svc.update_scope(db, scope_id, body)
    except scopes_svc.ScopeNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Ambito non trovato") from exc
    except scopes_svc.ScopeCodeLockedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scope_code_locked",
                "message": "Il codice dell'ambito non può cambiare: esistono già feature",
            },
        ) from exc
    except scopes_svc.ScopeCodeConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "scope_code_conflict",
                "message": "Esiste già un ambito con questo codice",
            },
        ) from exc
    return await scopes_svc.get_scope_out(db, scope)


# ── Feature, revisioni, scenari ───────────────────────────────────────────────

def _translate(exc: Exception) -> HTTPException:
    """Traduce le eccezioni di dominio del servizio feature in errori HTTP."""
    if isinstance(exc, UnknownActorError):
        return HTTPException(
            status_code=403,
            detail={"code": "unknown_actor", "message": "Utente non riconosciuto o non attivo"},
        )
    if isinstance(exc, features_svc.FeatureNotFoundError):
        return HTTPException(status_code=404, detail="Feature non trovata")
    if isinstance(exc, features_svc.RevisionNotFoundError):
        return HTTPException(status_code=404, detail="Revisione non trovata")
    if isinstance(exc, features_svc.ScenarioNotFoundError):
        return HTTPException(status_code=404, detail="Scenario non trovato")
    if isinstance(exc, scopes_svc.ScopeNotFoundError):
        return HTTPException(status_code=404, detail="Ambito non trovato")
    if isinstance(exc, features_svc.UnknownUserError):
        return HTTPException(
            status_code=422,
            detail={"code": "unknown_user", "message": "Utente indicato non esistente"},
        )
    if isinstance(exc, features_svc.StaleLockError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "stale_lock",
                "message": "La revisione è stata modificata da qualcun altro: ricarica e riprova",
            },
        )
    if isinstance(exc, features_svc.RevisionLockedError):
        return HTTPException(
            status_code=409,
            detail={
                "code": "revision_locked",
                "message": f"La revisione non è modificabile nello stato {exc.status.value}",
            },
        )
    raise exc


@router.get("/features", response_model=list[TlFeatureListItem])
async def list_features(
    db: DbDep,
    scope_id: uuid.UUID | None = None,
    status: TlFeatureStatus | None = None,
    owner_id: uuid.UUID | None = None,
) -> list[TlFeatureListItem]:
    return await features_svc.list_features(db, scope_id, status, owner_id)


@router.post("/features", response_model=TlFeatureDetail, status_code=status.HTTP_201_CREATED)
async def create_feature(body: TlFeatureCreate, db: DbDep) -> TlFeatureDetail:
    try:
        feature_id = await features_svc.create_feature(db, body)
        return await features_svc.get_feature_detail(db, feature_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/features/{feature_id}", response_model=TlFeatureDetail)
async def get_feature(
    feature_id: uuid.UUID, db: DbDep, actor_email: str | None = None
) -> TlFeatureDetail:
    try:
        return await features_svc.get_feature_detail(db, feature_id, actor_email)
    except Exception as exc:
        raise _translate(exc) from exc


@router.patch("/features/{feature_id}", response_model=TlFeatureDetail)
async def update_feature(
    feature_id: uuid.UUID, body: TlFeatureUpdate, db: DbDep
) -> TlFeatureDetail:
    try:
        return await features_svc.update_feature(db, feature_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.patch("/features/{feature_id}/revisions/{revision_id}", response_model=TlRevisionOut)
async def update_revision(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlRevisionUpdate, db: DbDep
) -> TlRevisionOut:
    try:
        return await features_svc.update_revision(db, feature_id, revision_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post(
    "/features/{feature_id}/revisions/{revision_id}/scenarios",
    response_model=TlScenarioOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_scenario(
    feature_id: uuid.UUID, revision_id: uuid.UUID, body: TlScenarioCreate, db: DbDep
) -> TlScenarioOut:
    try:
        return await features_svc.add_scenario(db, feature_id, revision_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.patch(
    "/features/{feature_id}/revisions/{revision_id}/scenarios/{scenario_id}",
    response_model=TlScenarioOut,
)
async def update_scenario(
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    scenario_id: uuid.UUID,
    body: TlScenarioUpdate,
    db: DbDep,
) -> TlScenarioOut:
    try:
        return await features_svc.update_scenario(db, feature_id, revision_id, scenario_id, body)
    except Exception as exc:
        raise _translate(exc) from exc


@router.delete(
    "/features/{feature_id}/revisions/{revision_id}/scenarios/{scenario_id}",
    response_model=TlRevisionOut,
)
async def remove_scenario(
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    scenario_id: uuid.UUID,
    actor_email: str,
    db: DbDep,
) -> TlRevisionOut:
    """Toglie lo scenario dalla revisione; il numero resta consumato. Ritorna la revisione."""
    try:
        return await features_svc.remove_scenario(
            db, feature_id, revision_id, scenario_id, actor_email
        )
    except Exception as exc:
        raise _translate(exc) from exc
