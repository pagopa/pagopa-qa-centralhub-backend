from __future__ import annotations

# ruff: noqa: S101
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.tm import router as tm_router
from app.deps import get_db
from app.models.tm import ExternalResource, ResourceAbsence
from app.services import tm
from tests._db import TestSession, get_test_db

LEAVES = "44e8b664-leaves"
EVENTS = "02d4c57b-events"
SUBCALENDARS = {
    "success": True,
    "payload": [
        {
            "subCalendar": {
                "id": "parent-1",
                "name": "QA TS300 - Calendario Team Ext",
                "type": "parent",
            },
            "childSubCalendars": [
                {"subCalendar": {"id": EVENTS, "type": "other", "name": "QA TS300"}},
                {"subCalendar": {"id": LEAVES, "type": "leaves", "name": "QA TS300"}},
                {"subCalendar": {"id": "nrt-1", "type": "custom", "name": "QA TS300"}},
            ],
        }
    ],
}


def _event(
    eid: str, start: str, end: str, who: list[str], title: str = "Ferie", all_day: bool = True
) -> dict[str, Any]:
    """``start``/``end`` are dates, or ``date Thh:mm`` strings for timed events."""

    def iso(v: str) -> str:
        return f"{v}:00.000+02:00" if "T" in v else f"{v}T00:00:00.000+02:00"

    return {
        "id": eid,
        "title": title,
        "allDay": all_day,
        "start": iso(start),
        "end": iso(end),
        "eventType": "leaves",
        "invitees": [{"displayName": n, "id": f"acc-{n}"} for n in who],
    }


