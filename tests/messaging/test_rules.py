from __future__ import annotations

# ruff: noqa: S101
from collections.abc import Iterator
from typing import Any

import pytest

from app.messaging.catalog import NRT_REVIEWS_PENDING, QG_RUN_COMPLETED, SANP_CHECKS_FAILING
from app.messaging.consumers import CONSUMERS, Consumer, DeliveryContext, register_consumer
from app.messaging.envelope import CloudEvent, new_event
from app.messaging.rules import (
    RULES,
    DeliverySpec,
    Match,
    Rule,
    params_hash,
    route,
    validate_registry,
)

THREE_ROLES = ["qa_analyst", "qa_engineer", "qa_manager"]


async def _noop(event: CloudEvent, ctx: DeliveryContext) -> None:
    return None


def _union(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    return {"roles": sorted(set(a["roles"]) | set(b["roles"]))}


@pytest.fixture
def consumers() -> Iterator[None]:
    saved = dict(CONSUMERS)
    register_consumer(Consumer(name="merging", handler=_noop, merge=_union))
    register_consumer(Consumer(name="plain", handler=_noop))
    yield
    CONSUMERS.clear()
    CONSUMERS.update(saved)


def _event(event_type: str, severity: str = "info", **extra: Any) -> CloudEvent:
    return new_event(event_type, "/t", severity, {}, **extra)  # type: ignore[arg-type]


def test_phase_one_rules_route_to_inbox_for_the_three_roles() -> None:
    for event_type, severity in [
        (QG_RUN_COMPLETED, "warning"),
        (QG_RUN_COMPLETED, "info"),
        (NRT_REVIEWS_PENDING, "info"),
        (SANP_CHECKS_FAILING, "warning"),
    ]:
        assert route(_event(event_type, severity)) == [
            DeliverySpec("inbox", {"roles": THREE_ROLES})
        ]


def test_unmapped_type_routes_nowhere() -> None:
    assert route(_event("it.pagopa.qa.other.thing.happened")) == []


def test_min_severity_filters(consumers: None) -> None:
    rules = [
        Rule(
            "r",
            Match(type=QG_RUN_COMPLETED, min_severity="warning"),
            (DeliverySpec("plain", {}),),
        )
    ]
    assert route(_event(QG_RUN_COMPLETED, "info"), rules) == []
    assert route(_event(QG_RUN_COMPLETED, "critical"), rules) == [DeliverySpec("plain", {})]


def test_glob_product_environment_and_predicate_filters(consumers: None) -> None:
    rules = [
        Rule(
            "r",
            Match(
                type="it.pagopa.qa.quality_gate.*",
                products=("fdr",),
                environments=("uat",),
                predicate=lambda e: e.subject == "go",
            ),
            (DeliverySpec("plain", {}),),
        )
    ]
    hit = _event(QG_RUN_COMPLETED, product="fdr", environment="uat", subject="go")
    assert route(hit, rules) == [DeliverySpec("plain", {})]
    assert (
        route(_event(QG_RUN_COMPLETED, product="gpd", environment="uat", subject="go"), rules) == []
    )
    assert (
        route(_event(QG_RUN_COMPLETED, product="fdr", environment="dev", subject="go"), rules) == []
    )
    assert (
        route(_event(QG_RUN_COMPLETED, product="fdr", environment="uat", subject="no"), rules) == []
    )


def test_same_consumer_params_are_merged_when_the_consumer_can(consumers: None) -> None:
    rules = [
        Rule(
            "a", Match(type=QG_RUN_COMPLETED), (DeliverySpec("merging", {"roles": ["qa_manager"]}),)
        ),
        Rule(
            "b",
            Match(type=QG_RUN_COMPLETED),
            (DeliverySpec("merging", {"roles": ["qa_engineer", "qa_manager"]}),),
        ),
    ]
    assert route(_event(QG_RUN_COMPLETED), rules) == [
        DeliverySpec("merging", {"roles": ["qa_engineer", "qa_manager"]})
    ]


def test_distinct_params_stay_separate_without_merge_hook_and_identical_collapse(
    consumers: None,
) -> None:
    rules = [
        Rule("a", Match(type=QG_RUN_COMPLETED), (DeliverySpec("plain", {"project": "A"}),)),
        Rule("b", Match(type=QG_RUN_COMPLETED), (DeliverySpec("plain", {"project": "B"}),)),
        Rule("c", Match(type=QG_RUN_COMPLETED), (DeliverySpec("plain", {"project": "A"}),)),
    ]
    assert route(_event(QG_RUN_COMPLETED), rules) == [
        DeliverySpec("plain", {"project": "A"}),
        DeliverySpec("plain", {"project": "B"}),
    ]


def test_params_hash_is_canonical() -> None:
    assert params_hash({"roles": ["b", "a"], "x": 1}) == params_hash({"x": 1, "roles": ["a", "b"]})
    assert params_hash({"roles": ["a"]}) != params_hash({"roles": ["b"]})
    assert len(params_hash({})) == 64


def test_validate_registry_rejects_unknown_consumer_and_role(consumers: None) -> None:
    ghost = [Rule("g", Match(type=QG_RUN_COMPLETED), (DeliverySpec("ghost", {}),))]
    with pytest.raises(ValueError, match="ghost"):
        validate_registry(ghost)

    wizard = [
        Rule(
            "w",
            Match(type=QG_RUN_COMPLETED),
            (DeliverySpec("merging", {"roles": ["wizard"]}),),
        )
    ]
    with pytest.raises(ValueError, match="wizard"):
        validate_registry(wizard)

    unknown_type = [Rule("t", Match(type="it.pagopa.qa.nope.x.y"), (DeliverySpec("plain", {}),))]
    with pytest.raises(ValueError, match="nope"):
        validate_registry(unknown_type)

    ok = [
        Rule(
            "ok",
            Match(type="it.pagopa.qa.*"),
            (DeliverySpec("merging", {"roles": ["qa_manager"]}),),
        )
    ]
    validate_registry(ok)


def test_rule_ids_are_unique() -> None:
    assert len({rule.id for rule in RULES}) == len(RULES)
