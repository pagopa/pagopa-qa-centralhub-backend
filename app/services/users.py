from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.role import Role
from app.models.user import User


async def sync_login(db: AsyncSession, email: str, name: str, idp_sub: str | None) -> User:
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None:
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


async def list_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).order_by(User.created_at))
    return list(result.scalars().all())


async def update_user(
    db: AsyncSession, user_id: uuid.UUID, role: str | None, is_active: bool | None
) -> User | None:
    user = await db.get(User, user_id)
    if user is None:
        return None

    if role is not None:
        role_row = await db.get(Role, role)
        if role_row is None:
            raise ValueError(f"Unknown role: {role}")
        user.role = role

    if is_active is not None:
        user.is_active = is_active

    await db.commit()
    await db.refresh(user)
    return user
