from __future__ import annotations

# ruff: noqa: S101
import pytest
from pydantic import BaseModel

from app.messaging.catalog import (
    ANNOUNCEMENT_PUBLISHED,
    NRT_REVIEWS_PENDING,
    QG_RUN_COMPLETED,
    SANP_CHECKS_FAILING,
    EventType,
    RenderedMessage,
    UnknownEventType,
    get_event_type,
    validate_catalog,
)
from app.messaging.envelope import new_event


def _qg(**overrides: int) -> dict[str, object]:
    data: dict[str, object] = {
        "run_id": "r-1",
        "test_object": "pagopa-fdr",
        "test_type": "integration",
        "environment": "uat",
        "scenario_qty": 148,
        "passed": 136,
        "failed": 8,
        "broken": 3,
        "skipped": 1,
    }
    data.update(overrides)
    return data


def test_every_event_type_declares_policy() -> None:
    validate_catalog()

    class Empty(BaseModel):
        pass

    bad = EventType(
        type="x",
        policy="condition",
        data_model=Empty,
        render=lambda e: RenderedMessage("t", "b", None),
        key=None,
        members=None,
    )
    with pytest.raises(ValueError):
        validate_catalog({"x": bad})

    bad_occurrence = EventType(
        type="y",
        policy="occurrence",
        data_model=Empty,
        render=lambda e: RenderedMessage("t", "b", None),
        key=lambda e: "k",
        members=lambda e: frozenset(),
    )
    with pytest.raises(ValueError):
        validate_catalog({"y": bad_occurrence})


def test_unknown_type_raises() -> None:
    with pytest.raises(UnknownEventType):
        get_event_type("nope")


def test_render_quality_gate_failed() -> None:
    event = new_event(QG_RUN_COMPLETED, "/s", "warning", _qg())
    msg = get_event_type(QG_RUN_COMPLETED).render(event)
    assert msg.title == "Quality Gate non superato · pagopa-fdr / integration"
    assert msg.body == "12 scenari su 148 non superati: 8 failed, 3 broken, 1 skipped."
    assert msg.link == "/quality-gate/report/r-1"


def test_render_quality_gate_passed() -> None:
    data = _qg(scenario_qty=86, passed=86, failed=0, broken=0, skipped=0)
    data.update(test_object="pagopa-gpd", test_type="e2e")
    event = new_event(QG_RUN_COMPLETED, "/s", "info", data)
    msg = get_event_type(QG_RUN_COMPLETED).render(event)
    assert msg.title == "Quality Gate superato · pagopa-gpd / e2e"
    assert msg.body == "86 scenari, tutti passati."


def test_render_nrt_pending_plural_and_singular() -> None:
    et = get_event_type(NRT_REVIEWS_PENDING)
    many = new_event(
        NRT_REVIEWS_PENDING, "/s", "info", {"count": 6, "run_ids": [str(i) for i in range(6)]}
    )
    one = new_event(NRT_REVIEWS_PENDING, "/s", "info", {"count": 1, "run_ids": ["a"]})
    assert et.render(many).title == "6 run NRT in attesa di analisi"
    assert et.render(many).link == "/nrts"
    assert et.render(one).title == "1 run NRT in attesa di analisi"


@pytest.mark.parametrize(
    ("ko", "warning", "title"),
    [
        (2, 3, "SANP Health: 2 controlli KO e 3 in warning"),
        (0, 3, "SANP Health: 3 controlli in warning"),
        (2, 0, "SANP Health: 2 controlli KO"),
    ],
)
def test_render_sanp_titles(ko: int, warning: int, title: str) -> None:
    event = new_event(
        SANP_CHECKS_FAILING, "/s", "warning", {"ko": ko, "warning": warning, "items": ["SANP:a"]}
    )
    msg = get_event_type(SANP_CHECKS_FAILING).render(event)
    assert msg.title == title
    assert msg.link == "/sanp-health"


def test_condition_key_and_members() -> None:
    nrt = new_event(NRT_REVIEWS_PENDING, "/s", "info", {"count": 2, "run_ids": ["a", "b"]})
    et = get_event_type(NRT_REVIEWS_PENDING)
    assert et.policy == "condition"
    assert et.key is not None
    assert et.members is not None
    assert et.key(nrt) == "nrt.reviews.pending"
    assert et.members(nrt) == frozenset({"a", "b"})

    sanp = new_event(
        SANP_CHECKS_FAILING, "/s", "warning", {"ko": 1, "warning": 0, "items": ["SANP:x"]}
    )
    et2 = get_event_type(SANP_CHECKS_FAILING)
    assert et2.key is not None
    assert et2.members is not None
    assert et2.key(sanp) == "sanp_health.checks.failing"
    assert et2.members(sanp) == frozenset({"SANP:x"})
    assert get_event_type(QG_RUN_COMPLETED).policy == "occurrence"


def test_announcement_is_an_occurrence_rendered_verbatim() -> None:
    event = new_event(
        ANNOUNCEMENT_PUBLISHED,
        "/s",
        "info",
        {"title": "Novità", "body": "Testo dell'annuncio.", "link": "/messages"},
    )
    et = get_event_type(ANNOUNCEMENT_PUBLISHED)
    assert et.policy == "occurrence"
    assert et.render(event) == RenderedMessage("Novità", "Testo dell'annuncio.", "/messages")


def test_announcement_link_is_optional() -> None:
    event = new_event(ANNOUNCEMENT_PUBLISHED, "/s", "info", {"title": "T", "body": "B"})
    assert get_event_type(ANNOUNCEMENT_PUBLISHED).render(event).link is None


@pytest.mark.parametrize(
    "data",
    [
        {"title": "", "body": "B"},
        {"title": "T", "body": ""},
        {"title": "T", "body": "B", "link": "https://example.com"},
        {"title": "T", "body": "B", "link": "//example.com"},
        {"title": "T", "body": "B", "link": "javascript:alert(1)"},
    ],
)
def test_announcement_rejects_invalid_data(data: dict[str, object]) -> None:
    event = new_event(ANNOUNCEMENT_PUBLISHED, "/s", "info", data)
    with pytest.raises(ValueError):
        get_event_type(ANNOUNCEMENT_PUBLISHED).data_model.model_validate(event.data)
