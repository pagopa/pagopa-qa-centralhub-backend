"""Flakiness scoring of a scenario from its recent execution outcomes.

Pure functions: no database access. See section 6 of
docs/superpowers/specs/2026-11-14-flaky-scenarios-management-design.md.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from app.config import settings

ScoreState = Literal["OK", "INSUFFICIENT_DATA", "BROKEN"]


class Outcome(StrEnum):
    PASSED = "P"
    PASSED_ON_RETRY = "R"
    FAILED = "F"


def outcome_for(status: str, retries: int | None) -> Outcome | None:
    """Map an execution status to an outcome; SKIPPED executions are ignored (None)."""
    if status == "PASSED":
        return Outcome.PASSED_ON_RETRY if retries else Outcome.PASSED
    if status in ("FAILED", "BROKEN"):
        return Outcome.FAILED
    return None


@dataclass(frozen=True)
class ExecutionPoint:
    run_id: uuid.UUID
    ts: datetime
    outcome: Outcome
    suite_id: uuid.UUID


@dataclass(frozen=True)
class ScoringConfig:
    half_life_days: float
    min_runs: int
    broken_streak: int
    level_bounds: tuple[int, int, int, int]

    @classmethod
    def from_settings(cls) -> ScoringConfig:
        return cls(
            half_life_days=settings.flaky_half_life_days,
            min_runs=settings.flaky_min_runs,
            broken_streak=settings.flaky_broken_streak,
            level_bounds=settings.flaky_level_bounds,
        )


@dataclass(frozen=True)
class ScoreResult:
    score: int | None
    level: int | None
    state: ScoreState
    flip_rate: float
    retry_pass_rate: float
    runs_analyzed: int
    last_failure_at: datetime | None


def level_for(score: int, bounds: tuple[int, int, int, int]) -> int:
    """Level 1-5: level N+2 starts at bounds[N]."""
    return 1 + sum(1 for bound in bounds if score >= bound)


def _weight(point: ExecutionPoint, now: datetime, half_life_days: float) -> float:
    age_days = max(0.0, (now - point.ts).total_seconds() / 86400)
    return float(0.5 ** (age_days / half_life_days))


def compute_score(
    points: Sequence[ExecutionPoint], now: datetime, cfg: ScoringConfig
) -> ScoreResult:
    """Score points ordered oldest to newest (already filtered: no SKIPPED, eligible runs only)."""
    weights = [_weight(point, now, cfg.half_life_days) for point in points]

    flip_weight = 0.0
    pair_weight = 0.0
    for index in range(1, len(points)):
        is_pass = points[index].outcome != Outcome.FAILED
        was_pass = points[index - 1].outcome != Outcome.FAILED
        pair_weight += weights[index]
        if is_pass != was_pass:
            flip_weight += weights[index]
    flip_rate = round(flip_weight / pair_weight, 4) if pair_weight else 0.0

    total_weight = sum(weights)
    retry_weight = sum(
        w for w, p in zip(weights, points, strict=True) if p.outcome == Outcome.PASSED_ON_RETRY
    )
    retry_pass_rate = round(retry_weight / total_weight, 4) if total_weight else 0.0

    failures = [point.ts for point in points if point.outcome == Outcome.FAILED]
    last_failure_at = max(failures) if failures else None
    runs = len(points)

    if runs < cfg.min_runs:
        return ScoreResult(
            None, None, "INSUFFICIENT_DATA", flip_rate, retry_pass_rate, runs, last_failure_at
        )

    score = round(100 * max(flip_rate, retry_pass_rate))
    tail = points[-cfg.broken_streak :]
    broken = len(tail) == cfg.broken_streak and all(p.outcome == Outcome.FAILED for p in tail)
    state: ScoreState = "BROKEN" if broken else "OK"
    return ScoreResult(
        score,
        level_for(score, cfg.level_bounds),
        state,
        flip_rate,
        retry_pass_rate,
        runs,
        last_failure_at,
    )
