from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


def _fake_user(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        id=uuid.uuid4(),
        email="user@example.com",
        name="User Name",
        role="qa_engineer",
        is_active=True,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.anyio
async def test_list_users(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.users.users_svc.list_users",
        new_callable=AsyncMock,
        return_value=[_fake_user()],
    ):
        response = await client.get("/api/v1/users")

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["email"] == "user@example.com"


@pytest.mark.anyio
async def test_update_user_returns_updated_user(client: AsyncClient) -> None:
    user_id = uuid.uuid4()
    with patch(
        "app.api.v1.users.users_svc.update_user",
        new_callable=AsyncMock,
        return_value=_fake_user(id=user_id, role="qa_manager"),
    ):
        response = await client.patch(f"/api/v1/users/{user_id}", json={"role": "qa_manager"})

    assert response.status_code == 200
    assert response.json()["role"] == "qa_manager"


@pytest.mark.anyio
async def test_update_user_returns_404_when_not_found(client: AsyncClient) -> None:
    user_id = uuid.uuid4()
    with patch(
        "app.api.v1.users.users_svc.update_user",
        new_callable=AsyncMock,
        return_value=None,
    ):
        response = await client.patch(f"/api/v1/users/{user_id}", json={"role": "qa_manager"})

    assert response.status_code == 404


@pytest.mark.anyio
async def test_update_user_returns_400_for_invalid_role(client: AsyncClient) -> None:
    user_id = uuid.uuid4()
    with patch(
        "app.api.v1.users.users_svc.update_user",
        new_callable=AsyncMock,
        side_effect=ValueError("Unknown role: bogus"),
    ):
        response = await client.patch(f"/api/v1/users/{user_id}", json={"role": "bogus"})

    assert response.status_code == 400


@pytest.mark.anyio
async def test_sync_login_returns_role_and_active(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.users.users_svc.sync_login",
        new_callable=AsyncMock,
        return_value=_fake_user(role="guest", is_active=True),
    ):
        response = await client.post(
            "/api/v1/users/sync-login",
            json={"email": "new@example.com", "name": "New User"},
        )

    assert response.status_code == 200
    assert response.json() == {"role": "guest", "is_active": True}
