"""Tag di categoria degli scenari: i 4 di base più quelli aggiunti (es. in migrazione)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_lifecycle import TlCategoryTag
from app.services.test_lifecycle.gherkin import ALLOWED_EXTRA_TAGS


class UnknownExtraTagError(Exception):
    def __init__(self, tags: list[str], allowed: tuple[str, ...]) -> None:
        super().__init__(", ".join(tags))
        self.tags = tags
        self.allowed = allowed


async def category_tags(db: AsyncSession) -> tuple[str, ...]:
    rows = set((await db.execute(select(TlCategoryTag.name))).scalars())
    return tuple(sorted(rows | set(ALLOWED_EXTRA_TAGS)))


async def check_extra_tags(db: AsyncSession, tags: list[str] | None) -> None:
    if not tags:
        return
    allowed = await category_tags(db)
    unknown = [t for t in tags if t not in allowed]
    if unknown:
        raise UnknownExtraTagError(unknown, allowed)
