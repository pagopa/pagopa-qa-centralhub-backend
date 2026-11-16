from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest_asyncio
from sqlalchemy import delete, event

from app.models.status_page import SpApplication, SpDeployEvent, SpProduct, SpVersionState
from app.models.test_metrics import TestRun, TestSuite
from app.services.status_page import (
    get_application_detail,
    get_status,
    get_summary,
    run_kind,
    run_outcome,
)
from tests._db import TestSession, test_engine

T0 = datetime(2026, 3, 1, 10, tzinfo=UTC)


def _run(**kw: Any) -> Any:
    base: dict[str, Any] = {
        "revisioned": False,
        "revision_result": "PENDING",
        "failed_scenario": 0,
        "broken_scenario": 0,
        "passed_scenario": 5,
        "test_origin": "NRT",
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_run_kind() -> None:
    assert run_kind("QG") == "QG"
    assert run_kind("TAS") == "TAS"
    assert run_kind(None) == "NRT"
    assert run_kind("CI") == "NRT"


def test_run_outcome_cases() -> None:
    assert run_outcome(_run(revisioned=True, revision_result="OK", failed_scenario=3)) == (
        "PASSED",
        False,
    )
    assert run_outcome(_run(revisioned=True, revision_result="KO", failed_scenario=0)) == (
        "FAILED",
        False,
    )
    assert run_outcome(_run()) == ("PASSED", True)
    assert run_outcome(_run(test_origin="QG", broken_scenario=1)) == ("FAILED", True)
    assert run_outcome(_run(test_origin="TAS", failed_scenario=2)) == ("FAILED", False)
    assert run_outcome(_run(failed_scenario=None, broken_scenario=None)) == ("NEUTRAL", True)


# ---------------------------------------------------------------- DB fixtures


class World:
    def __init__(self) -> None:
        self.tag = uuid.uuid4().hex[:8]
        self.product_ids: list[uuid.UUID] = []
        self.app_ids: list[uuid.UUID] = []
        self.suite_ids: list[uuid.UUID] = []

    def name(self, suffix: str) -> str:
        return f"zz-{self.tag}-{suffix}"


@pytest_asyncio.fixture
async def world() -> AsyncIterator[World]:
    w = World()
    yield w
    async with TestSession() as db:
        if w.suite_ids:
            await db.execute(delete(TestSuite).where(TestSuite.id.in_(w.suite_ids)))
        if w.app_ids:
            await db.execute(delete(SpApplication).where(SpApplication.id.in_(w.app_ids)))
        if w.product_ids:
            await db.execute(delete(SpProduct).where(SpProduct.id.in_(w.product_ids)))
        await db.commit()


async def add_product(w: World, suffix: str, area: str = "CORE") -> uuid.UUID:
    pid = uuid.uuid4()
    async with TestSession() as db:
        db.add(SpProduct(id=pid, name=w.name(suffix), area=area))
        await db.commit()
    w.product_ids.append(pid)
    return pid


async def add_app(
    w: World,
    product_id: uuid.UUID,
    suffix: str,
    test_object: str | None = None,
    states: dict[str, tuple[str, str]] | None = None,
) -> uuid.UUID:
    """states: target -> (version, read_status)."""
    aid = uuid.uuid4()
    async with TestSession() as db:
        db.add(
            SpApplication(
                id=aid,
                product_id=product_id,
                name=w.name(suffix),
                product_key=w.name(suffix),
                repository="repo",
                runtime="AKS",
                ci="GHA",
                test_object=test_object,
            )
        )
        await db.flush()
        for target, (version, status) in (states or {}).items():
            db.add(
                SpVersionState(
                    application_id=aid,
                    target=target,
                    version=version,
                    read_status=status,
                    checked_at=T0,
                    last_ok_at=T0,
                    version_since=T0,
                )
            )
        await db.commit()
    w.app_ids.append(aid)
    return aid


async def add_run(
    w: World,
    test_object: str,
    version: str,
    env: str,
    origin: str = "NRT",
    minutes: int = 0,
    failed: int = 0,
) -> uuid.UUID:
    rid = uuid.uuid4()
    async with TestSession() as db:
        from sqlalchemy import select

        suite = (
            await db.execute(
                select(TestSuite).where(
                    TestSuite.test_object == test_object,
                    TestSuite.test_object_version == version,
                    TestSuite.test_type == "e2e",
                )
            )
        ).scalar_one_or_none()
        if suite is None:
            suite = TestSuite(test_object=test_object, test_type="e2e", test_object_version=version)
            db.add(suite)
            await db.flush()
            w.suite_ids.append(cast("uuid.UUID", suite.id))
        db.add(
            TestRun(
                id=rid,
                suite_id=suite.id,
                env=env,
                test_origin=origin,
                timestamp_start=T0 + timedelta(minutes=minutes),
                passed_scenario=5,
                failed_scenario=failed,
                broken_scenario=0,
            )
        )
        await db.commit()
    return rid


def states(repo: str, dev: str, uat: str, prod: str, status: str = "OK") -> dict[str, Any]:
    return {
        "REPO": (repo, "OK"),
        "DEV": (dev, status),
        "UAT": (uat, "OK"),
        "PROD": (prod, "OK"),
    }


async def row(w: World, app_id: uuid.UUID) -> Any:
    async with TestSession() as db:
        out = await get_status(db)
    for area in out.areas:
        for product in area.products:
            for application in product.applications:
                if application.id == app_id:
                    return application
    raise AssertionError("application not found")


# ---------------------------------------------------------------------- tests


async def test_linkage_normalizes_and_filters_env(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.4.2", "1.4.2", "1.4.2", "1.4.2"))
    uat_run = await add_run(world, obj, "v1.4.2", "UAT")
    await add_run(world, obj, "v1.4.2", "PROD", origin="QG")
    await add_run(world, obj, "1.4.1", "UAT", minutes=5)  # other version
    r = await row(world, a)
    assert [t.run_id for t in r.envs["UAT"].tests] == [uat_run]
    assert [t.kind for t in r.envs["PROD"].tests] == ["QG"]
    assert r.envs["DEV"].tests == []
    assert r.repo.tests == [] and r.repo.drift is None


async def test_latest_run_per_kind(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.0.0", "1.0.0", "1.0.0", "1.0.0"))
    await add_run(world, obj, "1.0.0", "UAT", minutes=0)
    newer = await add_run(world, obj, "1.0.0", "UAT", minutes=10)
    await add_run(world, obj, "1.0.0", "UAT", origin="QG", minutes=1)
    await add_run(world, obj, "1.0.0", "UAT", origin="TAS", minutes=2)
    r = await row(world, a)
    tests = r.envs["UAT"].tests
    assert [t.kind for t in tests] == ["NRT", "QG", "TAS"]
    assert tests[0].run_id == newer
    assert tests[0].hub_url == f"/nrts/report/{newer}"
    assert tests[1].hub_url == f"/quality-gate/report/{tests[1].run_id}"
    assert tests[2].hub_url == f"/tas-nrts/report/{tests[2].run_id}"


async def test_shared_test_object(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a1 = await add_app(world, p, "a1", obj, states("1.0.0", "1.0.0", "1.0.0", "1.0.0"))
    a2 = await add_app(world, p, "a2", obj, states("1.0.0", "1.0.0", "1.0.0", "1.0.0"))
    rid = await add_run(world, obj, "1.0.0", "UAT")
    for a in (a1, a2):
        assert [t.run_id for t in (await row(world, a)).envs["UAT"].tests] == [rid]


async def test_no_test_object_never_untested(world: World) -> None:
    p = await add_product(world, "p")
    a = await add_app(world, p, "a", None, states("1.0.0", "1.0.0", "1.0.0", "1.0.0"))
    r = await row(world, a)
    assert r.envs["UAT"].tests == []
    assert "UNTESTED" not in r.flags
    assert "ALIGNED" in r.flags


async def test_flags(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    aligned = await add_app(world, p, "aligned", obj, states("1.0.0", "1.0.0", "1.0.0", "1.0.0"))
    await add_run(world, obj, "1.0.0", "UAT")
    await add_run(world, obj, "1.0.0", "PROD")
    behind = await add_app(world, p, "behind", None, states("1.1.0", "1.1.0", "1.1.0", "1.0.0"))
    dev_ahead = await add_app(
        world, p, "devahead", None, states("1.0.0", "1.1.0", "1.0.0", "1.0.0")
    )
    broken = await add_app(
        world, p, "broken", None, states("1.0.0", "1.0.0", "1.0.0", "1.0.0", status="ERROR")
    )
    untested = await add_app(
        world, p, "untested", world.name("other"), states("1.0.0", "1.0.0", "1.0.0", "1.0.0")
    )
    assert (await row(world, aligned)).flags == ["ALIGNED"]
    assert (await row(world, behind)).flags == ["DRIFT"]
    assert (await row(world, dev_ahead)).flags == ["ALIGNED"]
    b = await row(world, broken)
    assert "UNREACHABLE" in b.flags and "ALIGNED" not in b.flags
    assert "UNTESTED" in (await row(world, untested)).flags
    assert (await row(world, behind)).envs["PROD"].drift == "BEHIND"


async def test_status_grouping_and_query_count(world: World) -> None:
    pb = await add_product(world, "b", "CORE")
    pa = await add_product(world, "a", "CORE")
    pt = await add_product(world, "t", "TOUCHPOINT")
    obj = world.name("obj")
    for i, prod in enumerate((pb, pa, pt)):
        for name in ("y", "x"):
            st = states("1.0.0", "1.0.0", "1.0.0", "1.0.0")
            await add_app(world, prod, f"{i}{name}", obj, st)
    await add_run(world, obj, "1.0.0", "UAT")

    count = 0

    def _count(*_: Any) -> None:
        nonlocal count
        count += 1

    event.listen(test_engine.sync_engine, "before_cursor_execute", _count)
    try:
        async with TestSession() as db:
            out = await get_status(db)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", _count)
    print(f"get_status query count: {count}")
    assert count <= 6

    areas = [a.area for a in out.areas]
    assert areas.index("CORE") < areas.index("TOUCHPOINT")
    core = next(a for a in out.areas if a.area == "CORE")
    names = [p.name for p in core.products]
    assert names == sorted(names)
    mine = [p for p in core.products if p.id in (pa, pb)]
    assert [p.id for p in mine] == [pa, pb]
    for p in mine:
        apps = [a.name for a in p.applications]
        assert apps == sorted(apps)


async def test_summary_matches_flags(world: World) -> None:
    p = await add_product(world, "p")
    await add_app(world, p, "ok", None, states("1.0.0", "1.0.0", "1.0.0", "1.0.0"))
    await add_app(world, p, "drift", None, states("1.1.0", "1.1.0", "1.1.0", "1.0.0"))
    async with TestSession() as db:
        base = await get_summary(db)
        out = await get_status(db)
    flags = [f for a in out.areas for pr in a.products for ap in pr.applications for f in ap.flags]
    assert base.total == sum(len(pr.applications) for a in out.areas for pr in a.products)
    assert base.aligned == flags.count("ALIGNED")
    assert base.drift == flags.count("DRIFT")
    assert base.unreachable == flags.count("UNREACHABLE")
    assert base.untested == flags.count("UNTESTED")
    assert base.aligned >= 1 and base.drift >= 1


async def test_detail_history(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.4.2", "1.4.2", "1.4.1", "1.4.2"))
    async with TestSession() as db:
        evs = [
            ("UAT", None, "1.4.1", 0, True),
            ("UAT", "1.4.1", "1.4.2", 10, False),
            ("UAT", "1.4.2", "1.4.1", 20, False),
            ("UAT", "1.4.1", "nightly", 30, False),
        ]
        for target, prev, ver, mins, base in evs:
            db.add(
                SpDeployEvent(
                    application_id=a,
                    target=target,
                    previous_version=prev,
                    version=ver,
                    detected_at=T0 + timedelta(minutes=mins),
                    is_baseline=base,
                )
            )
        await db.commit()
    for i in range(22):
        await add_run(world, obj, "1.4.1", "UAT", minutes=i)
    await add_run(world, obj, "1.4.1", "DEV")
    async with TestSession() as db:
        detail = await get_application_detail(db, a)
    assert detail is not None
    assert detail.product_name == world.name("p")
    uat = detail.history["UAT"]
    assert [e.version for e in uat] == ["nightly", "1.4.1", "1.4.2", "1.4.1"]
    assert [e.direction for e in uat] == ["unknown", "rollback", "upgrade", "baseline"]
    assert len(uat[1].runs) == 20 and len(uat[3].runs) == 20
    stamps = [r.timestamp_start for r in uat[1].runs if r.timestamp_start is not None]
    assert stamps == sorted(stamps, reverse=True)
    assert uat[0].runs == []
    assert detail.history["DEV"] == [] and detail.history["REPO"] == []
    assert detail.application.id == a
    async with TestSession() as db:
        assert await get_application_detail(db, uuid.uuid4()) is None
