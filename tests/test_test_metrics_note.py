from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.schemas.test_metrics import TestRunOut, UpdateNrtsObject
from app.services import test_metrics as svc


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


def _body(note: str | None) -> UpdateNrtsObject:
    run = TestRunOut(id=uuid.uuid4(), suite_id=uuid.uuid4(), note=note)
    return UpdateNrtsObject(test_runs=[run])


def _updated_values(db: MagicMock) -> list[dict]:
    # update(TestRun) is called with the list of value dicts as second argument.
    return db.execute.await_args_list[0].args[1]


def test_test_run_out_exposes_note() -> None:
    obj = SimpleNamespace(id=uuid.uuid4(), suite_id=uuid.uuid4(), note="x")
    assert TestRunOut.model_validate(obj, from_attributes=True).note == "x"


async def test_update_nrt_persists_note() -> None:
    db = _fake_db()
    await svc.update_nrt(db, _body("general note"))
    assert _updated_values(db)[0]["note"] == "general note"


async def test_update_nrt_clears_empty_note() -> None:
    db = _fake_db()
    await svc.update_nrt(db, _body(""))
    assert _updated_values(db)[0]["note"] is None
