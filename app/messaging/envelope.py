"""CloudEvents 1.0 envelope used by every producer and consumer of the messaging hub."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["info", "warning", "critical"]
SEVERITY_ORDER: dict[str, int] = {"info": 0, "warning": 1, "critical": 2}

# Fixed namespace so that deterministic ids are stable across processes and releases.
_NAMESPACE = uuid.UUID("6f1c2c52-8f4d-4b0a-9a53-3f6f0b1d7a11")


class CloudEvent(BaseModel):
    """A CloudEvents 1.0 event in JSON structured mode, plus the hub's extension attributes."""

    model_config = ConfigDict(extra="forbid")

    specversion: Literal["1.0"] = "1.0"
    id: str
    source: str
    type: str
    subject: str | None = None
    time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    datacontenttype: Literal["application/json"] = "application/json"
    data: dict[str, Any]
    severity: Severity
    product: str | None = None
    environment: Literal["dev", "uat", "prod"] | None = None


def deterministic_id(event_type: str, key: str) -> str:
    """Return a stable UUIDv5 for an event type and a natural key (for example a run id)."""
    return str(uuid.uuid5(_NAMESPACE, f"{event_type}:{key}"))


def new_event(
    type: str,  # noqa: A002 - CloudEvents attribute name
    source: str,
    severity: Severity,
    data: dict[str, Any],
    *,
    subject: str | None = None,
    id: str | None = None,  # noqa: A002 - CloudEvents attribute name
    product: str | None = None,
    environment: str | None = None,
) -> CloudEvent:
    """Build an event with a generated id and the current time unless given."""
    return CloudEvent.model_validate(
        {
            "id": id or str(uuid.uuid4()),
            "source": source,
            "type": type,
            "subject": subject,
            "severity": severity,
            "data": data,
            "product": product,
            "environment": environment,
        }
    )
