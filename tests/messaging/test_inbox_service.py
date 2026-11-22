from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC, datetime, timedelta

from app.messaging.inbox_service import inbox_summary, list_inbox, mark_all_read, mark_read
from tests._db import TestSession
from tests.messaging.helpers import add_inbox, users


# The service compares with the real clock for retention, so rows are "now"-relative.
def _ago(minutes: int = 0, days: int = 0) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes, days=days)


async def test_list_orders_newest_first_and_paginates_with_a_cursor() -> None:
    async with users(("qa_manager", True)) as (uid,):
        for i in range(5):
            await add_inbox(uid, title=f"m{i}", created_at=_ago(minutes=50 - i))
        async with TestSession() as s:
            page1 = await list_inbox(s, uid, limit=2)
            assert [m.title for m in page1.items] == ["m4", "m3"]
            assert page1.next_cursor is not None
            page2 = await list_inbox(s, uid, limit=2, cursor=page1.next_cursor)
            assert [m.title for m in page2.items] == ["m2", "m1"]
            page3 = await list_inbox(s, uid, limit=2, cursor=page2.next_cursor)
            assert [m.title for m in page3.items] == ["m0"]
            assert page3.next_cursor is None


async def test_list_filters_by_unread_severity_and_type() -> None:
    async with users(("qa_manager", True)) as (uid,):
        await add_inbox(uid, title="read-info", severity="info", read=True, created_at=_ago(5))
        await add_inbox(
            uid, title="unread-warn", severity="warning", type="x.a", created_at=_ago(4)
        )
        await add_inbox(uid, title="unread-info", severity="info", type="x.b", created_at=_ago(3))
        async with TestSession() as s:
            titles = lambda page: sorted(m.title for m in page.items)  # noqa: E731
            assert titles(await list_inbox(s, uid, unread_only=True)) == [
                "unread-info",
                "unread-warn",
            ]
            assert titles(await list_inbox(s, uid, severity="warning")) == ["unread-warn"]
            assert titles(await list_inbox(s, uid, type="x.b")) == ["unread-info"]


async def test_summary_distinguishes_no_messages_all_read_and_unread() -> None:
    async with users(("qa_manager", True)) as (uid,):
        async with TestSession() as s:
            empty = await inbox_summary(s, uid)
        assert (empty.unread, empty.total, empty.max_unread_severity) == (0, 0, None)

        await add_inbox(uid, severity="critical", read=True, created_at=_ago(9))
        async with TestSession() as s:
            all_read = await inbox_summary(s, uid)
        assert (all_read.unread, all_read.total, all_read.max_unread_severity) == (0, 1, None)

        await add_inbox(uid, severity="info", created_at=_ago(8))
        await add_inbox(uid, severity="warning", created_at=_ago(7))
        async with TestSession() as s:
            mixed = await inbox_summary(s, uid)
        assert (mixed.unread, mixed.total, mixed.max_unread_severity) == (2, 3, "warning")

        await add_inbox(uid, severity="critical", created_at=_ago(6))
        async with TestSession() as s:
            assert (await inbox_summary(s, uid)).max_unread_severity == "critical"


async def test_rows_older_than_retention_are_excluded() -> None:
    async with users(("qa_manager", True)) as (uid,):
        await add_inbox(uid, title="old", created_at=_ago(days=100))
        await add_inbox(uid, title="new", created_at=_ago(1))
        async with TestSession() as s:
            assert (await inbox_summary(s, uid)).total == 1
            assert [m.title for m in (await list_inbox(s, uid)).items] == ["new"]


async def test_mark_read_only_touches_the_users_own_rows() -> None:
    async with users(("qa_manager", True), ("qa_manager", True)) as (mine, other):
        theirs = await add_inbox(other, created_at=_ago(2))
        own = await add_inbox(mine, created_at=_ago(3))
        async with TestSession() as s:
            assert await mark_read(s, mine, theirs) is False
            assert await mark_read(s, mine, uuid.uuid4()) is False
            assert await mark_read(s, mine, own) is True
            assert await mark_read(s, mine, own) is True  # idempotent
            assert (await inbox_summary(s, mine)).unread == 0
            assert (await inbox_summary(s, other)).unread == 1


async def test_mark_all_read_returns_the_count_and_is_idempotent() -> None:
    async with users(("qa_manager", True)) as (uid,):
        await add_inbox(uid, created_at=_ago(3))
        await add_inbox(uid, created_at=_ago(2))
        await add_inbox(uid, read=True, created_at=_ago(1))
        async with TestSession() as s:
            assert await mark_all_read(s, uid) == 2
            assert await mark_all_read(s, uid) == 0
            assert (await inbox_summary(s, uid)).unread == 0
