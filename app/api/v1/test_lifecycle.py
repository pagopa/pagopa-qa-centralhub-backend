from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.test_lifecycle import TlScopeCreate, TlScopeOut, TlScopeUpdate
from app.services.test_lifecycle import scopes as scopes_svc

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
