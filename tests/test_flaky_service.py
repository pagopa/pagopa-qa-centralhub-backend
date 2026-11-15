from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.flaky import FlakyRecomputeStatus, FlakyRegistry, FlakyScore
from app.models.test_metrics import EnvEnum, ScenarioStatusEnum, TriggerTypeEnum
from app.models.test_metrics import TestExecution as ExecutionModel
from app.models.test_metrics import TestRun as RunModel
from app.models.test_metrics import TestSuite as SuiteModel
from app.schemas.flaky import FlakyFlagIn, FlakyScenarioRowOut
from app.services import flaky as flaky_svc
from app.services.flaky import (
    RECOMPUTE_LOCK_ID,
    FlakyRecomputeInProgressError,
    get_history,
    get_summary,
    list_scenarios,
    recompute_scores,
    set_flag,
)
from tests._db import TestSession

NOW = datetime.now(UTC).replace(microsecond=0)


@dataclass
class Seed:
    """Creates suites, runs and executions under a test_object unique to the test."""

    db: AsyncSession
    test_object: str = field(default_factory=lambda: f"flaky-{uuid.uuid4().hex[:8]}")
    suites: dict[tuple[str, str], SuiteModel] = field(default_factory=dict)

    async def suite(self, test_type: str = "e2e", version: str = "1.0.0") -> SuiteModel:
        key = (test_type, version)
        if key not in self.suites:
            suite = SuiteModel(
                test_object=self.test_object, test_type=test_type, test_object_version=version
            )
            self.db.add(suite)
            await self.db.flush()
            self.suites[key] = suite
        return self.suites[key]

    async def executions(
        self,
        outcomes: str,
        *,
        scenario: str = "S",
        test_type: str = "e2e",
        version: str = "1.0.0",
        env: EnvEnum = EnvEnum.UAT,
        trigger: TriggerTypeEnum = TriggerTypeEnum.CRON,
        days_ago: float = 0,
        same_timestamp: bool = False,
        origin: str = "NRT",
    ) -> list[RunModel]:
        """One run per char, oldest first: P, R (passed on retry), F, B (broken), S (skipped)."""
        suite = await self.suite(test_type, version)
        runs = []
        end = NOW - timedelta(days=days_ago)
        for index, char in enumerate(outcomes):
            ts = end if same_timestamp else end - timedelta(minutes=len(outcomes) - 1 - index)
            run = RunModel(
                suite_id=suite.id,
                env=env,
                trigger_type=trigger,
                timestamp_start=ts,
                test_version="1",
                test_origin=origin,
            )
            self.db.add(run)
            await self.db.flush()
            status = {
                "P": ScenarioStatusEnum.PASSED,
                "R": ScenarioStatusEnum.PASSED,
                "F": ScenarioStatusEnum.FAILED,
                "B": ScenarioStatusEnum.BROKEN,
                "S": ScenarioStatusEnum.SKIPPED,
            }[char]
            self.db.add(
                ExecutionModel(
                    run_id=run.id,
                    status=status,
                    scenario_name=scenario,
                    retries=1 if char == "R" else 0,
                )
            )
            runs.append(run)
        await self.db.commit()
        return runs

    async def scores(self) -> list[FlakyScore]:
        result = await self.db.execute(
            select(FlakyScore)
            .where(FlakyScore.test_object == self.test_object)
            .order_by(FlakyScore.test_type, FlakyScore.scenario_name, FlakyScore.env)
        )
        return list(result.scalars().all())


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        yield session
        await session.rollback()


@pytest.fixture
async def seed(db: AsyncSession) -> AsyncIterator[Seed]:
    data = Seed(db)
    yield data
    await db.rollback()
    await db.execute(delete(FlakyScore).where(FlakyScore.test_object == data.test_object))
    await db.execute(delete(FlakyRegistry).where(FlakyRegistry.test_object == data.test_object))
    await db.execute(delete(SuiteModel).where(SuiteModel.test_object == data.test_object))
    await db.commit()


