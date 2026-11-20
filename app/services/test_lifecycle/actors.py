from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UnknownActorError(Exception):
    pass


async def resolve_actor(db: AsyncSession, email: str) -> User:
    """Risolve l'attore dichiarato nel body su `users.email` (case-insensitive, solo attivi)."""
    normalized = (email or "").strip().lower()
    user = (
        await db.execute(select(User).where(func.lower(User.email) == normalized).limit(1))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise UnknownActorError(email)
    return user
