"""Event catalog: every accepted event type, its deduplication policy and its message template.

Each type declares ``occurrence`` (a distinct fact identified by its id) or ``condition`` (the
current state of something that persists, reported by periodic scans and deduplicated on the set
of ``members``). Templates produce the Italian text stored in the user's inbox.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field, field_validator

from app.messaging.envelope import CloudEvent

QG_RUN_COMPLETED = "it.pagopa.qa.quality_gate.run.completed"
NRT_REVIEWS_PENDING = "it.pagopa.qa.nrt.reviews.pending"
SANP_CHECKS_FAILING = "it.pagopa.qa.sanp_health.checks.failing"
ANNOUNCEMENT_PUBLISHED = "it.pagopa.qa.announcement.published"

# Identity of each condition (the deduplication key), shared with the scanners that clear it.
NRT_PENDING_KEY = "nrt.reviews.pending"
SANP_FAILING_KEY = "sanp_health.checks.failing"


class UnknownEventType(LookupError):  # noqa: N818 - name fixed by the design
    """Raised when an event type is not in the catalog."""


class RenderedMessage(NamedTuple):
    title: str
    body: str
    link: str | None


class QgRunData(BaseModel):
    run_id: str
    test_object: str
    test_type: str
    environment: str | None = None
    scenario_qty: int
    passed: int
    failed: int
    broken: int
    skipped: int


class NrtPendingData(BaseModel):
    count: int
    run_ids: list[str]


class SanpFailingData(BaseModel):
    ko: int
    warning: int
    items: list[str]


class AnnouncementData(BaseModel):
    """A free-text announcement; the link, when present, must be an in-app path."""

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    link: str | None = Field(default=None, max_length=500)

    @field_validator("link")
    @classmethod
    def _internal_link(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("/") or value.startswith("//")):
            raise ValueError("link must be an in-app path starting with a single '/'")
        return value


# Alias defined outside the dataclass: inside it, the field named ``type`` shadows the builtin.
ModelClass = type[BaseModel]


@dataclass(frozen=True)
class EventType:
    type: str
    policy: Literal["occurrence", "condition"]
    data_model: ModelClass
    render: Callable[[CloudEvent], RenderedMessage]
    key: Callable[[CloudEvent], str] | None = None
    members: Callable[[CloudEvent], frozenset[str]] | None = None


def _render_qg(event: CloudEvent) -> RenderedMessage:
    data = QgRunData.model_validate(event.data)
    failures = data.failed + data.broken + data.skipped
    where = f"{data.test_object} / {data.test_type}"
    link = f"/quality-gate/report/{data.run_id}"
    if failures > 0:
        return RenderedMessage(
            f"Quality Gate non superato · {where}",
            f"{failures} scenari su {data.scenario_qty} non superati: "
            f"{data.failed} failed, {data.broken} broken, {data.skipped} skipped.",
            link,
        )
    return RenderedMessage(
        f"Quality Gate superato · {where}", f"{data.scenario_qty} scenari, tutti passati.", link
    )


def _render_nrt(event: CloudEvent) -> RenderedMessage:
    data = NrtPendingData.model_validate(event.data)
    return RenderedMessage(
        f"{data.count} run NRT in attesa di analisi",
        "Controlla le run in stato PENDING e assegna un esito.",
        "/nrts",
    )


def _render_sanp(event: CloudEvent) -> RenderedMessage:
    data = SanpFailingData.model_validate(event.data)
    parts: list[str] = []
    if data.ko:
        parts.append(f"{data.ko} controlli KO")
    if data.warning:
        parts.append(
            f"{data.warning} in warning" if data.ko else f"{data.warning} controlli in warning"
        )
    return RenderedMessage(
        f"SANP Health: {' e '.join(parts)}", "Controlla i dettagli per ambiente.", "/sanp-health"
    )


def _render_announcement(event: CloudEvent) -> RenderedMessage:
    data = AnnouncementData.model_validate(event.data)
    return RenderedMessage(data.title, data.body, data.link)


def _nrt_members(event: CloudEvent) -> frozenset[str]:
    return frozenset(NrtPendingData.model_validate(event.data).run_ids)


def _sanp_members(event: CloudEvent) -> frozenset[str]:
    return frozenset(SanpFailingData.model_validate(event.data).items)


EVENT_TYPES: dict[str, EventType] = {
    QG_RUN_COMPLETED: EventType(
        type=QG_RUN_COMPLETED, policy="occurrence", data_model=QgRunData, render=_render_qg
    ),
    ANNOUNCEMENT_PUBLISHED: EventType(
        type=ANNOUNCEMENT_PUBLISHED,
        policy="occurrence",
        data_model=AnnouncementData,
        render=_render_announcement,
    ),
    NRT_REVIEWS_PENDING: EventType(
        type=NRT_REVIEWS_PENDING,
        policy="condition",
        data_model=NrtPendingData,
        render=_render_nrt,
        key=lambda _event: NRT_PENDING_KEY,
        members=_nrt_members,
    ),
    SANP_CHECKS_FAILING: EventType(
        type=SANP_CHECKS_FAILING,
        policy="condition",
        data_model=SanpFailingData,
        render=_render_sanp,
        key=lambda _event: SANP_FAILING_KEY,
        members=_sanp_members,
    ),
}


def get_event_type(type: str) -> EventType:  # noqa: A002 - mirrors the CloudEvents attribute
    try:
        return EVENT_TYPES[type]
    except KeyError as exc:
        raise UnknownEventType(type) from exc


def validate_catalog(types: Mapping[str, EventType] | None = None) -> None:
    """Fail loudly when a type's policy and its key/members declarations disagree."""
    for name, event_type in (types if types is not None else EVENT_TYPES).items():
        if event_type.policy == "condition":
            if event_type.key is None or event_type.members is None:
                raise ValueError(f"{name}: a condition event type needs key and members")
        elif event_type.key is not None or event_type.members is not None:
            raise ValueError(f"{name}: an occurrence event type must not declare key or members")
