from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
    TlScenarioKind,
    TlScope,
)
from app.models.user import User
from app.schemas.test_lifecycle import (
    TlFeatureCreate,
    TlFeatureDetail,
    TlFeatureListItem,
    TlFeatureUpdate,
    TlLineError,
    TlRevisionOut,
    TlRevisionUpdate,
    TlScenarioCreate,
    TlScenarioOut,
    TlScenarioUpdate,
)
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.gherkin import (
    feature_code,
    scenario_tag,
    validate_steps,
)
from app.services.test_lifecycle.scopes import ScopeNotFoundError


class FeatureNotFoundError(Exception):
    pass


class RevisionNotFoundError(Exception):
    pass


class ScenarioNotFoundError(Exception):
    pass


class UnknownUserError(Exception):
    """Un id utente (owner/reviewer) non esiste."""


class StaleLockError(Exception):
    pass


class RevisionLockedError(Exception):
    def __init__(self, status: TlRevisionStatus):
        super().__init__(status.value)
        self.status = status


# ── helpers ───────────────────────────────────────────────────────────────────

def _record_event(
    db: AsyncSession, actor: User, action: str, feature: TlFeature, detail: dict[str, Any]
) -> None:
    """Scrive l'evento di storico (sostituibile da history.record_event, Task 5)."""
    db.add(
        AuditLog(
            actor_id=actor.id,
            action=action,
            resource_type="tl_feature",
            resource_id=str(feature.id),
            detail=json.dumps(detail),
        )
    )


async def _ensure_users(db: AsyncSession, ids: list[uuid.UUID | None]) -> None:
    wanted = {i for i in ids if i is not None}
    if not wanted:
        return
    found = set((await db.execute(select(User.id).where(User.id.in_(wanted)))).scalars())
    if wanted - found:
        raise UnknownUserError(", ".join(str(i) for i in wanted - found))


