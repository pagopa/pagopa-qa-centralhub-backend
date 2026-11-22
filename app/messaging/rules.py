"""Routing rules, defined in code and evaluated once, at acceptance time.

A rule maps events (by type, minimum severity, product, environment and an optional predicate) to
deliveries: a consumer plus its ``params``. The result is materialised as delivery rows, so a
consumer never re-evaluates rules and the rules stay independent from the transport.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

from app.core.permissions import EDITABLE_ROLES
from app.messaging.catalog import (
    EVENT_TYPES,
    NRT_REVIEWS_PENDING,
    QG_RUN_COMPLETED,
    SANP_CHECKS_FAILING,
)
from app.messaging.consumers import CONSUMERS
from app.messaging.envelope import SEVERITY_ORDER, CloudEvent, Severity

_GLOB_CHARS = "*?["


@dataclass(frozen=True)
class Match:
    type: str  # exact type id or fnmatch glob
    min_severity: Severity = "info"
    products: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    predicate: Callable[[CloudEvent], bool] | None = field(default=None, compare=False)

    def matches(self, event: CloudEvent) -> bool:
        if not fnmatchcase(event.type, self.type):
            return False
        if SEVERITY_ORDER[event.severity] < SEVERITY_ORDER[self.min_severity]:
            return False
        if self.products and event.product not in self.products:
            return False
        if self.environments and event.environment not in self.environments:
            return False
        return self.predicate is None or self.predicate(event)


@dataclass(frozen=True)
class DeliverySpec:
    consumer: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Rule:
    id: str
    match: Match
    deliveries: tuple[DeliverySpec, ...]


_ALL_QA_ROLES = ["qa_analyst", "qa_engineer", "qa_manager"]

RULES: tuple[Rule, ...] = (
    Rule(
        "qg-completed",
        Match(type=QG_RUN_COMPLETED),
        (DeliverySpec("inbox", {"roles": list(_ALL_QA_ROLES)}),),
    ),
    Rule(
        "nrt-pending-review",
        Match(type=NRT_REVIEWS_PENDING),
        (DeliverySpec("inbox", {"roles": list(_ALL_QA_ROLES)}),),
    ),
    Rule(
        "sanp-failing",
        Match(type=SANP_CHECKS_FAILING),
        (DeliverySpec("inbox", {"roles": list(_ALL_QA_ROLES)}),),
    ),
)


def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        if all(isinstance(item, str) for item in items):
            return sorted(items)
        return items
    return value


def params_hash(params: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the params (sorted keys, sorted lists of strings)."""
    payload = json.dumps(_canonical(params), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def route(event: CloudEvent, rules: Sequence[Rule] = RULES) -> list[DeliverySpec]:
    """Return the deliveries for an event: identical ones collapse, mergeable ones are merged."""
    result: list[DeliverySpec] = []
    for rule in rules:
        if not rule.match.matches(event):
            continue
        for spec in rule.deliveries:
            _add(result, spec)
    return result


def _add(result: list[DeliverySpec], spec: DeliverySpec) -> None:
    spec_hash = params_hash(spec.params)
    consumer = CONSUMERS.get(spec.consumer)
    for index, existing in enumerate(result):
        if existing.consumer != spec.consumer:
            continue
        if params_hash(existing.params) == spec_hash:
            return
        if consumer is not None and consumer.merge is not None:
            merged = consumer.merge(existing.params, spec.params)
            if merged is not None:
                result[index] = DeliverySpec(spec.consumer, merged)
                return
    result.append(spec)


def validate_registry(rules: Sequence[Rule] = RULES) -> None:
    """Fail loudly at startup on a rule that names an unknown consumer, role or event type."""
    for rule in rules:
        if not any(char in rule.match.type for char in _GLOB_CHARS) and (
            rule.match.type not in EVENT_TYPES
        ):
            raise ValueError(f"rule {rule.id}: unknown event type {rule.match.type}")
        for spec in rule.deliveries:
            if spec.consumer not in CONSUMERS:
                raise ValueError(f"rule {rule.id}: unknown consumer {spec.consumer}")
            for role in spec.params.get("roles", []):
                if role not in EDITABLE_ROLES:
                    raise ValueError(f"rule {rule.id}: unknown role {role}")
