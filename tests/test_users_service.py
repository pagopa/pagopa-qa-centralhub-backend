from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.users import list_users, sync_login, update_user
from tests._db import TestSession


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session
    async with TestSession() as session:
        await session.execute(delete(User).where(User.email.like("test-%@example.com")))
        await session.commit()


@pytest.mark.anyio
async def test_sync_login_creates_user_with_guest_role(db: AsyncSession) -> None:
    user = await sync_login(db, "test-new@example.com", "New User", "google-sub-1")
    assert user.role == "guest"
    assert user.is_active is True
    assert user.idp_sub == "google-sub-1"


@pytest.mark.anyio
async def test_sync_login_does_not_duplicate_by_email(db: AsyncSession) -> None:
    first = await sync_login(db, "test-dup@example.com", "Dup User", "google-sub-2")
    second = await sync_login(db, "test-dup@example.com", "Dup User", "google-sub-2")
    assert first.id == second.id

    users = await list_users(db)
    matches = [u for u in users if u.email == "test-dup@example.com"]
    assert len(matches) == 1


@pytest.mark.anyio
async def test_sync_login_updates_name_on_change(db: AsyncSession) -> None:
    user = await sync_login(db, "test-rename@example.com", "Old Name", "google-sub-3")
    updated = await sync_login(db, "test-rename@example.com", "New Name", "google-sub-3")
    assert updated.id == user.id
    assert updated.name == "New Name"


@pytest.mark.anyio
async def test_update_user_rejects_unknown_role(db: AsyncSession) -> None:
    user = await sync_login(db, "test-roleupdate@example.com", "Role User", "google-sub-4")
    with pytest.raises(ValueError):
        await update_user(db, user.id, role="not-a-role", is_active=None)


@pytest.mark.anyio
async def test_update_user_changes_role_and_active(db: AsyncSession) -> None:
    user = await sync_login(db, "test-promote@example.com", "Promote User", "google-sub-5")
    updated = await update_user(db, user.id, role="qa_engineer", is_active=False)
    assert updated is not None
    assert updated.role == "qa_engineer"
    assert updated.is_active is False


@pytest.mark.anyio
async def test_update_user_returns_none_for_unknown_id() -> None:
    import uuid

    async with TestSession() as session:
        result = await update_user(session, uuid.uuid4(), role=None, is_active=None)
    assert result is None