async def _lock_feature(db: AsyncSession, feature_id: uuid.UUID) -> TlFeature:
    feature = (
        await db.execute(
            select(TlFeature)
            .where(TlFeature.id == feature_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if feature is None:
        raise FeatureNotFoundError(str(feature_id))
    return feature


async def _lock_revision(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID
) -> TlFeatureRevision:
    rev = (
        await db.execute(
            select(TlFeatureRevision)
            .where(TlFeatureRevision.id == revision_id, TlFeatureRevision.feature_id == feature_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if rev is None:
        raise RevisionNotFoundError(str(revision_id))
    return rev


def _require_draft(rev: TlFeatureRevision) -> None:
    if rev.status != TlRevisionStatus.DRAFT:
        raise RevisionLockedError(rev.status)


def _require_lock(rev: TlFeatureRevision, lock_version: int) -> None:
    if rev.lock_version != lock_version:
        raise StaleLockError(f"{rev.lock_version} != {lock_version}")


def _scenario_out(
    scenario: TlScenario, content: TlScenarioContent
) -> TlScenarioOut:
    errors = validate_steps(content.steps, kind=content.kind, examples=content.examples)
    return TlScenarioOut(
        id=scenario.id,
        number=scenario.number,
        tag=scenario.tag,
        position=content.position,
        title=content.title,
        kind=content.kind,
        polarity=content.polarity,
        extra_tags=list(content.extra_tags or []),
        steps=content.steps,
        examples=content.examples,
        detail_link=content.detail_link,
        errors=[TlLineError(line=e.line, message=e.message) for e in errors],
    )


async def _revision_out(db: AsyncSession, rev: TlFeatureRevision) -> TlRevisionOut:
    rows = (
        await db.execute(
            select(TlScenario, TlScenarioContent)
            .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
            .where(TlScenarioContent.revision_id == rev.id)
            .order_by(TlScenarioContent.position)
        )
    ).all()
    background_errors: list[TlLineError] = []
    if rev.background_steps and rev.background_steps.strip():
        background_errors = [
            TlLineError(line=e.line, message=e.message)
            for e in validate_steps(
                rev.background_steps, kind=TlScenarioKind.SCENARIO, examples=None, background=True
            )
        ]
    return TlRevisionOut(
        id=rev.id,
        number=rev.number,
        status=rev.status,
        lock_version=rev.lock_version,
        title=rev.title,
        description=rev.description,
        background_steps=rev.background_steps,
        requirements_url=rev.requirements_url,
        product_issue_key=rev.product_issue_key,
        reverse_engineering=rev.reverse_engineering,
        design_estimate_days=rev.design_estimate_days,
        impl_estimate_days=rev.impl_estimate_days,
        created_by=rev.created_by,
        approved_by=rev.approved_by,
        approved_at=rev.approved_at,
        created_at=rev.created_at,
        updated_at=rev.updated_at,
        scenarios=[_scenario_out(s, c) for s, c in rows],
        background_errors=background_errors,
    )


async def _compact_positions(db: AsyncSession, revision_id: uuid.UUID) -> None:
    contents = list(
        (
            await db.execute(
                select(TlScenarioContent)
                .where(TlScenarioContent.revision_id == revision_id)
                .order_by(TlScenarioContent.position)
            )
        ).scalars()
    )
    for i, c in enumerate(contents, start=1):
        if c.position != i:
            c.position = i


# ── feature ───────────────────────────────────────────────────────────────────

async def create_feature(db: AsyncSession, data: TlFeatureCreate) -> uuid.UUID:
    actor = await resolve_actor(db, data.actor_email)
    await _ensure_users(db, [data.design_owner_id])
    scope = (
        await db.execute(
            select(TlScope)
            .where(TlScope.id == data.scope_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if scope is None:
        raise ScopeNotFoundError(str(data.scope_id))
    number = scope.next_feature_number
    scope.next_feature_number = number + 1
    feature = TlFeature(
        scope_id=scope.id,
        number=number,
        code=feature_code(scope.code, number),
        status=TlFeatureStatus.DRAFT,
        design_owner_id=data.design_owner_id,
        created_by=actor.email,
    )
    db.add(feature)
    await db.flush()
    revision = TlFeatureRevision(
        feature_id=feature.id,
        number=1,
        status=TlRevisionStatus.DRAFT,
        title=data.title,
        created_by=actor.email,
    )
    db.add(revision)
    await db.flush()
    feature.working_revision_id = revision.id
    _record_event(
        db, actor, "tl.feature.created", feature, {"code": feature.code, "title": data.title}
    )
    await db.commit()
    return feature.id


async def update_feature(
    db: AsyncSession, feature_id: uuid.UUID, data: TlFeatureUpdate
) -> TlFeatureDetail:
    actor = await resolve_actor(db, data.actor_email)
    changes = data.model_dump(exclude_unset=True, exclude={"actor_email"})
    await _ensure_users(
        db, [changes.get(k) for k in ("design_owner_id", "reviewer_id", "impl_owner_id")]
    )
    feature = await _lock_feature(db, feature_id)
    for key, value in changes.items():
        setattr(feature, key, value)
    if changes:
        _record_event(
            db, actor, "tl.feature.updated", feature,
            {k: (str(v) if v is not None else None) for k, v in changes.items()},
        )
    await db.commit()
    return await get_feature_detail(db, feature_id)


async def get_feature_detail(
    db: AsyncSession, feature_id: uuid.UUID, actor_email: str | None = None
) -> TlFeatureDetail:
    # actor_email: usato dal Task 5 per calcolare `transitions`; per ora ignorato.
    row = (
        await db.execute(
            select(TlFeature, TlScope.code)
            .join(TlScope, TlScope.id == TlFeature.scope_id)
            .where(TlFeature.id == feature_id)
        )
    ).first()
    if row is None:
        raise FeatureNotFoundError(str(feature_id))
    feature, scope_code = row
    working = published = None
    if feature.working_revision_id:
        rev = await db.get(TlFeatureRevision, feature.working_revision_id)
        working = await _revision_out(db, rev) if rev else None
    if feature.published_revision_id:
        rev = await db.get(TlFeatureRevision, feature.published_revision_id)
        published = await _revision_out(db, rev) if rev else None
    return TlFeatureDetail(
        id=feature.id,
        code=feature.code,
        number=feature.number,
        status=feature.status,
        scope_id=feature.scope_id,
        scope_code=scope_code,
        design_owner_id=feature.design_owner_id,
        reviewer_id=feature.reviewer_id,
        impl_owner_id=feature.impl_owner_id,
        jira_design_key=feature.jira_design_key,
        jira_impl_key=feature.jira_impl_key,
        created_by=feature.created_by,
        created_at=feature.created_at,
        updated_at=feature.updated_at,
        working_revision=working,
        published_revision=published,
        transitions=[],
    )


async def list_features(
    db: AsyncSession,
    scope_id: uuid.UUID | None = None,
    status: TlFeatureStatus | None = None,
    owner_id: uuid.UUID | None = None,
) -> list[TlFeatureListItem]:
    stmt = (
        select(TlFeature, TlScope.code)
        .join(TlScope, TlScope.id == TlFeature.scope_id)
        .order_by(func.lower(TlScope.code), TlFeature.number)
    )
    if scope_id is not None:
        stmt = stmt.where(TlFeature.scope_id == scope_id)
    if status is not None:
        stmt = stmt.where(TlFeature.status == status)
    if owner_id is not None:
        stmt = stmt.where(
            or_(
                TlFeature.design_owner_id == owner_id,
                TlFeature.impl_owner_id == owner_id,
                TlFeature.reviewer_id == owner_id,
            )
        )
    rows = (await db.execute(stmt)).all()
    shown = {
        f.id: f.working_revision_id or f.published_revision_id
        for f, _ in rows
        if f.working_revision_id or f.published_revision_id
    }
    rev_ids = list(set(shown.values()))
    titles: dict[uuid.UUID, str] = {}
    counts: dict[uuid.UUID, int] = {}
    if rev_ids:
        titles = dict(
            (
                await db.execute(
                    select(TlFeatureRevision.id, TlFeatureRevision.title).where(
                        TlFeatureRevision.id.in_(rev_ids)
                    )
                )
            ).all()
        )
        counts = dict(
            (
                await db.execute(
                    select(TlScenarioContent.revision_id, func.count())
                    .where(TlScenarioContent.revision_id.in_(rev_ids))
                    .group_by(TlScenarioContent.revision_id)
                )
            ).all()
        )
    out: list[TlFeatureListItem] = []
    for f, scope_code in rows:
        rev_id = shown.get(f.id)
        out.append(
            TlFeatureListItem(
                id=f.id,
                code=f.code,
                number=f.number,
                title=titles.get(rev_id) if rev_id else None,
                status=f.status,
                scope_id=f.scope_id,
                scope_code=scope_code,
                design_owner_id=f.design_owner_id,
                reviewer_id=f.reviewer_id,
                impl_owner_id=f.impl_owner_id,
                scenario_count=counts.get(rev_id, 0) if rev_id else 0,
                updated_at=f.updated_at,
            )
        )
    return out


# ── revisione ─────────────────────────────────────────────────────────────────

async def update_revision(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID, data: TlRevisionUpdate
) -> TlRevisionOut:
    await resolve_actor(db, data.actor_email)
    rev = await _lock_revision(db, feature_id, revision_id)
    _require_draft(rev)
    _require_lock(rev, data.lock_version)
    changes = data.model_dump(exclude_unset=True, exclude={"actor_email", "lock_version"})
    for key, value in changes.items():
        setattr(rev, key, value)
    rev.lock_version += 1
    await db.commit()
    await db.refresh(rev)
    return await _revision_out(db, rev)


# ── scenari ───────────────────────────────────────────────────────────────────

async def add_scenario(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID, data: TlScenarioCreate
) -> TlScenarioOut:
    await resolve_actor(db, data.actor_email)
    feature = await _lock_feature(db, feature_id)
    rev = await _lock_revision(db, feature_id, revision_id)
    _require_draft(rev)
    scope_code = (
        await db.execute(select(TlScope.code).where(TlScope.id == feature.scope_id))
    ).scalar_one()
    number = feature.next_scenario_number
    feature.next_scenario_number = number + 1
    scenario = TlScenario(
        feature_id=feature.id,
        number=number,
        tag=scenario_tag(scope_code, feature.number, number),
    )
    db.add(scenario)
    await db.flush()
    max_pos = (
        await db.execute(
            select(func.coalesce(func.max(TlScenarioContent.position), 0)).where(
                TlScenarioContent.revision_id == rev.id
            )
        )
    ).scalar_one()
    content = TlScenarioContent(
        revision_id=rev.id,
        scenario_id=scenario.id,
        position=max_pos + 1,
        title=data.title,
        kind=data.kind,
        polarity=data.polarity,
        extra_tags=[],
        steps="",
    )
    db.add(content)
    rev.lock_version += 1
    await db.commit()
    return _scenario_out(scenario, content)


async def _load_content(
    db: AsyncSession, revision_id: uuid.UUID, scenario_id: uuid.UUID
) -> tuple[TlScenario, TlScenarioContent]:
    row = (
        await db.execute(
            select(TlScenario, TlScenarioContent)
            .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
            .where(
                TlScenarioContent.revision_id == revision_id,
                TlScenario.id == scenario_id,
            )
        )
    ).first()
    if row is None:
        raise ScenarioNotFoundError(str(scenario_id))
    return row[0], row[1]


async def update_scenario(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    scenario_id: uuid.UUID,
    data: TlScenarioUpdate,
) -> TlScenarioOut:
    await resolve_actor(db, data.actor_email)
    rev = await _lock_revision(db, feature_id, revision_id)
    scenario, content = await _load_content(db, rev.id, scenario_id)
    _require_draft(rev)
    _require_lock(rev, data.lock_version)
    changes = data.model_dump(
        exclude_unset=True, exclude={"actor_email", "lock_version", "position"}
    )
    for key, value in changes.items():
        setattr(content, key, value)
    if data.position is not None:
        contents = list(
            (
                await db.execute(
                    select(TlScenarioContent)
                    .where(TlScenarioContent.revision_id == rev.id)
                    .order_by(TlScenarioContent.position)
                )
            ).scalars()
        )
        contents.remove(content)
        contents.insert(min(data.position, len(contents) + 1) - 1, content)
        for i, c in enumerate(contents, start=1):
            c.position = i
    rev.lock_version += 1
    await db.commit()
    await db.refresh(content)
    return _scenario_out(scenario, content)


async def remove_scenario(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision_id: uuid.UUID,
    scenario_id: uuid.UUID,
    actor_email: str,
) -> TlRevisionOut:
    """Toglie il contenuto dalla revisione; identità e numero restano consumati."""
    await resolve_actor(db, actor_email)
    rev = await _lock_revision(db, feature_id, revision_id)
    _, content = await _load_content(db, rev.id, scenario_id)
    _require_draft(rev)
    await db.delete(content)
    await db.flush()
    await _compact_positions(db, rev.id)
    rev.lock_version += 1
    await db.commit()
    await db.refresh(rev)
    return await _revision_out(db, rev)
