"""Anomalie del modulo: unico punto di scrittura delle righe `tl_issues`."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlIssue,
    TlIssueKind,
    TlIssueStatus,
)
from app.models.user import User
from app.schemas.test_lifecycle import TlIssueOut
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import history
from app.services.test_lifecycle.actors import resolve_actor


class IssueNotFoundError(Exception):
    pass


async def open_issue(
    db: AsyncSession,
    *,
    kind: TlIssueKind,
    feature_id: uuid.UUID | None = None,
    revision_id: uuid.UUID | None = None,
    path: str | None = None,
    commit_sha: str | None = None,
    detail: dict[str, Any],
) -> TlIssue:
    """Apre un'anomalia, riusando (e aggiornando) quella aperta con la stessa identità.

    Identità: `(kind, feature_id, path, detail.get("key"))`, solo tra le anomalie `open`.
    Non esegue il commit: lo fa il chiamante.
    """
    key = detail.get("key")
    candidates = (
        await db.execute(
            select(TlIssue)
            .where(
                TlIssue.kind == kind,
                TlIssue.status == TlIssueStatus.OPEN,
                TlIssue.feature_id.is_(None)
                if feature_id is None
                else TlIssue.feature_id == feature_id,
                TlIssue.path.is_(None) if path is None else TlIssue.path == path,
            )
            .order_by(TlIssue.created_at, TlIssue.id)
        )
    ).scalars()
    for existing in candidates:
        if (existing.detail or {}).get("key") == key:
            existing.detail = detail
            if revision_id is not None:
                existing.revision_id = revision_id
            if commit_sha is not None:
                existing.commit_sha = commit_sha
            return existing
    issue = TlIssue(
        kind=kind,
        status=TlIssueStatus.OPEN,
        feature_id=feature_id,
        revision_id=revision_id,
        path=path,
        commit_sha=commit_sha,
        detail=detail,
    )
    db.add(issue)
    return issue


async def resolve_issues(
    db: AsyncSession,
    *,
    kind: TlIssueKind,
    revision_id: uuid.UUID,
    actor: User | None = None,
) -> list[TlIssue]:
    """Risolve le anomalie `open` di quel tipo per la revisione. Nessun commit."""
    rows = list(
        (
            await db.execute(
                select(TlIssue).where(
                    TlIssue.revision_id == revision_id,
                    TlIssue.kind == kind,
                    TlIssue.status == TlIssueStatus.OPEN,
                )
            )
        ).scalars()
    )
    for issue in rows:
        issue.status = TlIssueStatus.RESOLVED
        if actor is not None:
            issue.updated_by = actor.email
    return rows


async def _to_out(
    db: AsyncSession, rows: list[tuple[TlIssue, str | None, str | None]]
) -> list[TlIssueOut]:
    out = []
    for issue, code, title in rows:
        item = TlIssueOut.model_validate(issue, from_attributes=True)
        item.feature_code = code
        item.feature_title = title
        out.append(item)
    return out


def _title_query():
    """Titolo corrente: revisione pubblicata, altrimenti quella di lavoro."""
    rev_id = func.coalesce(TlFeature.published_revision_id, TlFeature.working_revision_id)
    return (
        select(TlIssue, TlFeature.code, TlFeatureRevision.title)
        .select_from(TlIssue)
        .outerjoin(TlFeature, TlFeature.id == TlIssue.feature_id)
        .outerjoin(TlFeatureRevision, TlFeatureRevision.id == rev_id)
    )


async def list_issues(
    db: AsyncSession,
    status: TlIssueStatus | None = None,
    feature_id: uuid.UUID | None = None,
    kind: TlIssueKind | None = None,
) -> list[TlIssueOut]:
    stmt = _title_query()
    if status is not None:
        stmt = stmt.where(TlIssue.status == status)
    if feature_id is not None:
        stmt = stmt.where(TlIssue.feature_id == feature_id)
    if kind is not None:
        stmt = stmt.where(TlIssue.kind == kind)
    rows = (await db.execute(stmt.order_by(TlIssue.created_at.desc(), TlIssue.id.desc()))).all()
    return await _to_out(db, [(r[0], r[1], r[2]) for r in rows])


async def update_issue(
    db: AsyncSession, issue_id: uuid.UUID, new_status: TlIssueStatus, actor_email: str
) -> TlIssueOut:
    actor = await resolve_actor(db, actor_email)
    issue = (
        await db.execute(
            select(TlIssue)
            .where(TlIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if issue is None:
        raise IssueNotFoundError(str(issue_id))
    previous = issue.status
    issue.status = new_status
    issue.updated_by = actor.email
    if issue.feature_id is not None:
        feature = await features_svc._lock_feature(db, issue.feature_id)
        history.record_event(
            db,
            feature.id,
            "tl.issue.updated",
            actor,
            {
                "issue_id": str(issue.id),
                "kind": issue.kind.value,
                "from": previous.value,
                "to": new_status.value,
            },
        )
        if (
            new_status == TlIssueStatus.RESOLVED
            and issue.kind == TlIssueKind.STRUCTURAL_CHANGE
            and feature.status == TlFeatureStatus.DESIGNED
            and feature.published_revision_id is not None
        ):
            await db.flush()
            pending = (
                await db.execute(
                    select(func.count())
                    .select_from(TlIssue)
                    .where(
                        TlIssue.feature_id == feature.id,
                        TlIssue.kind == TlIssueKind.STRUCTURAL_CHANGE,
                        TlIssue.status != TlIssueStatus.RESOLVED,
                    )
                )
            ).scalar_one()
            if pending == 0:
                feature.status = TlFeatureStatus.IMPLEMENTED
                history.record_event(
                    db,
                    feature.id,
                    "tl.feature.implemented",
                    actor,
                    {"reason": "structural_changes_resolved", "issue_id": str(issue.id)},
                )
    await db.commit()
    rows = (await db.execute(_title_query().where(TlIssue.id == issue_id))).all()
    return (await _to_out(db, [(r[0], r[1], r[2]) for r in rows]))[0]
