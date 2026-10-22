from __future__ import annotations

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import ACTION_CATALOG, ACTION_KEYS, compute_role_matrix
from app.models.role import Role
from app.schemas.role import ActionCatalogEntry, RoleMatrixResponse, RoleOut

logger = structlog.get_logger(__name__)


async def get_role_matrix(db: AsyncSession) -> RoleMatrixResponse:
    try:
        result = await db.execute(select(Role).order_by(Role.key))
        roles = list(result.scalars().all())
        if not roles:
            logger.info("get_role_matrix query succeeded with 0 results")
        matrix = compute_role_matrix(roles)
        return RoleMatrixResponse(
            roles=[RoleOut(key=r.key, label=r.label, is_system=r.is_system) for r in roles],
            catalog=[
                ActionCatalogEntry(key=entry["key"], label=entry["label"], category=entry["category"])
                for entry in ACTION_CATALOG
            ],
            matrix=matrix,
        )
    except Exception:
        logger.exception("get_role_matrix failed")
        raise


async def update_role_permissions(
    db: AsyncSession, role: str, permissions: dict[str, bool]
) -> dict[str, bool]:
    try:
        db_role = await db.get(Role, role)
        if db_role is None:
            logger.info("update_role_permissions query succeeded with 0 results")
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
        if not roles:
            logger.info("update_role_permissions matrix query succeeded with 0 results")
        matrix = compute_role_matrix(roles)
        return matrix[role]
    except Exception:
        logger.exception("update_role_permissions failed")
        raise
