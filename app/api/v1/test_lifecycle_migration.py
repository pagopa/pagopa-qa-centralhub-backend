"""API della migrazione da Confluence (fase 5). Tutte richiedono `manage:tl_migration`."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from app.core.permissions import ACTION_CATALOG, compute_role_matrix
from app.deps import DbDep
from app.models.role import Role
from app.models.test_lifecycle import TlFeatureStatus, TlMigrationItem, TlMigrationTag
from app.models.user import User
from app.schemas.test_lifecycle import (
    TlMigrationItemOut,
    TlMigrationScopeOut,
    TlMigrationSummary,
)
from app.services.test_lifecycle import confluence_client as confluence_mod
from app.services.test_lifecycle import git_client as git_client_mod
from app.services.test_lifecycle import migration as migration_svc
from app.services.test_lifecycle import migration_align
from app.services.test_lifecycle.actors import UnknownActorError, resolve_actor

router = APIRouter()
ACTION = "manage:tl_migration"


class TlMigrationActor(BaseModel):
    actor_email: str


class TlMigrationItemPatch(BaseModel):
    actor_email: str
    decision: Literal["pending", "confirmed", "excluded"] | None = None
    overrides: dict[str, Any] | None = None

    @field_validator("overrides")
    @classmethod
    def _v_overrides(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        unknown = set(value) - set(migration_svc.OVERRIDABLE)
        if unknown:
            raise ValueError(f"Campi non correggibili: {', '.join(sorted(unknown))}")
        if "number" in value and value["number"] is not None:
            if not isinstance(value["number"], int) or not 1 <= value["number"] <= 999:
                raise ValueError("Il numero deve essere tra 1 e 999")
        if "status" in value and value["status"] is not None:
            TlFeatureStatus(value["status"])
        if "scope_code" in value and value["scope_code"] is not None:
            code = str(value["scope_code"])
            if not code[:1].isalpha() or not code.isalnum() or not 2 <= len(code) <= 20:
                raise ValueError("Codice ambito: 2-20 caratteri alfanumerici, il primo una lettera")
        for key in ("owner_id", "reviewer_id"):
            if value.get(key):
                uuid.UUID(str(value[key]))
        if "title" in value and not str(value.get("title") or "").strip():
            raise ValueError("Il titolo non può essere vuoto")
        return value


class TlMigrationTagPatch(BaseModel):
    actor_email: str
    decision: Literal["keep", "drop"]


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


async def _require(db: DbDep, actor_email: str) -> User:
    try:
        user = await resolve_actor(db, actor_email)
    except UnknownActorError as exc:
        raise _err(403, "unknown_actor", "Utente non riconosciuto") from exc
    roles = list((await db.execute(select(Role))).scalars())
    matrix = compute_role_matrix(roles)
    allowed = matrix.get(user.role, {}).get(ACTION)
    if allowed is None:  # ruolo senza riga in tabella: default del catalogo
        entry = next(e for e in ACTION_CATALOG if e["key"] == ACTION)
        allowed = entry["defaults"].get(user.role, False)
    if not allowed:
        raise _err(403, "forbidden", "Serve il permesso di migrazione")
    return user


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, migration_svc.MigrationInProgress):
        return _err(409, "migration_in_progress", "Un'altra operazione di migrazione è in corso")
    if isinstance(exc, migration_svc.MigrationBlocked):
        return _err(409, "migration_blocked", "Esecuzione bloccata", reasons=exc.reasons)
    if isinstance(
        exc, (confluence_mod.TlConfluenceNotConfigured, git_client_mod.TlGitNotConfigured)
    ):
        return _err(503, "migration_not_configured", f"Integrazione non configurata: {exc}")
    if isinstance(exc, confluence_mod.TlConfluenceError):
        return _err(502, "confluence_error", exc.message)
    if isinstance(exc, git_client_mod.TlGitError):
        return _err(502, "github_error", exc.message)
    raise exc


@router.get("/migration", response_model=TlMigrationSummary)
async def get_migration(actor_email: str, db: DbDep) -> TlMigrationSummary:
    await _require(db, actor_email)
    return await migration_svc.summary(db)


@router.post("/migration/analyze", response_model=TlMigrationSummary)
async def analyze(body: TlMigrationActor, db: DbDep) -> TlMigrationSummary:
    """Legge pagine FEAT e feature file e ricostruisce le proposte (nessuna scrittura esterna)."""
    await _require(db, body.actor_email)
    try:
        confluence = confluence_mod.get_confluence_client()
        git = git_client_mod.get_git_client()
        return await migration_svc.analyze(db, confluence, git)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/migration/items", response_model=list[TlMigrationItemOut])
async def list_items(
    actor_email: str,
    db: DbDep,
    scope_code: str | None = None,
    decision: str | None = None,
    problem: str | None = None,
) -> list[TlMigrationItemOut]:
    await _require(db, actor_email)
    rows = (await db.execute(select(TlMigrationItem))).scalars()
    out = []
    for row in rows:
        item = migration_svc.item_out(row)
        if scope_code and str(item.effective.get("scope_code") or "").lower() != scope_code.lower():
            continue
        if decision and (("executed" if item.executed_at else item.decision) != decision):
            continue
        if problem and not any(p.code == problem for p in item.problems):
            continue
        out.append(item)
    out.sort(
        key=lambda i: (
            str(i.effective.get("scope_code") or "").lower(),
            i.effective.get("number") or 0,
            i.title,
        )
    )
    return out


@router.patch("/migration/items/{item_id}", response_model=TlMigrationItemOut)
async def patch_item(
    item_id: uuid.UUID, body: TlMigrationItemPatch, db: DbDep
) -> TlMigrationItemOut:
    await _require(db, body.actor_email)
    row = await db.get(TlMigrationItem, item_id, with_for_update=True)
    if row is None:
        raise _err(404, "migration_item_not_found", "Riga di migrazione inesistente")
    if row.executed_at is not None:
        raise _err(409, "migration_item_executed", "Riga già eseguita: si modifica la feature")
    if body.overrides is not None:
        row.overrides = {
            k: v for k, v in {**row.overrides, **body.overrides}.items() if v is not None
        }
    if body.decision is not None:
        row.decision = body.decision
    await db.flush()
    await migration_svc.recompute_problems(db)
    await db.commit()
    await db.refresh(row)
    return migration_svc.item_out(row)


@router.patch("/migration/tags/{name}", response_model=TlMigrationSummary)
async def patch_tag(name: str, body: TlMigrationTagPatch, db: DbDep) -> TlMigrationSummary:
    await _require(db, body.actor_email)
    tag = await db.get(TlMigrationTag, name)
    if tag is None:
        raise _err(404, "migration_tag_not_found", "Tag non trovato nell'analisi")
    tag.decision = body.decision
    await db.commit()
    return await migration_svc.summary(db)


@router.post("/migration/scopes/{scope_code}/execute", response_model=TlMigrationScopeOut)
async def execute(scope_code: str, body: TlMigrationActor, db: DbDep) -> TlMigrationScopeOut:
    """Crea le feature confermate dell'ambito, aggiorna Confluence (se abilitato), apre la PR."""
    await _require(db, body.actor_email)
    try:
        confluence = confluence_mod.get_confluence_client()
        git = git_client_mod.get_git_client()
        return await migration_svc.execute_scope(
            db, scope_code, body.actor_email, git=git, confluence=confluence
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/migration/scopes/{scope_code}/retry-pr", response_model=TlMigrationScopeOut)
async def retry_pr(scope_code: str, body: TlMigrationActor, db: DbDep) -> TlMigrationScopeOut:
    await _require(db, body.actor_email)
    try:
        return await migration_align.open_alignment_pr(
            db, scope_code, git_client_mod.get_git_client()
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/migration/scopes/{scope_code}/retry-confluence", response_model=TlMigrationScopeOut)
async def retry_confluence(
    scope_code: str, body: TlMigrationActor, db: DbDep
) -> TlMigrationScopeOut:
    await _require(db, body.actor_email)
    try:
        await migration_svc.write_confluence_panels(
            db, scope_code, confluence_mod.get_confluence_client()
        )
        return await migration_align.scope_out(db, scope_code)
    except Exception as exc:
        raise _translate(exc) from exc
