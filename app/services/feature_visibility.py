from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.features import FEATURE_CATALOG, FEATURE_KEYS
from app.models.feature_visibility import FeatureVisibility
from app.schemas.feature_visibility import (
    FeatureVisibilityItem,
    FeatureVisibilityResponse,
    FeatureVisibilityUpdateResponse,
)


async def list_feature_visibility(db: AsyncSession) -> FeatureVisibilityResponse:
    result = await db.scalars(select(FeatureVisibility))
    enabled_by_key = {row.key: row.enabled for row in result}
    return FeatureVisibilityResponse(
        items=[
            FeatureVisibilityItem(**feature, enabled=enabled_by_key.get(feature["key"], True))
            for feature in FEATURE_CATALOG
        ]
    )


async def update_feature_visibility(
    db: AsyncSession, feature_key: str, enabled: bool
) -> FeatureVisibilityUpdateResponse:
    if feature_key not in FEATURE_KEYS:
        raise LookupError(f"Unknown feature: {feature_key}")

    override = await db.get(FeatureVisibility, feature_key)
    if override is None:
        override = FeatureVisibility(key=feature_key, enabled=enabled)
        db.add(override)
    else:
        override.enabled = enabled

    await db.commit()
    await db.refresh(override)
    return FeatureVisibilityUpdateResponse(key=feature_key, enabled=override.enabled)