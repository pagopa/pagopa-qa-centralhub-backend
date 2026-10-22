from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.role import Role
from app.models.user import User

logger = structlog.get_logger(__name__)


async def sync_login(db: AsyncSession, email: str, name: str, idp_sub: str | None) -> User:
    try:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

        if user is None:
            logger.info("sync_login query succeeded with 0 results; creating user")
            user = User(email=email, name=name, role="guest", is_active=True, idp_sub=idp_sub)
            db.add(user)
            await db.commit()
            await db.refresh(user)
            return user

        changed = False
        if user.name != name:
            user.name = name
            changed = True
        if idp_sub is not None and user.idp_sub != idp_sub:
            user.idp_sub = idp_sub
            changed = True

        if changed:
            await db.commit()
            await db.refresh(user)
        return user
    except Exception:
        logger.exception("sync_login failed")
        raise


async def list_users(db: AsyncSession) -> list[User]:
    try:
        result = await db.execute(select(User).order_by(User.created_at))
        users = list(result.scalars().all())
        if not users:
            logger.info("list_users query succeeded with 0 results")
        return users
    except Exception:
        logger.exception("list_users failed")
        raise


async def update_user(
    db: AsyncSession, user_id: uuid.UUID, role: str | None, is_active: bool | None
) -> User | None:
    try:
        user = await db.get(User, user_id)
        if user is None:
            logger.info("update_user query succeeded with 0 results")
            return None

        if role is not None:
            role_row = await db.get(Role, role)
            if role_row is None:
                logger.info("update_user role query succeeded with 0 results")
                raise ValueError(f"Unknown role: {role}")
            user.role = role

        if is_active is not None:
            user.is_active = is_active

        await db.commit()
        await db.refresh(user)
        return user
    except Exception:
        logger.exception("update_user failed")
        raise
