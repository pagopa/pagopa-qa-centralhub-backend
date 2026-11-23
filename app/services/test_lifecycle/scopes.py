from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import TlFeature, TlFeatureStatus, TlScope
from app.schemas.test_lifecycle import TlScopeCreate, TlScopeOut, TlScopeUpdate
from app.services.test_lifecycle import jira_tasks, nrt


class ScopeNotFoundError(Exception):
    pass


class ScopeCodeConflictError(Exception):
    pass


class ScopeCodeLockedError(Exception):
    pass


def _empty_counts() -> dict[str, int]:
    return {s.value: 0 for s in TlFeatureStatus}


async def _counts(
    db: AsyncSession, scope_ids: list[uuid.UUID] | None = None
) -> dict[uuid.UUID, dict[str, int]]:
    stmt = select(TlFeature.scope_id, TlFeature.status, func.count()).group_by(
        TlFeature.scope_id, TlFeature.status
    )
    if scope_ids is not None:
        stmt = stmt.where(TlFeature.scope_id.in_(scope_ids))
    out: dict[uuid.UUID, dict[str, int]] = {}
    for scope_id, status, n in (await db.execute(stmt)).all():
        out.setdefault(scope_id, _empty_counts())[TlFeatureStatus(status).value] = n
    return out


def to_out(scope: TlScope, counts: dict[str, int] | None = None) -> TlScopeOut:
    counts = counts or _empty_counts()
    out = TlScopeOut.model_validate(scope)
    out.status_counts = counts
    out.feature_count = sum(counts.values())
    return out


async def list_scopes(db: AsyncSession) -> list[TlScopeOut]:
    scopes = list((await db.execute(select(TlScope).order_by(func.lower(TlScope.code)))).scalars())
    counts = await _counts(db)
    rates = await nrt.scope_pass_rates(db)
    outs = [to_out(s, counts.get(s.id)) for s in scopes]
    for out in outs:
        out.nrt_pass_rate = rates.get(out.id)
    return outs


async def get_scope_out(db: AsyncSession, scope: TlScope) -> TlScopeOut:
    return to_out(scope, (await _counts(db, [scope.id])).get(scope.id))


async def _code_taken(db: AsyncSession, code: str, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(TlScope.id).where(func.lower(TlScope.code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(TlScope.id != exclude_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def create_scope(db: AsyncSession, data: TlScopeCreate) -> TlScope:
    # Epic QA verificata su Jira prima di aprire la transazione
    await jira_tasks.verify_keys({"epic": data.jira_qa_epic_key}, epics={"epic"})
    if await _code_taken(db, data.code):
        raise ScopeCodeConflictError(data.code)
    scope = TlScope(**data.model_dump())
    db.add(scope)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ScopeCodeConflictError(data.code) from exc
    await db.refresh(scope)
    return scope


async def update_scope(db: AsyncSession, scope_id: uuid.UUID, data: TlScopeUpdate) -> TlScope:
    if data.jira_qa_epic_key:
        await jira_tasks.verify_keys({"epic": data.jira_qa_epic_key}, epics={"epic"})
    # Lock della riga: create_feature blocca lo stesso ambito, quindi il controllo
    # "nessuna feature" resta valido fino al commit.
    scope = (
        await db.execute(
            select(TlScope)
            .where(TlScope.id == scope_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if scope is None:
        raise ScopeNotFoundError(str(scope_id))
    changes = data.model_dump(exclude_unset=True)
    new_code = changes.get("code")
    if new_code is not None and new_code != scope.code:
        has_features = (
            await db.execute(select(TlFeature.id).where(TlFeature.scope_id == scope_id).limit(1))
        ).first() is not None
        if has_features:
            raise ScopeCodeLockedError(str(scope_id))
        if await _code_taken(db, new_code, exclude_id=scope_id):
            raise ScopeCodeConflictError(new_code)
    elif "code" in changes:
        changes.pop("code")
    for key, value in changes.items():
        setattr(scope, key, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ScopeCodeConflictError(str(new_code)) from exc
    await db.refresh(scope)
    return scope
