from __future__ import annotations

# ruff: noqa: S101
import uuid
from datetime import UTC, datetime, timedelta

from app.services.flaky_scoring import (
    ExecutionPoint,
    Outcome,
    ScoringConfig,
    compute_score,
    level_for,
    outcome_for,
)

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
CFG = ScoringConfig(half_life_days=14, min_runs=5, broken_streak=5, level_bounds=(5, 15, 30, 50))
SUITE = uuid.uuid4()


def pts(sequence: str, days_ago: float = 0) -> list[ExecutionPoint]:
    """Points oldest first; each one a minute after the previous, ending `days_ago` days back."""
    end = NOW - timedelta(days=days_ago)
    count = len(sequence)
    return [
        ExecutionPoint(
            run_id=uuid.uuid4(),
            ts=end - timedelta(minutes=count - 1 - index),
            outcome=Outcome(char),
            suite_id=SUITE,
        )
        for index, char in enumerate(sequence)
    ]


def test_always_passing_is_level_1() -> None:
    result = compute_score(pts("P" * 10), NOW, CFG)
    assert (result.score, result.level, result.state) == (0, 1, "OK")


def test_strict_alternation_is_level_5() -> None:
    result = compute_score(pts("PF" * 5), NOW, CFG)
    assert (result.flip_rate, result.score, result.level) == (1.0, 100, 5)


def test_all_passed_on_retry() -> None:
    result = compute_score(pts("R" * 6), NOW, CFG)
    assert (result.retry_pass_rate, result.flip_rate, result.score) == (1.0, 0.0, 100)


def test_retry_counts_as_pass_for_flips() -> None:
    assert compute_score(pts("PRPRPR"), NOW, CFG).flip_rate == 0.0


def test_last_five_failed_is_broken() -> None:
    result = compute_score(pts("PPPPPFFFFF"), NOW, CFG)
    assert result.state == "BROKEN"
    assert result.level is not None
    assert result.score is not None


def test_failures_not_at_the_end_are_not_broken() -> None:
    assert compute_score(pts("FFFFFP"), NOW, CFG).state == "OK"


def test_insufficient_data() -> None:
    result = compute_score(pts("PFPF"), NOW, CFG)
    assert (result.state, result.score, result.level, result.runs_analyzed) == (
        "INSUFFICIENT_DATA",
        None,
        None,
        4,
    )


def test_no_points() -> None:
    result = compute_score([], NOW, CFG)
    assert (result.state, result.flip_rate, result.retry_pass_rate, result.runs_analyzed) == (
        "INSUFFICIENT_DATA",
        0.0,
        0.0,
        0,
    )
    assert result.last_failure_at is None


def test_recent_flips_weigh_more() -> None:
    old = compute_score(pts("PFPF", days_ago=28) + pts("PPPPPP", days_ago=0), NOW, CFG)
    new = compute_score(pts("PPPPPP", days_ago=28) + pts("PFPF", days_ago=0), NOW, CFG)
    assert new.score is not None and old.score is not None
    assert new.score > old.score


def test_level_bounds() -> None:
    scores = (0, 4, 5, 14, 15, 29, 30, 49, 50, 100)
    assert [level_for(s, CFG.level_bounds) for s in scores] == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]


def test_last_failure_at_is_latest_failure() -> None:
    points = pts("PFPPFPP")
    assert compute_score(points, NOW, CFG).last_failure_at == points[4].ts


def test_rates_are_rounded_to_four_decimals() -> None:
    result = compute_score(pts("PPFPPPP"), NOW, CFG)
    assert result.flip_rate == round(result.flip_rate, 4)
    assert 0 < result.flip_rate < 1


def test_outcome_for() -> None:
    cases = [
        ("PASSED", 0),
        ("PASSED", None),
        ("PASSED", 2),
        ("FAILED", 0),
        ("BROKEN", 1),
        ("SKIPPED", 0),
    ]
    assert [outcome_for(status, retries) for status, retries in cases] == [
        "P",
        "P",
        "R",
        "F",
        "F",
        None,
    ]


def test_config_from_settings() -> None:
    cfg = ScoringConfig.from_settings()
    assert cfg == ScoringConfig(
        half_life_days=14.0, min_runs=5, broken_streak=5, level_bounds=(5, 15, 30, 50)
    )
