"""Commenti di review, anteprima del feature file e validazione live (spec §6, fase 1)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlReviewComment,
    TlRevisionStatus,
    TlScenario,
    TlScenarioContent,
    TlScenarioKind,
)
from app.models.user import User
from app.schemas.test_lifecycle import (
    TlCommentCreate,
    TlCommentOut,
    TlGherkinValidateRequest,
    TlLineError,
    TlPreviewError,
    TlPreviewOut,
)
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import history
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.gherkin import render_feature_file, validate_steps

COMMENTABLE_STATUSES = (TlRevisionStatus.DRAFT, TlRevisionStatus.IN_REVIEW)


class CommentNotFoundError(Exception):
    pass


class RevisionNotCommentableError(Exception):
    def __init__(self, status: TlRevisionStatus):
        super().__init__(status.value)
        self.status = status


def create_comment(
    db: AsyncSession,
    feature_id: uuid.UUID,
    revision: TlFeatureRevision,
    actor: User,
    body: str,
    scenario_id: uuid.UUID | None = None,
    *,
    emit_event: bool = True,
) -> TlReviewComment:
    """Unico punto di scrittura dei commenti. Non fa commit (lo fa il chiamante).

    `emit_event=False` serve alla richiesta di modifiche, che scrive già un proprio evento
    con la stessa nota.
    """
    comment = TlReviewComment(
        revision_id=revision.id, scenario_id=scenario_id, author=actor.email, body=body
    )
    db.add(comment)
    if emit_event:
        history.record_event(
            db,
            feature_id,
            "tl.comment.added",
            actor,
            {
                "revision_id": str(revision.id),
                "scenario_id": str(scenario_id) if scenario_id else None,
            },
        )
    return comment


def _comment_out(comment: TlReviewComment, scenario_tag: str | None) -> TlCommentOut:
    return TlCommentOut(
        id=comment.id,
        revision_id=comment.revision_id,
        scenario_id=comment.scenario_id,
        scenario_tag=scenario_tag,
        author=comment.author,
        body=comment.body,
        resolved=comment.resolved,
        created_at=comment.created_at,
    )


async def _require_revision(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID
) -> TlFeatureRevision:
    rev = (
        await db.execute(
            select(TlFeatureRevision).where(
                TlFeatureRevision.id == revision_id, TlFeatureRevision.feature_id == feature_id
            )
        )
    ).scalar_one_or_none()
    if rev is None:
        raise features_svc.RevisionNotFoundError(str(revision_id))
    return rev


async def list_comments(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID
) -> list[TlCommentOut]:
    await _require_revision(db, feature_id, revision_id)
    rows = (
        await db.execute(
            select(TlReviewComment, TlScenario.tag)
            .outerjoin(TlScenario, TlScenario.id == TlReviewComment.scenario_id)
            .where(TlReviewComment.revision_id == revision_id)
            .order_by(TlReviewComment.created_at, TlReviewComment.id)
        )
    ).all()
    return [_comment_out(c, tag) for c, tag in rows]


async def add_comment(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID, data: TlCommentCreate
) -> TlCommentOut:
    actor = await resolve_actor(db, data.actor_email)
    rev = await features_svc._lock_revision(db, feature_id, revision_id)
    if rev.status not in COMMENTABLE_STATUSES:
        raise RevisionNotCommentableError(rev.status)
    tag: str | None = None
    if data.scenario_id is not None:
        tag = (
            await db.execute(
                select(TlScenario.tag)
                .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
                .where(
                    TlScenario.id == data.scenario_id,
                    TlScenarioContent.revision_id == revision_id,
                )
            )
        ).scalar_one_or_none()
        if tag is None:
            raise features_svc.ScenarioNotFoundError(str(data.scenario_id))
    comment = create_comment(db, feature_id, rev, actor, data.body, data.scenario_id)
    await db.commit()
    await db.refresh(comment)
    return _comment_out(comment, tag)


async def set_resolved(
    db: AsyncSession, comment_id: uuid.UUID, actor_email: str, resolved: bool
) -> TlCommentOut:
    actor = await resolve_actor(db, actor_email)
    row = (
        await db.execute(
            select(TlReviewComment, TlScenario.tag, TlFeatureRevision.feature_id)
            .join(TlFeatureRevision, TlFeatureRevision.id == TlReviewComment.revision_id)
            .outerjoin(TlScenario, TlScenario.id == TlReviewComment.scenario_id)
            .where(TlReviewComment.id == comment_id)
            .with_for_update(of=TlReviewComment)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise CommentNotFoundError(str(comment_id))
    comment, tag, feature_id = row
    if comment.resolved != resolved:
        comment.resolved = resolved
        history.record_event(
            db,
            feature_id,
            "tl.comment.resolved" if resolved else "tl.comment.reopened",
            actor,
            {"comment_id": str(comment.id), "revision_id": str(comment.revision_id)},
        )
    await db.commit()
    await db.refresh(comment)
    return _comment_out(comment, tag)


async def preview(
    db: AsyncSession, feature_id: uuid.UUID, revision_id: uuid.UUID
) -> TlPreviewOut:
    """Feature file generato dalla revisione (qualsiasi stato) + errori di validazione."""
    rev = await _require_revision(db, feature_id, revision_id)
    feature = (
        await db.execute(select(TlFeature).where(TlFeature.id == feature_id))
    ).scalar_one()
    render, _rows = await history.build_render(db, feature, rev)
    errors: list[TlPreviewError] = []
    if render.background_steps and render.background_steps.strip():
        for e in validate_steps(
            render.background_steps, kind=TlScenarioKind.SCENARIO, examples=None, background=True
        ):
            errors.append(TlPreviewError(scenario_tag=None, line=e.line, message=e.message))
    for sc in render.scenarios:
        for e in validate_steps(sc.steps, kind=sc.kind, examples=sc.examples):
            errors.append(TlPreviewError(scenario_tag=sc.tag, line=e.line, message=e.message))
    return TlPreviewOut(file=render_feature_file(render), errors=errors)


def validate_block(data: TlGherkinValidateRequest) -> list[TlLineError]:
    """Validazione pura di un blocco di step (nessun accesso al DB)."""
    examples = data.examples.model_dump() if data.examples else None
    return [
        TlLineError(line=e.line, message=e.message)
        for e in validate_steps(
            data.steps, kind=data.kind, examples=examples, background=data.background
        )
    ]