async def test_recompute_scores_one_row_per_scenario_and_env(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("PFPFPF", env=EnvEnum.UAT)
    await seed.executions("PPPPPP", env=EnvEnum.DEV)

    await recompute_scores(db, trigger="MANUAL", requested_by="me@x.it", now=NOW)

    rows = await seed.scores()
    assert [(r.env, r.level, r.state) for r in rows] == [
        (EnvEnum.DEV, 1, "OK"),
        (EnvEnum.UAT, 5, "OK"),
    ]


async def test_manual_and_qa_gate_runs_are_ignored(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("PFPFPF", trigger=TriggerTypeEnum.MANUAL)
    await seed.executions("PFPFPF", trigger=TriggerTypeEnum.QA_GATE)

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    assert await seed.scores() == []


async def test_quality_gate_runs_are_ignored(db: AsyncSession, seed: Seed) -> None:
    # The ingestion sends Quality Gate runs with trigger CI_PIPELINE and origin QG.
    await seed.executions("PFPFPF", trigger=TriggerTypeEnum.CI_PIPELINE, origin="QG")

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    assert await seed.scores() == []


async def test_quality_gate_runs_are_left_out_of_the_history(db: AsyncSession, seed: Seed) -> None:
    nrt = await seed.executions("PF", trigger=TriggerTypeEnum.CRON, origin="NRT")
    await seed.executions("F", trigger=TriggerTypeEnum.CI_PIPELINE, origin="QG")

    history = await get_history(
        db, env=EnvEnum.UAT, test_object=seed.test_object, test_type="e2e", scenario_name="S"
    )

    assert [item.run_id for item in history] == [nrt[1].id, nrt[0].id]


async def test_eligible_triggers_and_skipped(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("PP", trigger=TriggerTypeEnum.CRON)
    await seed.executions("PP", trigger=TriggerTypeEnum.CI_PIPELINE)
    await seed.executions("PSS", trigger=TriggerTypeEnum.TAS)

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    (row,) = await seed.scores()
    assert row.runs_analyzed == 5


async def test_window_and_max_runs(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("FFFFFFFFFF", days_ago=31)
    await seed.executions("P" * 35)

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    (row,) = await seed.scores()
    assert row.runs_analyzed == 30
    assert row.last_failure_at is None
    assert len(row.recent_outcomes) == 30


async def test_same_name_different_test_type_kept_apart(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("PFPFPF", test_type="e2e")
    await seed.executions("PPPPPP", test_type="api")

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    assert [(r.test_type, r.level) for r in await seed.scores()] == [("api", 1), ("e2e", 5)]


async def test_history_spans_product_versions(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("PFP", version="1.0.0", days_ago=1)
    await seed.executions("FPF", version="2.0.0")

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    (row,) = await seed.scores()
    assert row.runs_analyzed == 6
    assert row.last_suite_id == seed.suites[("e2e", "2.0.0")].id


async def test_same_timestamp_is_ordered_by_run_id(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("PFPFFPPF", same_timestamp=True)

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)
    first = [item["run_id"] for item in (await seed.scores())[0].recent_outcomes]
    db.expire_all()
    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)
    second = [item["run_id"] for item in (await seed.scores())[0].recent_outcomes]

    assert first == second
    assert first == sorted(first)


async def test_recent_outcomes_payload(db: AsyncSession, seed: Seed) -> None:
    runs = await seed.executions("PRBF" + "P")

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    (row,) = await seed.scores()
    assert [item["outcome"] for item in row.recent_outcomes] == ["P", "R", "F", "F", "P"]
    assert [item["run_id"] for item in row.recent_outcomes] == [str(run.id) for run in runs]
    assert datetime.fromisoformat(row.recent_outcomes[-1]["ts"]) == runs[-1].timestamp_start
    assert row.last_failure_at == runs[3].timestamp_start


async def test_stale_rows_are_removed(db: AsyncSession, seed: Seed) -> None:
    db.add(
        FlakyScore(
            test_object=seed.test_object,
            test_type="e2e",
            scenario_name="gone",
            env=EnvEnum.UAT,
            score=10,
            level=2,
            state="OK",
            flip_rate=0.1,
            retry_pass_rate=0,
            runs_analyzed=9,
            recent_outcomes=[],
            computed_at=NOW,
        )
    )
    await db.commit()

    await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)

    assert await seed.scores() == []


async def test_status_row_updated(db: AsyncSession, seed: Seed) -> None:
    await seed.executions("PPPPP")

    status = await recompute_scores(db, trigger="SCHEDULE", requested_by=None, now=NOW)

    assert (status.id, status.last_trigger, status.last_requested_by, status.last_error) == (
        1,
        "SCHEDULE",
        None,
        None,
    )
    assert status.last_success_at == NOW
    assert status.scored_count >= 1

    status = await recompute_scores(
        db, trigger="MANUAL", requested_by="me@x.it", now=NOW + timedelta(hours=1)
    )
    assert (status.last_trigger, status.last_requested_by) == ("MANUAL", "me@x.it")


async def test_failure_is_recorded_and_reraised(
    db: AsyncSession, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    await recompute_scores(db, trigger="SCHEDULE", requested_by=None, now=NOW)
    monkeypatch.setattr(
        flaky_svc, "load_execution_points", AsyncMock(side_effect=RuntimeError("db down"))
    )

    with pytest.raises(RuntimeError, match="db down"):
        await recompute_scores(
            db, trigger="MANUAL", requested_by="me@x.it", now=NOW + timedelta(hours=1)
        )

    async with TestSession() as other:
        status = await other.get(FlakyRecomputeStatus, 1)
    assert status is not None
    assert status.last_error == "db down"
    assert status.last_success_at == NOW
    assert status.last_attempt_at == NOW + timedelta(hours=1)


async def test_concurrent_recompute_raises(db: AsyncSession, seed: Seed) -> None:
    async with TestSession() as holder:
        await holder.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": RECOMPUTE_LOCK_ID})
        with pytest.raises(FlakyRecomputeInProgressError):
            await recompute_scores(db, trigger="MANUAL", requested_by=None, now=NOW)
        await holder.rollback()


# ── Queries and flags ────────────────────────────────────────────────────


def _score(seed: Seed, name: str, **values: object) -> FlakyScore:
    defaults: dict[str, object] = {
        "test_object": seed.test_object,
        "test_type": "e2e",
        "scenario_name": name,
        "env": EnvEnum.UAT,
        "score": 0,
        "level": 1,
        "state": "OK",
        "flip_rate": 0.0,
        "retry_pass_rate": 0.0,
        "runs_analyzed": 20,
        "recent_outcomes": [],
        "computed_at": NOW,
    }
    return FlakyScore(**{**defaults, **values})


def _flag(seed: Seed, name: str, is_flaky: bool = True) -> FlakyRegistry:
    return FlakyRegistry(
        test_object=seed.test_object,
        test_type="e2e",
        scenario_name=name,
        is_flaky=is_flaky,
        flagged_by="a@x.it" if is_flaky else None,
        flagged_at=NOW if is_flaky else None,
        updated_at=NOW,
    )


async def _list(db: AsyncSession, seed: Seed, **filters: object) -> list[FlakyScenarioRowOut]:
    params: dict[str, object] = {
        "env": EnvEnum.UAT,
        "test_objects": [seed.test_object],
        "flag": "all",
        "min_level": None,
        "hint": None,
        "q": None,
        "page": 1,
        "page_size": 50,
        "scenario_names": None,
    }
    return (await list_scenarios(db, **{**params, **filters})).items  # type: ignore[arg-type]


@pytest.fixture
async def hint_rows(db: AsyncSession, seed: Seed) -> Seed:
    db.add_all(
        [
            _score(seed, "to-mark", score=68, level=5),
            _score(seed, "level-3-unflagged", score=22, level=3),
            _score(seed, "to-unmark", score=0, level=1, runs_analyzed=10),
            _score(seed, "flagged-level-1-few-runs", score=0, level=1, runs_analyzed=9),
            _score(seed, "broken", score=4, level=1, state="BROKEN"),
            _score(seed, "other-env", score=68, level=5, env=EnvEnum.DEV),
            _flag(seed, "to-unmark"),
            _flag(seed, "flagged-level-1-few-runs"),
            _flag(seed, "flagged-no-score"),
            _flag(seed, "unflagged-no-score", is_flaky=False),
        ]
    )
    await db.commit()
    return seed


async def test_list_hints(db: AsyncSession, hint_rows: Seed) -> None:
    rows = {row.scenario_name: row for row in await _list(db, hint_rows)}
    assert {name: row.hint for name, row in rows.items()} == {
        "to-mark": "to_mark",
        "level-3-unflagged": None,
        "to-unmark": "to_unmark",
        "flagged-level-1-few-runs": None,
        "broken": "broken",
        "flagged-no-score": None,
    }


async def test_list_includes_flagged_without_score(db: AsyncSession, hint_rows: Seed) -> None:
    (row,) = [r for r in await _list(db, hint_rows) if r.scenario_name == "flagged-no-score"]
    assert (row.state, row.is_flaky, row.level, row.runs_analyzed, row.recent_outcomes) == (
        "INSUFFICIENT_DATA",
        True,
        None,
        0,
        [],
    )
    assert row.env == "UAT"
    assert row.flagged_by == "a@x.it"


async def test_list_filters_and_default_sort(db: AsyncSession, hint_rows: Seed) -> None:
    names = [r.scenario_name for r in await _list(db, hint_rows)]
    assert names[:3] == ["to-mark", "level-3-unflagged", "broken"]
    assert names[-1] == "flagged-no-score"

    assert [r.scenario_name for r in await _list(db, hint_rows, hint="to_mark")] == ["to-mark"]
    assert {r.scenario_name for r in await _list(db, hint_rows, flag="flagged")} == {
        "to-unmark",
        "flagged-level-1-few-runs",
        "flagged-no-score",
    }
    assert "to-unmark" not in {
        r.scenario_name for r in await _list(db, hint_rows, flag="unflagged")
    }
    assert [r.scenario_name for r in await _list(db, hint_rows, min_level=3)] == [
        "to-mark",
        "level-3-unflagged",
    ]
    assert [r.scenario_name for r in await _list(db, hint_rows, q="UNMARK")] == ["to-unmark"]
    # Flagged scenarios without a score in DEV still show up, after the scored ones.
    assert [r.scenario_name for r in await _list(db, hint_rows, env=EnvEnum.DEV)] == [
        "other-env",
        "flagged-level-1-few-runs",
        "flagged-no-score",
        "to-unmark",
    ]


async def test_list_paging(db: AsyncSession, hint_rows: Seed) -> None:
    page = await list_scenarios(
        db,
        env=EnvEnum.UAT,
        test_objects=[hint_rows.test_object],
        flag="all",
        min_level=None,
        hint=None,
        q=None,
        page=2,
        page_size=4,
    )
    assert (page.total, page.page, page.page_size, len(page.items)) == (6, 2, 4, 2)


async def test_summary_ignores_hint_filter(db: AsyncSession, hint_rows: Seed) -> None:
    summary = await get_summary(
        db,
        env=EnvEnum.UAT,
        test_objects=[hint_rows.test_object],
        flag="all",
        min_level=None,
        q=None,
    )
    assert summary.model_dump() == {
        "to_mark": 1,
        "to_unmark": 1,
        "broken": 1,
        "flagged": 3,
        "analyzed": 5,
    }


async def test_set_flag_upsert_and_audit_fields(db: AsyncSession, seed: Seed) -> None:
    key = {"test_object": seed.test_object, "test_type": "e2e", "scenario_name": "S"}
    first = await set_flag(db, FlakyFlagIn(**key, is_flaky=True, user="a@x.it"), now=NOW)
    second = await set_flag(
        db, FlakyFlagIn(**key, is_flaky=False, user="b@x.it"), now=NOW + timedelta(minutes=5)
    )

    assert first.id == second.id
    assert (second.is_flaky, second.flagged_by, second.flagged_at) == (False, "a@x.it", NOW)
    assert (second.updated_by, second.updated_at) == ("b@x.it", NOW + timedelta(minutes=5))
    audits = (
        await db.execute(
            select(AuditLog.action, AuditLog.detail)
            .where(
                AuditLog.resource_type == "flaky_scenario", AuditLog.resource_id == str(first.id)
            )
            .order_by(AuditLog.created_at, AuditLog.action)
        )
    ).all()
    assert sorted(action for action, _ in audits) == ["flaky.mark", "flaky.unmark"]
    assert all(seed.test_object in (detail or "") for _, detail in audits)
    await db.execute(delete(AuditLog).where(AuditLog.resource_id == str(first.id)))
    await db.commit()


async def test_get_history_newest_first_and_eligible_only(db: AsyncSession, seed: Seed) -> None:
    runs = await seed.executions("PF", origin="TAS", trigger=TriggerTypeEnum.TAS)
    await seed.executions("F", trigger=TriggerTypeEnum.MANUAL)
    await seed.executions("F", env=EnvEnum.DEV)

    history = await get_history(
        db, env=EnvEnum.UAT, test_object=seed.test_object, test_type="e2e", scenario_name="S"
    )

    assert [item.run_id for item in history] == [runs[1].id, runs[0].id]
    assert [(item.outcome, item.status, item.test_origin) for item in history] == [
        ("F", "FAILED", "TAS"),
        ("P", "PASSED", "TAS"),
    ]
    assert history[0].test_object_version == "1.0.0"


async def test_list_filters_by_exact_scenario_names(db: AsyncSession, hint_rows: Seed) -> None:
    rows = await _list(db, hint_rows, scenario_names=["to-mark", "broken", "missing"])
    assert [r.scenario_name for r in rows] == ["to-mark", "broken"]
