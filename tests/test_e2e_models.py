from __future__ import annotations
from app.models.e2e import E2eRun, E2eSuite


def test_e2e_suite_tablename() -> None:
    assert E2eSuite.__tablename__ == "e2e_suites"


def test_e2e_run_tablename() -> None:
    assert E2eRun.__tablename__ == "e2e_runs"
