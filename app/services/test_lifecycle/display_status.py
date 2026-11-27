"""The status a feature is shown with: the change in progress, not the published version.

`TlFeature.status` only moves on the first publication, so a feature that is reopened keeps
`implemented`/`running` while its new revision is drafted, reviewed or approved. What people work
on is that revision: it decides the status shown. "Running" (NRT executions) is not a phase: it is
reported apart (`is_running`) and always shown as a badge.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureRevision,
    TlFeatureStatus,
    TlRevisionStatus,
)

_OF_REVISION = {
    TlRevisionStatus.DRAFT: TlFeatureStatus.DRAFT,
    TlRevisionStatus.IN_REVIEW: TlFeatureStatus.IN_REVIEW,
    TlRevisionStatus.APPROVED: TlFeatureStatus.DESIGNED,
}


def display_status(
    status: TlFeatureStatus, working_status: TlRevisionStatus | None
) -> TlFeatureStatus:
    """Status shown: the open revision phase, else the stored one (`running` is implemented)."""
    if working_status in _OF_REVISION:
        return _OF_REVISION[working_status]
    return TlFeatureStatus.IMPLEMENTED if status == TlFeatureStatus.RUNNING else status


def is_running(status: TlFeatureStatus) -> bool:
    return status == TlFeatureStatus.RUNNING


async def working_statuses(
    db: AsyncSession, features: list[TlFeature]
) -> dict[uuid.UUID, TlRevisionStatus]:
    """Status of the open (working) revision of each feature that has one, by feature id."""
    rev_ids = {f.working_revision_id: f.id for f in features if f.working_revision_id}
    if not rev_ids:
        return {}
    rows = await db.execute(
        select(TlFeatureRevision.id, TlFeatureRevision.status).where(
            TlFeatureRevision.id.in_(list(rev_ids))
        )
    )
    return {rev_ids[rid]: st for rid, st in rows.all()}
