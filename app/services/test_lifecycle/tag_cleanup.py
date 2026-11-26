"""Cleanup of test-type tags (`@e2e`, `@integration`, `@api`) carried by scenarios.

The test type belongs to the file (design 2026-11-26): the cleanup proposes to move it there and
removes it from the scenarios of the working revision. It never changes anything by itself.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlScenario,
    TlScenarioContent,
)
from app.schemas.test_lifecycle import TlFeatureDetail, TlTagCleanup, TlTagCleanupRow
from app.services.test_lifecycle import features as features_svc
from app.services.test_lifecycle import history
from app.services.test_lifecycle.actors import resolve_actor
from app.services.test_lifecycle.tags import legacy_test_type_tags


class TagCleanupConflictError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


async def _carriers(
    db: AsyncSession, revision_id: uuid.UUID
) -> list[tuple[TlScenario, TlScenarioContent, list[str]]]:
    rows = (
        await db.execute(
            select(TlScenario, TlScenarioContent)
            .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
            .where(TlScenarioContent.revision_id == revision_id)
            .order_by(TlScenarioContent.position)
        )
    ).all()
    out = []
    for scenario, content in rows:
        found = legacy_test_type_tags(content.extra_tags or [])
        if found:
            out.append((scenario, content, found))
    return out


def _proposal(
    feature: TlFeature, carriers: list[tuple[TlScenario, TlScenarioContent, list[str]]]
) -> tuple[str | None, str | None]:
    """`(proposed_test_type, conflict)` for the tags found."""
    found = sorted({t for _, _, tags in carriers for t in tags})
    if not found:
        return None, None
    if len(found) > 1:
        names = ", ".join(f"@{t}" for t in found)
        return None, f"I tag di test-type degli scenari non coincidono: {names}"
    only = found[0]
    if feature.test_type and feature.test_type != only:
        return None, f"Il test-type del file è già {feature.test_type}, diverso da @{only}"
    return only, None


async def preview(db: AsyncSession, feature_id: uuid.UUID) -> TlTagCleanup:
    feature = await db.get(TlFeature, feature_id)
    if feature is None:
        raise features_svc.FeatureNotFoundError(str(feature_id))
    if feature.working_revision_id is None:
        return TlTagCleanup(rows=[], proposed_test_type=None, conflict=None)
    carriers = await _carriers(db, feature.working_revision_id)
    proposed, conflict = _proposal(feature, carriers)
    return TlTagCleanup(
        rows=[TlTagCleanupRow(scenario_tag=sc.tag, tags=tags) for sc, _, tags in carriers],
        proposed_test_type=proposed,
        conflict=conflict,
    )


async def apply(db: AsyncSession, feature_id: uuid.UUID, actor_email: str) -> TlFeatureDetail:
    actor = await resolve_actor(db, actor_email)
    feature = await features_svc._lock_feature(db, feature_id)
    if feature.working_revision_id is None:
        raise features_svc.RevisionNotFoundError(str(feature_id))
    rev = await features_svc._lock_revision(db, feature_id, feature.working_revision_id)
    features_svc._require_draft(rev)
    carriers = await _carriers(db, rev.id)
    proposed, conflict = _proposal(feature, carriers)
    if conflict:
        await db.rollback()
        raise TagCleanupConflictError(conflict)
    if carriers:
        for _, content, tags in carriers:
            content.extra_tags = [t for t in content.extra_tags if t not in tags]
        if proposed and feature.test_type is None:
            feature.test_type = proposed
        rev.lock_version += 1
        history.record_event(
            db,
            feature.id,
            "tl.feature.tag_cleanup",
            actor,
            {"scenarios": len(carriers), "test_type": proposed},
        )
    await db.commit()
    await db.refresh(feature)  # updated_at is computed by the database
    await db.refresh(rev)
    return await features_svc.get_feature_detail(db, feature_id, actor_email)