class FakeConfluence:
    def __init__(
        self, events: dict[str, list[dict[str, Any]]] | None = None, status: int = 200
    ) -> None:
        self.events = events or {}
        self.status = status
        self.event_calls: list[dict[str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subcalendars.json"):
            assert request.url.params["calendarContext"] == "spaceCalendars"
            assert request.url.params["viewingSpaceKey"] == "QDP"
            return httpx.Response(200, json=SUBCALENDARS)
        if request.url.path.endswith("/events.json"):
            self.event_calls.append(dict(request.url.params))
            if self.status != 200:
                return httpx.Response(self.status, text="boom")
            events = self.events.get(request.url.params["subCalendarId"], [])
            return httpx.Response(200, json={"events": events, "success": True})
        return httpx.Response(404)


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tm.settings, "jira_base_url", "https://pagopa.atlassian.net")
    monkeypatch.setattr(tm.settings, "jira_email", "bot@example.test")
    monkeypatch.setattr(tm.settings, "jira_api_token", "token")


def _use(monkeypatch: pytest.MonkeyPatch, fake: FakeConfluence) -> None:
    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(tm.httpx, "AsyncClient", factory)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await session.execute(delete(ResourceAbsence))
        await session.execute(delete(ExternalResource))
        session.add(
            ExternalResource(
                first_name="Mario",
                last_name="Rossì",
                email="mario.rossi@example.test",
                company="ACME",
                role="QA",
                daily_rate=300,
                contract_start=date(2026, 1, 1),
            )
        )
        await session.commit()
        yield session
        await session.rollback()
        await session.execute(delete(ResourceAbsence))
        await session.execute(delete(ExternalResource))
        await session.commit()


async def _absences(db: AsyncSession) -> list[tuple[date, str, str]]:
    rows = await db.scalars(select(ResourceAbsence).order_by(ResourceAbsence.absence_date))
    return [(a.absence_date, a.source, a.absence_type) for a in rows]


def test_month_window_uses_iso_datetimes_in_rome_time() -> None:
    assert tm._utc_window(2026, 10) == ("2026-09-30T22:00:00Z", "2026-10-31T23:00:00Z")
    assert tm._utc_window(2026, 1) == ("2025-12-31T23:00:00Z", "2026-01-31T23:00:00Z")


def test_event_days_treat_all_day_end_as_exclusive_and_clip_to_month() -> None:
    one_day = _event("1", "2026-06-03", "2026-06-04", [])
    assert tm._event_days(one_day, *tm._month_bounds(2026, 6)) == [date(2026, 6, 3)]
    across = _event("2", "2026-09-28", "2026-10-02", [])
    assert tm._event_days(across, *tm._month_bounds(2026, 10)) == [date(2026, 10, 1)]
    assert tm._event_days(across, *tm._month_bounds(2026, 9)) == [
        date(2026, 9, 28),
        date(2026, 9, 29),
        date(2026, 9, 30),
    ]


def test_names_match_regardless_of_accents_and_order() -> None:
    assert tm._normalize_name("  Rossì   Mario ") == "rossi mario"


async def test_sync_reads_events_and_leaves_calendars_and_replaces_the_month(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, fake_settings: None
) -> None:
    resource = await db.scalar(select(ExternalResource))
    assert resource is not None
    db.add_all(
        [
            # removed from the calendar since the last sync: must disappear
            ResourceAbsence(
                resource_id=resource.id, absence_date=date(2026, 10, 20), source="confluence"
            ),
            # entered by hand: must stay
            ResourceAbsence(
                resource_id=resource.id, absence_date=date(2026, 10, 27), source="manual"
            ),
        ]
    )
    await db.commit()
    fake = FakeConfluence(
        {
            LEAVES: [
                # Fri 9 → Mon 12 (exclusive end 13): Sat/Sun skipped
                _event("e1", "2026-10-09", "2026-10-13", ["Rossi Mario"]),
                # with hours → permesso, whatever the title says
                _event(
                    "e2",
                    "2026-10-15T09:00",
                    "2026-10-15T13:00",
                    ["Mario Rossi"],
                    title="Malattia",
                    all_day=False,
                ),
            ],
            EVENTS: [_event("e3", "2026-10-21", "2026-10-22", ["Giulia Bianchi"])],
            "nrt-1": [_event("x", "2026-10-22", "2026-10-23", ["Mario Rossi"])],
        }
    )
    _use(monkeypatch, fake)

    result = await tm.sync_from_confluence(db, 2026, 10)

    assert [c["subCalendarId"] for c in fake.event_calls] == [EVENTS, LEAVES]
    assert fake.event_calls[0]["start"] == "2026-09-30T22:00:00Z"
    assert result["synced"] == 3
    assert result["errors"] == [
        "Persone nel calendario non presenti tra le risorse T&M attive: Giulia Bianchi"
    ]
    assert await _absences(db) == [
        (date(2026, 10, 9), "confluence", "ferie"),
        (date(2026, 10, 12), "confluence", "ferie"),
        (date(2026, 10, 15), "confluence", "permesso"),
        (date(2026, 10, 27), "manual", "ferie"),
    ]
    permesso = await db.scalar(
        select(ResourceAbsence).where(ResourceAbsence.absence_date == date(2026, 10, 15))
    )
    assert permesso is not None and permesso.note == "09:00–13:00"

    # idempotent: the same picture gives the same rows
    again = await tm.sync_from_confluence(db, 2026, 10)
    assert again["synced"] == 3 and len(await _absences(db)) == 4


async def test_a_failing_calendar_changes_nothing(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, fake_settings: None
) -> None:
    resource = await db.scalar(select(ExternalResource))
    assert resource is not None
    db.add(
        ResourceAbsence(
            resource_id=resource.id, absence_date=date(2026, 10, 20), source="confluence"
        )
    )
    await db.commit()
    _use(monkeypatch, FakeConfluence(status=500))

    result = await tm.sync_from_confluence(db, 2026, 10)

    assert result["synced"] == 0 and "nessuna modifica applicata" in result["errors"][0]
    assert await _absences(db) == [(date(2026, 10, 20), "confluence", "ferie")]


def test_easter_monday_is_the_day_after_easter_also_at_month_end() -> None:
    # Easter 2027 is on 28 March: Easter Monday is 29 March, not 1 April
    assert date(2027, 3, 29) in tm._italian_holidays(2027, 3)
    assert date(2027, 4, 1) not in tm._italian_holidays(2027, 4)
    # Easter 2026 is on 5 April
    assert date(2026, 4, 6) in tm._italian_holidays(2026, 4)


async def test_events_calendar_counts_too_and_a_whole_day_wins_over_a_permesso(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, fake_settings: None
) -> None:
    fake = FakeConfluence(
        {
            EVENTS: [
                _event(
                    "p1",
                    "2026-10-14T14:00",
                    "2026-10-14T18:00",
                    ["Mario Rossi"],
                    title="Visita",
                    all_day=False,
                ),
                _event("f2", "2026-10-16", "2026-10-17", ["Mario Rossi"], title="Evento"),
            ],
            LEAVES: [_event("f1", "2026-10-14", "2026-10-15", ["Mario Rossi"])],
        }
    )
    _use(monkeypatch, fake)

    result = await tm.sync_from_confluence(db, 2026, 10)

    assert result["synced"] == 2 and result["errors"] == []
    assert await _absences(db) == [
        (date(2026, 10, 14), "confluence", "ferie"),
        (date(2026, 10, 16), "confluence", "ferie"),
    ]


async def test_delete_all_absences_needs_confirm_and_wipes_every_source(db: AsyncSession) -> None:
    resource = await db.scalar(select(ExternalResource))
    assert resource is not None
    db.add_all(
        [
            ResourceAbsence(resource_id=resource.id, absence_date=date(2026, 9, d), source=source)
            for d, source in [(1, "confluence"), (2, "manual"), (3, "csv")]
        ]
    )
    await db.commit()
    app = FastAPI()
    app.include_router(tm_router, prefix="/api/v1/tm")
    app.dependency_overrides[get_db] = get_test_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        refused = await client.delete("/api/v1/tm/absences")
        assert refused.status_code == 400
        assert len(await _absences(db)) == 3

        done = await client.delete("/api/v1/tm/absences", params={"confirm": "true"})

    assert done.status_code == 200 and done.json() == {"deleted": 3}
    assert await _absences(db) == []


def test_sync_window_spans_last_year_to_next_year() -> None:
    assert tm.sync_window(date(2026, 10, 16)) == (date(2025, 1, 1), date(2027, 12, 31))


async def test_full_sync_covers_the_whole_window_one_year_at_a_time(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, fake_settings: None
) -> None:
    resource = await db.scalar(select(ExternalResource))
    assert resource is not None
    db.add_all(
        [
            # stale, inside the window: must go
            ResourceAbsence(
                resource_id=resource.id, absence_date=date(2027, 5, 3), source="confluence"
            ),
            # outside the window: left alone
            ResourceAbsence(
                resource_id=resource.id, absence_date=date(2025, 6, 3), source="confluence"
            ),
        ]
    )
    await db.commit()
    monkeypatch.setattr(tm, "sync_window", lambda: (date(2026, 1, 1), date(2027, 12, 31)))
    fake = FakeConfluence(
        {
            LEAVES: [
                _event("a", "2026-02-02", "2026-02-03", ["Mario Rossi"]),
                _event("b", "2027-01-04", "2027-01-05", ["Mario Rossi"]),
            ]
        }
    )
    _use(monkeypatch, fake)

    result = await tm.sync_from_confluence(db)

    assert [(c["subCalendarId"], c["start"], c["end"]) for c in fake.event_calls] == [
        (EVENTS, "2025-12-31T23:00:00Z", "2026-12-31T23:00:00Z"),
        (EVENTS, "2026-12-31T23:00:00Z", "2027-12-31T23:00:00Z"),
        (LEAVES, "2025-12-31T23:00:00Z", "2026-12-31T23:00:00Z"),
        (LEAVES, "2026-12-31T23:00:00Z", "2027-12-31T23:00:00Z"),
    ]
    assert result["synced"] == 2
    assert (result["period_start"], result["period_end"]) == ("2026-01-01", "2027-12-31")
    assert await _absences(db) == [
        (date(2025, 6, 3), "confluence", "ferie"),
        (date(2026, 2, 2), "confluence", "ferie"),
        (date(2027, 1, 4), "confluence", "ferie"),
    ]


async def test_events_without_invitees_take_the_person_from_the_title(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, fake_settings: None
) -> None:
    permesso = _event(
        "p",
        "2026-07-01T09:00",
        "2026-07-01T12:00",
        [],
        title="Permesso - Mario Rossi",
        all_day=False,
    )
    ferie = _event("f", "2026-07-03", "2026-07-04", [], title="Ferie - Rossi Mario")
    other = _event("o", "2026-07-06", "2026-07-07", [], title="Riunione di team")
    for e in (permesso, ferie, other):
        e.pop("invitees")
    _use(monkeypatch, FakeConfluence({EVENTS: [permesso, ferie, other]}))

    result = await tm.sync_from_confluence(db, 2026, 7)

    assert result["synced"] == 2
    assert result["errors"] == [
        "Persone nel calendario non presenti tra le risorse T&M attive: Riunione di team"
    ]
    assert await _absences(db) == [
        (date(2026, 7, 1), "confluence", "permesso"),
        (date(2026, 7, 3), "confluence", "ferie"),
    ]
