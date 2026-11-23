from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

import app.messaging  # noqa: F401  (registers the inbox consumer)
from app.messaging.catalog import (
    ANNOUNCEMENT_PUBLISHED,
    NRT_REVIEWS_PENDING,
    QG_RUN_COMPLETED,
    SANP_CHECKS_FAILING,
)
from app.messaging.consumers import CONSUMERS
from app.messaging.inbox import merge_roles
from app.messaging.rules import validate_registry
from app.messaging.transport import PostgresTransport
from app.models.messaging import MsgDelivery, MsgInbox
from app.models.user import User
from tests._db import TestSession
from tests.messaging.helpers import NOW, get_delivery, seeded, users

QG_DATA = {
    "run_id": "r-1",
    "test_object": "pagopa-fdr",
    "test_type": "integration",
    "environment": "uat",
    "scenario_qty": 10,
    "passed": 9,
    "failed": 1,
    "broken": 0,
    "skipped": 0,
}


async def _drain(at: datetime = NOW) -> None:
    await PostgresTransport(TestSession, clock=lambda: at).drain_due(limit=100)


async def _inbox_for(user_ids: list[uuid.UUID]) -> list[MsgInbox]:
    async with TestSession() as s:
        return list(
            (
                await s.execute(
                    select(MsgInbox)
                    .where(MsgInbox.user_id.in_(user_ids))
                    .order_by(MsgInbox.created_at)
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        )


def test_inbox_consumer_is_registered_and_production_rules_are_valid() -> None:
    assert "inbox" in CONSUMERS
    validate_registry()


def test_merge_roles_unions_and_sorts() -> None:
    assert merge_roles({"roles": ["b"]}, {"roles": ["a", "b"]}) == {"roles": ["a", "b"]}


async def test_fan_out_by_role_with_rendered_snapshot() -> None:
    async with users(("qa_manager", True), ("qa_engineer", True), ("guest", True)) as (
        mgr,
        eng,
        gst,
    ):
        async with seeded(
            ["inbox"],
            event_type=QG_RUN_COMPLETED,
            severity="warning",
            data=QG_DATA,
            params={"roles": ["qa_manager", "qa_engineer"]},
        ):
            await _drain()
            rows = await _inbox_for([mgr, eng, gst])
        assert {r.user_id for r in rows} == {mgr, eng}
        row = rows[0]
        assert row.title == "Quality Gate non superato · pagopa-fdr / integration"
        assert row.body == "1 scenari su 10 non superati: 1 failed, 0 broken, 0 skipped."
        assert row.link == "/quality-gate/report/r-1"
        assert row.severity == "warning"
        assert row.type == QG_RUN_COMPLETED
        assert row.dedupe_key is None
        assert row.read_at is None
        assert row.auto_read is False


async def test_announcement_is_delivered_with_its_own_text_and_link() -> None:
    data = {"title": "Novità", "body": "Testo.", "link": "/messages"}
    async with users(("qa_analyst", True)) as (analyst,):
        async with seeded(
            ["inbox"],
            event_type=ANNOUNCEMENT_PUBLISHED,
            data=data,
            params={"roles": ["qa_analyst"]},
        ):
            await _drain()
            rows = await _inbox_for([analyst])
        assert [(r.title, r.body, r.link, r.severity) for r in rows] == [
            ("Novità", "Testo.", "/messages", "info")
        ]


async def test_inactive_users_are_skipped() -> None:
    async with users(("qa_manager", True), ("qa_manager", False)) as (active, inactive):
        async with seeded(
            ["inbox"],
            event_type=QG_RUN_COMPLETED,
            data=QG_DATA,
            params={"roles": ["qa_manager"]},
        ):
            await _drain()
            rows = await _inbox_for([active, inactive])
        assert {r.user_id for r in rows} == {active}


async def test_superadmin_always_receives_even_when_not_in_the_audience() -> None:
    async with users(("superadmin", True), ("guest", True)) as (admin, guest):
        async with seeded(
            ["inbox"],
            event_type=QG_RUN_COMPLETED,
            data=QG_DATA,
            params={"roles": ["qa_manager"]},
        ):
            await _drain()
            rows = await _inbox_for([admin, guest])
        assert {r.user_id for r in rows} == {admin}


async def test_no_matching_users_ends_done_without_rows() -> None:
    async with seeded(
        ["inbox"], event_type=QG_RUN_COMPLETED, data=QG_DATA, params={"roles": []}
    ) as seed:
        await _drain()
        delivery = await get_delivery(seed.delivery_ids[0])
        assert delivery.status == "DONE"
        async with TestSession() as s:
            rows = (
                await s.execute(
                    select(MsgInbox)
                    .join(User, User.id == MsgInbox.user_id)
                    .where(MsgInbox.event_id == seed.event_id, User.role != "superadmin")
                )
            ).all()
        assert rows == []


async def test_running_the_handler_twice_creates_one_row_per_user() -> None:
    async with users(("qa_manager", True)) as (mgr,):
        async with seeded(
            ["inbox"], event_type=QG_RUN_COMPLETED, data=QG_DATA, params={"roles": ["qa_manager"]}
        ) as seed:
            await _drain()
            async with TestSession() as s:
                row = await s.get(MsgDelivery, seed.delivery_ids[0])
                assert row is not None
                row.status = "PENDING"
                await s.commit()
            await _drain()
            assert len(await _inbox_for([mgr])) == 1


async def test_condition_message_supersedes_older_same_key_messages() -> None:
    roles = {"roles": ["qa_manager"]}
    async with users(("qa_manager", True)) as (mgr,):
        async with (
            seeded(
                ["inbox"],
                event_type=NRT_REVIEWS_PENDING,
                data={"count": 1, "run_ids": ["a"]},
                params=roles,
            ),
            seeded(
                ["inbox"],
                event_type=SANP_CHECKS_FAILING,
                severity="warning",
                data={"ko": 1, "warning": 0, "items": ["SANP:x"]},
                params=roles,
            ),
        ):
            await _drain()
            first = {r.type: r for r in await _inbox_for([mgr])}
            assert first[NRT_REVIEWS_PENDING].dedupe_key == "nrt.reviews.pending"
            assert first[NRT_REVIEWS_PENDING].read_at is None

            async with seeded(
                ["inbox"],
                event_type=NRT_REVIEWS_PENDING,
                data={"count": 2, "run_ids": ["a", "b"]},
                params=roles,
            ):
                await _drain(NOW + timedelta(minutes=1))
                rows = await _inbox_for([mgr])
        by_event: dict[str, list[MsgInbox]] = {}
        for r in rows:
            by_event.setdefault(r.type, []).append(r)
        nrt = sorted(by_event[NRT_REVIEWS_PENDING], key=lambda r: r.title)
        assert len(nrt) == 2
        older, newer = nrt[0], nrt[1]
        assert older.title.startswith("1 run NRT")
        assert older.read_at is not None
        assert older.auto_read is True
        assert newer.title.startswith("2 run NRT")
        assert newer.read_at is None
        sanp = by_event[SANP_CHECKS_FAILING][0]
        assert sanp.read_at is None
        assert sanp.auto_read is False
