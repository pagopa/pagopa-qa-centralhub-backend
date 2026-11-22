from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.deps import get_db
from app.main import app
from app.models.user import User
from tests._db import TestSession, get_test_db
from tests.messaging.helpers import add_inbox, users

BASE = "/api/v1/messaging"


@pytest.fixture
def _db_override() -> Iterator[None]:
    app.dependency_overrides[get_db] = get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def client(_db_override: None) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def _email(user_id: uuid.UUID) -> str:
    async with TestSession() as s:
        return (await s.execute(select(User.email).where(User.id == user_id))).scalar_one()


def _ago(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


async def test_missing_identity_header_is_401(client: AsyncClient) -> None:
    for method, path in [("GET", "/inbox"), ("GET", "/inbox/summary"), ("POST", "/inbox/read-all")]:
        response = await client.request(method, BASE + path)
        assert response.status_code == 401


async def test_unknown_and_inactive_users_get_an_empty_inbox(client: AsyncClient) -> None:
    async with users(("qa_manager", False)) as (inactive,):
        await add_inbox(inactive, created_at=_ago(1))
        for email in ["nobody@example.org", await _email(inactive)]:
            headers = {"X-User-Email": email}
            listing = await client.get(BASE + "/inbox", headers=headers)
            assert listing.status_code == 200
            assert listing.json() == {"items": [], "next_cursor": None}
            summary = await client.get(BASE + "/inbox/summary", headers=headers)
            assert summary.json() == {"unread": 0, "total": 0, "max_unread_severity": None}
            assert (await client.post(BASE + "/inbox/read-all", headers=headers)).json() == {
                "updated": 0
            }


async def test_list_summary_and_read_flow_with_case_insensitive_email(client: AsyncClient) -> None:
    async with users(("qa_manager", True)) as (uid,):
        first = await add_inbox(
            uid, title="a", severity="warning", link="/nrts", created_at=_ago(2)
        )
        await add_inbox(uid, title="b", severity="info", created_at=_ago(1))
        headers = {"X-User-Email": (await _email(uid)).upper()}

        listing = (await client.get(BASE + "/inbox", headers=headers)).json()
        assert [m["title"] for m in listing["items"]] == ["b", "a"]
        item = listing["items"][1]
        assert item["link"] == "/nrts"
        assert item["severity"] == "warning"
        assert item["read_at"] is None
        assert item["auto_read"] is False

        summary = (await client.get(BASE + "/inbox/summary", headers=headers)).json()
        assert summary == {"unread": 2, "total": 2, "max_unread_severity": "warning"}

        assert (await client.post(f"{BASE}/inbox/{first}/read", headers=headers)).status_code == 200
        after = (await client.get(BASE + "/inbox/summary", headers=headers)).json()
        assert after == {"unread": 1, "total": 2, "max_unread_severity": "info"}

        unread = (await client.get(BASE + "/inbox?unread_only=true", headers=headers)).json()
        assert [m["title"] for m in unread["items"]] == ["b"]

        assert (await client.post(BASE + "/inbox/read-all", headers=headers)).json() == {
            "updated": 1
        }
        done = (await client.get(BASE + "/inbox/summary", headers=headers)).json()
        assert done == {"unread": 0, "total": 2, "max_unread_severity": None}


async def test_reading_an_unknown_or_foreign_message_is_404(client: AsyncClient) -> None:
    async with users(("qa_manager", True), ("qa_manager", True)) as (mine, other):
        theirs = await add_inbox(other, created_at=_ago(1))
        headers = {"X-User-Email": await _email(mine)}
        assert (
            await client.post(f"{BASE}/inbox/{theirs}/read", headers=headers)
        ).status_code == 404
        random = uuid.uuid4()
        assert (
            await client.post(f"{BASE}/inbox/{random}/read", headers=headers)
        ).status_code == 404
