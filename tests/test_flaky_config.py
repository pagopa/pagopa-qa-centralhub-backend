from __future__ import annotations

# ruff: noqa: S101
from app.config import Settings
from app.core.features import FEATURE_CATALOG


def test_flaky_feature_registered() -> None:
    feature = next(f for f in FEATURE_CATALOG if f["key"] == "flaky_scenarios")
    assert feature["route"] == "/flaky-scenarios"
    assert feature["action"] == "view:flaky"
    assert feature["label"] == "Scenari flaky"
    assert feature["external"] is False


def test_flaky_settings_defaults() -> None:
    s = Settings(_env_file=None)
    assert (s.flaky_window_days, s.flaky_max_runs, s.flaky_min_runs) == (30, 30, 5)
    assert s.flaky_half_life_days == 14.0
    assert (s.flaky_broken_streak, s.flaky_unmark_min_runs) == (5, 10)
    assert s.flaky_level_bounds == (5, 15, 30, 50)


def test_flaky_level_bounds_parse_override() -> None:
    s = Settings(_env_file=None, flaky_level_thresholds="10, 20,40,60")
    assert s.flaky_level_bounds == (10, 20, 40, 60)
