from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC
from typing import Any

import pytest
from pydantic import ValidationError

from app.messaging.catalog import QG_RUN_COMPLETED
from app.messaging.envelope import CloudEvent, deterministic_id, new_event


def test_new_event_defaults() -> None:
    event = new_event(QG_RUN_COMPLETED, "/qa-hub/quality-gate", "info", {"a": 1})

    assert uuid.UUID(event.id)
    assert event.specversion == "1.0"
    assert event.datacontenttype == "application/json"
    assert event.time.tzinfo is not None
    assert event.time.utcoffset() == UTC.utcoffset(None)


def test_cloudevent_rejects_unknown_severity_and_extra_fields() -> None:
    base: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "source": "/x",
        "type": "t",
        "data": {},
        "severity": "info",
    }
    CloudEvent(**base)
    with pytest.raises(ValidationError):
        CloudEvent(**{**base, "severity": "fatal"})
    with pytest.raises(ValidationError):
        CloudEvent(**{**base, "unexpected": 1})


def test_deterministic_id_is_stable() -> None:
    first = deterministic_id(QG_RUN_COMPLETED, "run-1")
    assert first == deterministic_id(QG_RUN_COMPLETED, "run-1")
    assert first != deterministic_id(QG_RUN_COMPLETED, "run-2")
    assert uuid.UUID(first)
