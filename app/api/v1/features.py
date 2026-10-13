from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.deps import DbDep
from app.schemas.feature_visibility import (
    FeatureVisibilityResponse,
    FeatureVisibilityUpdate,
    FeatureVisibilityUpdateResponse,
)
from app.services import feature_visibility as feature_visibility_svc

router = APIRouter()


@router.get("", response_model=FeatureVisibilityResponse)
async def list_feature_visibility(db: DbDep) -> FeatureVisibilityResponse:
    return await feature_visibility_svc.list_feature_visibility(db)


@router.patch("/{feature_key}", response_model=FeatureVisibilityUpdateResponse)
async def update_feature_visibility(
    feature_key: str,
    payload: FeatureVisibilityUpdate,
    db: DbDep,
) -> FeatureVisibilityUpdateResponse:
    try:
        return await feature_visibility_svc.update_feature_visibility(
            db, feature_key, payload.enabled
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc