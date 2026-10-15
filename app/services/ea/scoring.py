"""QA maturity score for EA Map components (0..5).

One point for each level of testing found. It measures the *presence* of tests,
not their outcome. The same formula is described in the Guide tab of the map.
"""

from __future__ import annotations

from dataclasses import dataclass

RUNTIME_KINDS = frozenset({"be", "fn", "fe"})

KIND_LABELS = {
    "be": "Microservizio",
    "fn": "Azure Functions",
    "fe": "Frontend",
    "lib": "Libreria",
    "infra": "Infrastruttura",
    "test": "Repo di test",
    "mock": "Mock",
    "tool": "Tool",
    "doc": "Doc/Spec",
}


@dataclass(frozen=True)
class QaFacts:
    unit_files: int = 0
    it_features: int = 0
    it_workflow: bool = False
    perf: bool = False
    central_suites: tuple[str, ...] = ()
    nrt_suites: tuple[str, ...] = ()
    nodo_bdd: bool = False


def maturity_score(facts: QaFacts) -> int:
    score = 0
    if facts.unit_files > 0:
        score += 1
    if facts.it_features > 0 or facts.it_workflow or facts.nodo_bdd:
        score += 1
    if facts.central_suites or facts.nodo_bdd:
        score += 1
    if facts.perf:
        score += 1
    if facts.nrt_suites:
        score += 1
    return score
