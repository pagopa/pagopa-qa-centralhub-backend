from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ACTION_CATALOG, ACTION_KEYS, compute_role_matrix
from app.models.role import Role
from app.schemas.role import ActionCatalogEntry, RoleMatrixResponse, RoleOut


async def get_role_matrix(db: AsyncSession) -> RoleMatrixResponse:
    result = await db.execute(select(Role).order_by(Role.key))
    roles = list(result.scalars().all())
    matrix = compute_role_matrix(roles)
    return RoleMatrixResponse(
        roles=[RoleOut(key=r.key, label=r.label, is_system=r.is_system) for r in roles],
        catalog=[
            ActionCatalogEntry(key=entry["key"], label=entry["label"], category=entry["category"])
            for entry in ACTION_CATALOG
        ],
        matrix=matrix,
    )


async def update_role_permissions(
    db: AsyncSession, role: str, permissions: dict[str, bool]
) -> dict[str, bool]:
    db_role = await db.get(Role, role)
    if db_role is None:
        raise LookupError(f"Unknown role: {role}")
    if db_role.is_system:
        raise ValueError("Cannot modify permissions for a system role")

    unknown_keys = set(permissions) - ACTION_KEYS
    if unknown_keys:
        raise ValueError(f"Unknown permission keys: {sorted(unknown_keys)}")

    db_role.permissions = {**db_role.permissions, **permissions}
    await db.commit()
    await db.refresh(db_role)

    result = await db.execute(select(Role).order_by(Role.key))
    roles = list(result.scalars().all())
    matrix = compute_role_matrix(roles)
    return matrix[role]
