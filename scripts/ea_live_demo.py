"""Load (or remove) demo test runs to try the «Live» view of the EA Map locally.

Only for local development. Runs of the TAS suites have ``test_origin = "TAS"`` (TAS run
report), the others ``test_origin = "ea-live-demo"``, an NRT run for the Hub (NRT run
report); every run has its scenarios, so the report linked from the «Live» panel is
full. Every demo run belongs to a demo suite (version ``demo``,
owner ``demo``): ``--clear`` removes exactly those runs (scenarios cascade) and the suites.

    uv run python -m scripts.ea_live_demo            # load 7 days of runs in DEV
    uv run python -m scripts.ea_live_demo --clear    # remove them
"""

from __future__ import annotations

import argparse
import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import TextClause, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import async_session
from app.models.test_metrics import EnvEnum, TestRun, TestSuite, TriggerTypeEnum
from app.services.ea.live import run_columns

ORIGIN = "ea-live-demo"
TAS_ORIGIN = "TAS"
VERSION = "demo"
OWNER = "demo"
# test object -> (type, trigger, hours between runs, failure pattern)
OBJECTS: dict[str, tuple[str, TriggerTypeEnum, int, str]] = {
    "wisp": ("NRT", TriggerTypeEnum.CRON, 24, "fails-now"),
    "checkout": ("NRT", TriggerTypeEnum.CRON, 24, "green"),
    "ecommerce-cdc": ("NRT", TriggerTypeEnum.CRON, 24, "recovered"),
    "fdr3": ("NRT", TriggerTypeEnum.CRON, 24, "flaky"),
    "integration/gpd": ("TAS", TriggerTypeEnum.TAS, 8, "green"),
    "integration/cup": ("TAS", TriggerTypeEnum.TAS, 12, "partial-now"),
    "integration/ebollo": ("TAS", TriggerTypeEnum.TAS, 12, "green"),
    "api/checkout-npg": ("TAS", TriggerTypeEnum.CI_PIPELINE, 6, "recovered"),
    "demo-non-mappato": ("TAS", TriggerTypeEnum.MANUAL, 48, "green"),
}


def _outcome(pattern: str, index: int, rng: random.Random) -> tuple[int, int, int]:
    """(passed, failed, total) of the run ``index`` runs ago (0 = the latest)."""
    total = 20
    if pattern == "fails-now" and index == 0:
        return 17, 3, total
    if pattern == "partial-now" and index == 0:
        return 12, 0, total
    if pattern == "recovered" and index in (1, 2):
        return 18, 2, total
    if pattern == "flaky" and rng.random() < 0.4:
        return 19, 1, total
    return total, 0, total


def _insert_sql(values: dict[str, object]) -> TextClause:
    """Plain INSERT with only the given columns: works on databases that miss the NRT
    persistence columns of later migrations (the ORM would write all of them)."""
    names = list(values)
    casts = {"env": "qachub.env_enum", "trigger_type": "qachub.trigger_type_enum"}
    params = ", ".join(f"CAST(:{n} AS {casts[n]})" if n in casts else f":{n}" for n in names)
    return text(f"INSERT INTO qachub.test_runs ({', '.join(names)}) VALUES ({params})")  # noqa: S608


async def _insert_scenarios(
    db: AsyncSession, run_id: object, obj: str, passed: int, failed: int, total: int
) -> None:
    """Scenarios of a demo run, so its report (TAS or NRT) has something to show."""
    name = obj.rsplit("/", 1)[-1]
    for i in range(total):
        status = "PASSED" if i < passed else "FAILED" if i < passed + failed else "SKIPPED"
        await db.execute(
            text(
                "INSERT INTO qachub.test_executions "
                "(id, run_id, status, scenario_name, duration_ms, retries, error_message) "
                "VALUES (:id, :run_id, CAST(:status AS qachub.scenario_status_enum), "
                ":name, :duration, 0, :error)"
            ),
            {
                "id": uuid.uuid4(),
                "run_id": run_id,
                "status": status,
                "name": f"{name} – scenario {i + 1:02d}",
                "duration": 30_000,
                "error": "Demo: expected 200 but was 500" if status == "FAILED" else None,
            },
        )


async def load(days: int, env: EnvEnum) -> int:
    rng = random.Random(42)  # noqa: S311 – demo data, not security
    now = datetime.now(UTC)
    written = 0
    async with async_session() as db:
        with_allure = "allure_url" in await run_columns(db)
        for obj, (ttype, trigger, every, pattern) in OBJECTS.items():
            suite = await db.scalar(
                select(TestSuite).where(
                    TestSuite.test_object == obj,
                    TestSuite.test_type == ttype,
                    TestSuite.test_object_version == VERSION,
                )
            )
            if suite is None:
                suite = TestSuite(
                    test_object=obj, test_type=ttype, test_object_version=VERSION, owner_team=OWNER
                )
                db.add(suite)
                await db.flush()
            index, at = 0, now - timedelta(minutes=rng.randint(20, 90))
            while at > now - timedelta(days=days):
                passed, failed, total = _outcome(pattern, index, rng)
                values = {
                    "id": uuid.uuid4(),
                    "suite_id": suite.id,
                    "scenario_qty": total,
                    "passed_scenario": passed,
                    "failed_scenario": failed,
                    "broken_scenario": 0,
                    "skipped_scenario": total - passed - failed,
                    "timestamp_start": at,
                    "timestamp_end": at + timedelta(minutes=12),
                    "duration_ms": 720_000,
                    "env": env.value,
                    "trigger_type": trigger.value,
                    "test_version": VERSION,
                    "test_origin": TAS_ORIGIN if ttype == "TAS" else ORIGIN,
                }
                if with_allure:
                    values["allure_url"] = (
                        f"https://example.invalid/allure/{obj.replace('/', '-')}/{index}"
                    )
                await db.execute(_insert_sql(values), values)
                await _insert_scenarios(db, values["id"], obj, passed, failed, total)
                written += 1
                index += 1
                at -= timedelta(hours=every)
        await db.commit()
    return written


async def clear() -> int:
    demo_suites = select(TestSuite.id).where(
        TestSuite.test_object_version == VERSION, TestSuite.owner_team == OWNER
    )
    async with async_session() as db:
        result = await db.execute(
            delete(TestRun).where(
                TestRun.suite_id.in_(demo_suites),
                TestRun.test_version == VERSION,
                TestRun.test_origin.in_((ORIGIN, TAS_ORIGIN)),
            )
        )
        await db.execute(
            delete(TestSuite).where(
                TestSuite.test_object_version == VERSION,
                TestSuite.owner_team == OWNER,
                ~select(TestRun.id).where(TestRun.suite_id == TestSuite.id).exists(),
            )
        )
        await db.commit()
        return int(result.rowcount or 0)  # type: ignore[attr-defined]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clear", action="store_true", help="remove the demo runs")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--env", choices=[e.value for e in EnvEnum], default="DEV")
    args = parser.parse_args()
    if args.clear:
        print(f"Rimosse {asyncio.run(clear())} run demo")
    else:
        n = asyncio.run(load(args.days, EnvEnum(args.env)))
        print(f"Caricate {n} run demo in {args.env} (origini {TAS_ORIGIN} e {ORIGIN})")


if __name__ == "__main__":
    main()
