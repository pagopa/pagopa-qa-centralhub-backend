from __future__ import annotations

# ruff: noqa: S101
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from app.deps import get_db
from app.main import app


class FeatureVisibilityDatabase:
    def __init__(self) -> None:
        self.rows: dict[str, object] = {}

    async def scalars(self, *_args: object, **_kwargs: object) -> list[object]:
        return list(self.rows.values())

    async def get(self, _model: type, key: str) -> object | None:
        return self.rows.get(key)

    def add(self, row: object) -> None:
        self.rows[row.key] = row  # type: ignore[attr-defined]

    async def commit(self) -> None:
        pass

    async def refresh(self, _row: object) -> None:
        pass


@pytest.fixture
async def feature_client(client: AsyncClient) -> AsyncIterator[AsyncClient]:
    database = FeatureVisibilityDatabase()

    async def override_db() -> AsyncIterator[FeatureVisibilityDatabase]:
        yield database

    app.dependency_overrides[get_db] = override_db
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.anyio
async def test_list_feature_visibility_defaults_all_features_to_enabled(
    feature_client: AsyncClient,
) -> None:
    response = await feature_client.get("/api/v1/features")

    assert response.status_code == 200
    items = response.json()["items"]
    enabled_by_key = {item["key"]: item["enabled"] for item in items}

    assert enabled_by_key["nrt_metrics"] is True
    assert enabled_by_key["psp_fees"] is True
    assert enabled_by_key["gpd_positions"] is True
    assert enabled_by_key["api_docs"] is True
    assert enabled_by_key["settings_e2e"] is True
    assert enabled_by_key["settings_gherkin"] is True


@pytest.mark.anyio
async def test_update_feature_visibility_persists_the_override(
    feature_client: AsyncClient,
) -> None:
    response = await feature_client.patch("/api/v1/features/nrt_metrics", json={"enabled": False})

    assert response.status_code == 200
    assert response.json() == {"key": "nrt_metrics", "enabled": False}

    listing = await feature_client.get("/api/v1/features")
    enabled_by_key = {item["key"]: item["enabled"] for item in listing.json()["items"]}
    assert enabled_by_key["nrt_metrics"] is False


@pytest.mark.anyio
async def test_update_feature_visibility_rejects_unknown_key(
    feature_client: AsyncClient,
) -> None:
    response = await feature_client.patch("/api/v1/features/not-a-feature", json={"enabled": False})

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown feature: not-a-feature"
