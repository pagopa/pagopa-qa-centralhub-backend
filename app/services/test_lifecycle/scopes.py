from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import TlFeature, TlFeatureStatus, TlScope
from app.schemas.test_lifecycle import TlScopeCreate, TlScopeOut, TlScopeUpdate
from app.services.test_lifecycle import history, jira_tasks, nrt
from app.services.test_lifecycle.advisory_lock import try_advisory_lock
from app.services.test_lifecycle.alias_guard import code_held_by_aliases
from app.services.test_lifecycle.deletable import undeletable_clause


class ScopeNotFoundError(Exception):
    pass


class ScopeCodeConflictError(Exception):
    pass


class ScopeCodeBlockedError(Exception):
    """The new code cannot be applied: some features cannot be adapted. `blocked` lists them."""

    def __init__(self, blocked: list[dict]) -> None:
        super().__init__(", ".join(b["code"] for b in blocked))
        self.blocked = blocked


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


async def _blocked_scope_ids(
    db: AsyncSession, scope_ids: list[uuid.UUID] | None = None
) -> set[uuid.UUID]:
    """Scopes holding at least one feature that cannot be deleted."""
    stmt = select(TlFeature.scope_id).where(undeletable_clause()).distinct()
    if scope_ids is not None:
        stmt = stmt.where(TlFeature.scope_id.in_(scope_ids))
    return set((await db.execute(stmt)).scalars())


def to_out(
    scope: TlScope, counts: dict[str, int] | None = None, deletable: bool = True
) -> TlScopeOut:
    counts = counts or _empty_counts()
    out = TlScopeOut.model_validate(scope)
    out.status_counts = counts
    out.feature_count = sum(counts.values())
    out.deletable = deletable
    return out


async def list_scopes(db: AsyncSession) -> list[TlScopeOut]:
    scopes = list((await db.execute(select(TlScope).order_by(func.lower(TlScope.code)))).scalars())
    counts = await _counts(db)
    rates = await nrt.scope_pass_rates(db)
    blocked = await _blocked_scope_ids(db)
    outs = [to_out(s, counts.get(s.id), s.id not in blocked) for s in scopes]
    for out in outs:
        out.nrt_pass_rate = rates.get(out.id)
    return outs


async def get_scope_out(db: AsyncSession, scope: TlScope) -> TlScopeOut:
    blocked = await _blocked_scope_ids(db, [scope.id])
    return to_out(scope, (await _counts(db, [scope.id])).get(scope.id), scope.id not in blocked)


async def _code_taken(db: AsyncSession, code: str, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(TlScope.id).where(func.lower(TlScope.code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(TlScope.id != exclude_id)
    return (await db.execute(stmt.limit(1))).first() is not None


async def create_scope(db: AsyncSession, data: TlScopeCreate) -> TlScope:
    # Epic QA verificata su Jira prima di aprire la transazione
    await jira_tasks.verify_keys({"epic": data.jira_qa_epic_key}, epics={"epic"})
    if await _code_taken(db, data.code) or await code_held_by_aliases(db, data.code):
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
    renamed = new_code is not None and new_code != scope.code
    if not renamed:
        changes.pop("code", None)
    elif await _code_taken(db, new_code, exclude_id=scope_id) or await code_held_by_aliases(
        db, new_code, own_scope_id=scope_id
    ):
        raise ScopeCodeConflictError(new_code)
    if not renamed:
        for key, value in changes.items():
            setattr(scope, key, value)
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise ScopeCodeConflictError(str(new_code)) from exc
        await db.refresh(scope)
        return scope
    await _rename_code(db, scope, changes)
    await db.refresh(scope)
    return scope


async def _rename_code(db: AsyncSession, scope: TlScope, changes: dict) -> None:
    """Applies `changes` (new code included): every feature takes the identifiers of the new code.

    All or nothing: a feature that cannot be adapted (its identifiers are already on the
    repository) refuses the rename with the list of the blocking features. Not while a
    migration runs.
    """
    from app.services.test_lifecycle import rekey
    from app.services.test_lifecycle.migration import LOCK_KEY

    old_code = scope.code
    async with try_advisory_lock(db, LOCK_KEY) as got:
        if not got:
            await db.rollback()
            raise rekey.MigrationRunningError
        features = list(
            (
                await db.execute(
                    select(TlFeature)
                    .where(TlFeature.scope_id == scope.id)
                    .order_by(TlFeature.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )
        blocked = []
        for f in features:
            reasons = await rekey.adapt_blockers(db, f)
            if reasons:
                blocked.append({"feature_id": f.id, "code": f.code, "reasons": reasons})
        if blocked:
            await db.rollback()
            raise ScopeCodeBlockedError(blocked)
        for key, value in changes.items():
            setattr(scope, key, value)
        for f in features:
            old_feature_code = f.code
            _, new_feature_code = await rekey.adapt_feature(db, f, scope, f.number)
            history.record_event(
                db,
                f.id,
                "tl.feature.moved",
                None,
                {
                    "old_scope": old_code,
                    "new_scope": scope.code,
                    "old_code": old_feature_code,
                    "new_code": new_feature_code,
                    "adapted": True,
                },
            )
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise ScopeCodeConflictError(scope.code) from exc
