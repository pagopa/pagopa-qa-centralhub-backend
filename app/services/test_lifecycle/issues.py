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

# Ordine globale dei lock di riga: feature -> issue (mai il contrario), per evitare deadlock
# tra `update_issue` e i writer che aprono/aggiornano anomalie dopo aver bloccato la feature.


class IssueNotFoundError(Exception):
    pass


class IssueAlreadyResolvedError(Exception):
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
    Non esegue il commit: lo fa il chiamante. La deduplica non è imposta dal DB: il chiamante
    deve tenere il lock di riga della feature o il lock advisory del sync, e rispettare l'ordine
    dei lock feature -> issue.
    """
    existing = await find_open_issue(
        db, kind=kind, feature_id=feature_id, path=path, key=detail.get("key")
    )
    if existing is not None:
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


async def find_open_issue(
    db: AsyncSession,
    *,
    kind: TlIssueKind,
    feature_id: uuid.UUID | None,
    path: str | None,
    key: Any,
    statuses: tuple[TlIssueStatus, ...] = (TlIssueStatus.OPEN,),
) -> TlIssue | None:
    """L'anomalia con identità `(kind, feature_id, path, detail.key)` in uno degli stati dati
    (default: solo `open`), se esiste."""
    candidates = (
        await db.execute(
            select(TlIssue)
            .where(
                TlIssue.kind == kind,
                TlIssue.status.in_(statuses),
                TlIssue.feature_id.is_(None)
                if feature_id is None
                else TlIssue.feature_id == feature_id,
                TlIssue.path.is_(None) if path is None else TlIssue.path == path,
            )
            .order_by(TlIssue.created_at, TlIssue.id)
        )
    ).scalars()
    return next((i for i in candidates if (i.detail or {}).get("key") == key), None)


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


async def resolve_feature_issues(
    db: AsyncSession, *, kind: TlIssueKind, feature_id: uuid.UUID
) -> list[TlIssue]:
    """Risolve le anomalie `open` di quel tipo per la feature (azione di sistema). Nessun commit.

    Il chiamante tiene il lock di riga della feature (ordine feature -> issue).
    """
    rows = list(
        (
            await db.execute(
                select(TlIssue).where(
                    TlIssue.feature_id == feature_id,
                    TlIssue.kind == kind,
                    TlIssue.status == TlIssueStatus.OPEN,
                )
            )
        ).scalars()
    )
    for issue in rows:
        issue.status = TlIssueStatus.RESOLVED
    return rows


# Anomalie che impediscono IMPLEMENTED finché non sono risolte (aperte o riconosciute):
# il file su Git non è coerente con la pubblicata (spec §6, ruling R8/R10).
BLOCKING_KINDS = (
    TlIssueKind.STRUCTURAL_CHANGE,
    TlIssueKind.PARSE_ERROR,
    TlIssueKind.FILE_DELETED,
)


async def maybe_promote_feature(
    db: AsyncSession,
    feature: TlFeature,
    actor: User | None,
    *,
    reason: str,
    issue_id: uuid.UUID | None = None,
) -> bool:
    """Unica regola DESIGNED -> IMPLEMENTED: revisione pubblicata e nessuna anomalia bloccante
    non risolta. Il chiamante tiene il lock della feature. Nessun commit."""
    if feature.status != TlFeatureStatus.DESIGNED or feature.published_revision_id is None:
        return False
    await db.flush()
    pending = (
        await db.execute(
            select(func.count())
            .select_from(TlIssue)
            .where(
                TlIssue.feature_id == feature.id,
                TlIssue.kind.in_(BLOCKING_KINDS),
                TlIssue.status != TlIssueStatus.RESOLVED,
            )
        )
    ).scalar_one()
    if pending:
        return False
    feature.status = TlFeatureStatus.IMPLEMENTED
    detail: dict[str, Any] = {
        "reason": reason,
        "revision_id": str(feature.published_revision_id),
    }
    if issue_id is not None:
        detail["issue_id"] = str(issue_id)
    history.record_event(db, feature.id, "tl.feature.implemented", actor, detail)
    return True


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
    feature_id = (
        await db.execute(select(TlIssue.feature_id).where(TlIssue.id == issue_id))
    ).one_or_none()
    if feature_id is None:
        raise IssueNotFoundError(str(issue_id))
    locked_feature = (
        await features_svc._lock_feature(db, feature_id[0]) if feature_id[0] is not None else None
    )
    issue = (
        await db.execute(
            select(TlIssue)
            .where(TlIssue.id == issue_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if issue is None or issue.feature_id != feature_id[0]:
        raise IssueNotFoundError(str(issue_id))
    if issue.status == new_status:
        await db.rollback()
        return await _get_out(db, issue_id)
    if issue.status == TlIssueStatus.RESOLVED:
        await db.rollback()
        raise IssueAlreadyResolvedError(str(issue_id))
    previous = issue.status
    issue.status = new_status
    issue.updated_by = actor.email
    if locked_feature is not None:
        feature = locked_feature
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
        if new_status == TlIssueStatus.RESOLVED and issue.kind in BLOCKING_KINDS:
            await maybe_promote_feature(
                db, feature, actor, reason="issues_resolved", issue_id=issue.id
            )
    await db.commit()
    return await _get_out(db, issue_id)


async def _get_out(db: AsyncSession, issue_id: uuid.UUID) -> TlIssueOut:
    rows = (await db.execute(_title_query().where(TlIssue.id == issue_id))).all()
    return (await _to_out(db, [(r[0], r[1], r[2]) for r in rows]))[0]
