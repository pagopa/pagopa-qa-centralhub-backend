from __future__ import annotations

# ruff: noqa: S101
import logging
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel
from sqlalchemy import delete, select

from app.config import settings
from app.deps import get_db
from app.main import app
from app.messaging import rules as rules_module
from app.messaging.catalog import EVENT_TYPES, QG_RUN_COMPLETED, EventType, RenderedMessage
from app.messaging.rules import DeliverySpec, Match, Rule
from app.models.audit_log import AuditLog
from app.models.messaging import MsgDedupe, MsgEvent
from tests._db import TestSession, get_test_db

URL = "/api/v1/messaging/events"
KEY = "ingest-secret-for-tests"
COND = "it.pagopa.qa.test.condition.reported"


class CondData(BaseModel):
    key: str
    items: list[str]


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    app.dependency_overrides[get_db] = get_test_db
    monkeypatch.setattr(settings, "messaging_ingest_key", KEY)
    monkeypatch.setattr("app.api.v1.messaging.wake_dispatcher", lambda: None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def cond(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setitem(
        EVENT_TYPES,
        COND,
        EventType(
            type=COND,
            policy="condition",
            data_model=CondData,
            render=lambda e: RenderedMessage("t", "b", None),
            key=lambda e: str(e.data["key"]),
            members=lambda e: frozenset(e.data["items"]),
        ),
    )
    monkeypatch.setattr(
        rules_module,
        "RULES",
        (Rule("c", Match(type=COND), (DeliverySpec("inbox", {"roles": ["qa_manager"]}),)),),
    )
    yield


def _body(event_id: str | None = None, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "specversion": "1.0",
        "id": event_id or str(uuid.uuid4()),
        "source": "/test",
        "type": COND,
        "severity": "info",
        "data": {"key": f"k-{uuid.uuid4()}", "items": ["a"]},
    }
    body.update(overrides)
    return body


async def _cleanup(event_ids: list[str], keys: list[str]) -> None:
    async with TestSession() as s:
        await s.execute(delete(MsgEvent).where(MsgEvent.id.in_([uuid.UUID(i) for i in event_ids])))
        await s.execute(delete(MsgDedupe).where(MsgDedupe.key.in_(keys)))
        await s.execute(delete(AuditLog).where(AuditLog.resource_id.in_(event_ids)))
        await s.commit()


async def test_ingest_is_503_when_the_key_is_not_configured(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "messaging_ingest_key", "")
    response = await client.post(URL, json=_body(), headers={"X-Hub-Ingest-Key": KEY})
    assert response.status_code == 503


async def test_ingest_is_401_without_or_with_a_wrong_key(client: AsyncClient) -> None:
    assert (await client.post(URL, json=_body())).status_code == 401
    wrong = await client.post(URL, json=_body(), headers={"X-Hub-Ingest-Key": "nope"})
    assert wrong.status_code == 401


async def test_accepted_then_suppressed_for_the_same_condition(
    client: AsyncClient, cond: None
) -> None:
    headers = {"X-Hub-Ingest-Key": KEY}
    first = _body()
    key = first["data"]["key"]  # type: ignore[index]
    second = _body(data={"key": key, "items": ["a"]})
    try:
        accepted = await client.post(URL, json=first, headers=headers)
        assert accepted.status_code == 202
        assert accepted.json() == {"id": first["id"], "status": "accepted"}

        suppressed = await client.post(URL, json=second, headers=headers)
        assert suppressed.status_code == 200
        assert suppressed.json()["status"] == "suppressed"
    finally:
        await _cleanup([str(first["id"]), str(second["id"])], [key])


async def test_repeated_event_id_is_a_duplicate(client: AsyncClient) -> None:
    event_id = str(uuid.uuid4())
    data = {
        "run_id": "r-ing",
        "test_object": "o",
        "test_type": "e2e",
        "scenario_qty": 1,
        "passed": 1,
        "failed": 0,
        "broken": 0,
        "skipped": 0,
    }
    body = _body(event_id, type=QG_RUN_COMPLETED, data=data)
    headers = {"X-Hub-Ingest-Key": KEY}
    try:
        assert (await client.post(URL, json=body, headers=headers)).status_code == 202
        again = await client.post(URL, json=body, headers=headers)
        assert again.status_code == 200
        assert again.json()["status"] == "duplicate"
    finally:
        await _cleanup([event_id], [])


async def test_invalid_events_are_422(client: AsyncClient) -> None:
    headers = {"X-Hub-Ingest-Key": KEY}
    unknown = await client.post(URL, json=_body(type="it.pagopa.qa.nope.x.y"), headers=headers)
    assert unknown.status_code == 422
    bad_severity = await client.post(URL, json=_body(severity="fatal"), headers=headers)
    assert bad_severity.status_code == 422
    bad_data = await client.post(
        URL, json=_body(type=QG_RUN_COMPLETED, data={"run_id": "only"}), headers=headers
    )
    assert bad_data.status_code == 422
    bad_id = await client.post(URL, json=_body("not-a-uuid"), headers=headers)
    assert bad_id.status_code == 422


async def test_audit_row_is_written_and_the_key_is_never_logged(
    client: AsyncClient, cond: None, caplog: pytest.LogCaptureFixture
) -> None:
    body = _body()
    key = body["data"]["key"]  # type: ignore[index]
    try:
        with caplog.at_level(logging.DEBUG):
            response = await client.post(URL, json=body, headers={"X-Hub-Ingest-Key": KEY})
        assert response.status_code == 202
        async with TestSession() as s:
            row = (
                await s.execute(select(AuditLog).where(AuditLog.resource_id == body["id"]))
            ).scalar_one()
        assert row.action == "messaging.event.ingest"
        assert row.resource_type == "msg_event"
        assert COND in (row.detail or "")
        assert KEY not in caplog.text
    finally:
        await _cleanup([str(body["id"])], [key])
