from __future__ import annotations

# ruff: noqa: S101
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feature_visibility import FeatureVisibility
from app.services.feature_visibility import list_feature_visibility, update_feature_visibility
from tests._db import TestSession


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session
        await session.rollback()


@pytest.mark.anyio
async def test_feature_visibility_override_round_trips_through_database(
    db: AsyncSession,
) -> None:
    key = "nrt_metrics"
    existing = await db.get(FeatureVisibility, key)
    previous_enabled = existing.enabled if existing is not None else None

    try:
        updated = await update_feature_visibility(db, key, False)
        assert updated.key == key
        assert updated.enabled is False

        listing = await list_feature_visibility(db)
        enabled_by_key = {item.key: item.enabled for item in listing.items}
        assert enabled_by_key[key] is False
    finally:
        current = await db.get(FeatureVisibility, key)
        if previous_enabled is None:
            await db.execute(delete(FeatureVisibility).where(FeatureVisibility.key == key))
        elif current is not None:
            current.enabled = previous_enabled
        await db.commit()


@pytest.mark.anyio
async def test_update_feature_visibility_rejects_unknown_key_without_writing(
    db: AsyncSession,
) -> None:
    with pytest.raises(LookupError, match="Unknown feature: unknown"):
        await update_feature_visibility(db, "unknown", False)
