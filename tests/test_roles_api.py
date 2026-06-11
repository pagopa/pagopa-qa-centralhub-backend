from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.schemas.role import ActionCatalogEntry, RoleMatrixResponse, RoleOut


def _matrix_response() -> RoleMatrixResponse:
    return RoleMatrixResponse(
        roles=[
            RoleOut(key="superadmin", label="Superadmin", is_system=True),
            RoleOut(key="qa_manager", label="QA Manager", is_system=False),
        ],
        catalog=[ActionCatalogEntry(key="view:overview", label="Overview", category="Generale")],
        matrix={
            "superadmin": {"view:overview": True},
            "qa_manager": {"view:overview": True},
        },
    )


@pytest.mark.anyio
async def test_get_role_matrix(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.roles.roles_svc.get_role_matrix",
        new_callable=AsyncMock,
        return_value=_matrix_response(),
    ):
        response = await client.get("/api/v1/roles")

    assert response.status_code == 200
    body = response.json()
    assert len(body["roles"]) == 2
    assert body["matrix"]["qa_manager"]["view:overview"] is True


@pytest.mark.anyio
async def test_update_role_permissions(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.roles.roles_svc.update_role_permissions",
        new_callable=AsyncMock,
        return_value={"view:overview": True, "view:bdd": False},
    ):
        response = await client.patch(
            "/api/v1/roles/qa_manager", json={"permissions": {"view:bdd": False}}
        )

    assert response.status_code == 200
    assert response.json() == {"view:overview": True, "view:bdd": False}


@pytest.mark.anyio
async def test_update_role_permissions_returns_404_for_unknown_role(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.roles.roles_svc.update_role_permissions",
        new_callable=AsyncMock,
        side_effect=LookupError("Unknown role: bogus"),
    ):
        response = await client.patch(
            "/api/v1/roles/bogus", json={"permissions": {"view:overview": True}}
        )

    assert response.status_code == 404


@pytest.mark.anyio
async def test_update_role_permissions_returns_400_for_system_role(client: AsyncClient) -> None:
    with patch(
        "app.api.v1.roles.roles_svc.update_role_permissions",
        new_callable=AsyncMock,
        side_effect=ValueError("Cannot modify permissions for a system role"),
    ):
        response = await client.patch(
            "/api/v1/roles/superadmin", json={"permissions": {"view:overview": False}}
        )

    assert response.status_code == 400
