from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.features import FEATURE_CATALOG, FEATURE_KEYS
from app.models.feature_visibility import FeatureVisibility
from app.schemas.feature_visibility import (
    FeatureVisibilityItem,
    FeatureVisibilityResponse,
    FeatureVisibilityUpdateResponse,
)

logger = structlog.get_logger(__name__)


async def list_feature_visibility(db: AsyncSession) -> FeatureVisibilityResponse:
    try:
        result = await db.scalars(select(FeatureVisibility))
        enabled_by_key = {row.key: row.enabled for row in result}
        if not enabled_by_key:
            logger.info("list_feature_visibility query succeeded with 0 results; using defaults")
        return FeatureVisibilityResponse(
            items=[
                FeatureVisibilityItem(**feature, enabled=enabled_by_key.get(feature["key"], True))
                for feature in FEATURE_CATALOG
            ]
        )
    except Exception:
        logger.exception("list_feature_visibility failed")
        raise


async def update_feature_visibility(
    db: AsyncSession, feature_key: str, enabled: bool
) -> FeatureVisibilityUpdateResponse:
    try:
        if feature_key not in FEATURE_KEYS:
            raise LookupError(f"Unknown feature: {feature_key}")

        override = await db.get(FeatureVisibility, feature_key)
        if override is None:
            logger.info("update_feature_visibility query succeeded with 0 results; creating override")
            override = FeatureVisibility(key=feature_key, enabled=enabled)
            db.add(override)
        else:
            override.enabled = enabled

        await db.commit()
        await db.refresh(override)
        return FeatureVisibilityUpdateResponse(key=feature_key, enabled=override.enabled)
    except Exception:
        logger.exception("update_feature_visibility failed")
        raise