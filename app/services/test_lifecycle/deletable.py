"""SQL condition shared by the feature/scope listings and the delete service.

Kept in its own module (models only) so `features`, `scopes` and `deletion` can all import it
without an import cycle.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, exists, or_, select

from app.models.test_lifecycle import TlFeature, TlFeatureRevision


def pr_open_clause() -> ColumnElement[bool]:
    """True when the feature's working revision has an open pull request."""
    return exists(
        select(TlFeatureRevision.id).where(
            TlFeatureRevision.id == TlFeature.working_revision_id,
            TlFeatureRevision.pr_state == "open",
        )
    )


def published_clause() -> ColumnElement[bool]:
    """True when the feature has a published revision or a file in Git."""
    return or_(TlFeature.published_revision_id.is_not(None), TlFeature.git_path.is_not(None))


def undeletable_clause() -> ColumnElement[bool]:
    """True for features that cannot be deleted from the hub."""
    return or_(published_clause(), pr_open_clause())
