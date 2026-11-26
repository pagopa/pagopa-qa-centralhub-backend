"""Tag lists for the scenario editor and the import: the catalog table and the tags in use.

The catalog is no longer a gate: any well-formed tag can be saved (design 2026-11-26).
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import TlCategoryTag, TlScenarioContent
from app.services.test_lifecycle.tags import FLAKY_TAG, STATUS_TAGS


async def catalog_names(db: AsyncSession) -> set[str]:
    """Names in the catalog table (the migration's "keep" decisions): nothing else."""
    return set((await db.execute(select(TlCategoryTag.name))).scalars())


async def known_tags(db: AsyncSession) -> set[str]:
    """Allow-list for pushing/keeping tags during imports: catalog + status + flaky.

    Deliberately NOT the tags in use: what an import keeps must not depend on what other
    features happen to contain.
    """
    return await catalog_names(db) | set(STATUS_TAGS) | {FLAKY_TAG}


async def category_tags(db: AsyncSession) -> tuple[str, ...]:
    """Suggestions: catalog table + status/flaky names + every tag currently used on a scenario."""
    used = set(
        (
            await db.execute(
                select(func.jsonb_array_elements_text(TlScenarioContent.extra_tags)).distinct()
            )
        ).scalars()
    )
    return tuple(sorted(await catalog_names(db) | used | set(STATUS_TAGS) | {FLAKY_TAG}))
