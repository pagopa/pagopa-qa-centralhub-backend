from __future__ import annotations

# ruff: noqa: S101
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import pytest_asyncio
from sqlalchemy import delete, event

from app.config import settings
from app.models.status_page import SpApplication, SpDeployEvent, SpProduct, SpVersionState
from app.models.test_metrics import TestRun, TestSuite
from app.services import status_page as read_model
from app.services.status_page import (
    VersionNotFoundError,
    _run_ref,
    get_application_detail,
    get_status,
    get_summary,
    get_version,
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


@pytest.fixture
def match_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "status_page_runs_match_version", True)


async def add_event(
    app_id: uuid.UUID,
    target: str,
    version: str,
    detected: int,
    previous: str | None = None,
    previous_seen: int | None = None,
) -> None:
    """Deploy event at T0 + ``detected`` minutes (a baseline when ``previous`` is None)."""
    async with TestSession() as db:
        db.add(
            SpDeployEvent(
                application_id=app_id,
                target=target,
                previous_version=previous,
                version=version,
                detected_at=T0 + timedelta(minutes=detected),
                previous_seen_at=(
                    T0 + timedelta(minutes=previous_seen) if previous_seen is not None else None
                ),
                is_baseline=previous is None,
            )
        )
        await db.commit()


# ---------------------------------------------------------------------- tests


async def test_linkage_normalizes_and_filters_env(world: World, match_version: None) -> None:
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


async def test_detail_history(world: World, match_version: None) -> None:
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


def test_run_ref_failed_counts_broken_scenarios() -> None:
    def ref(failed: int | None, broken: int | None) -> Any:
        return _run_ref(
            _run(
                id=uuid.uuid4(),
                failed_scenario=failed,
                broken_scenario=broken,
                trigger_type=None,
                timestamp_start=T0,
                allure_url=None,
                gha_run_url=None,
            )
        )

    assert ref(1, 2).failed == 3
    assert ref(None, 2).failed == 2
    assert ref(1, None).failed == 1
    assert ref(None, None).failed is None


async def test_flags_prod_ahead_is_drift(world: World) -> None:
    p = await add_product(world, "p")
    a = await add_app(world, p, "ahead", None, states("1.0.0", "1.0.0", "1.0.0", "1.1.0"))
    r = await row(world, a)
    assert r.envs["PROD"].drift == "AHEAD"
    assert r.flags == ["DRIFT"]


async def test_flags_uat_or_prod_error_is_unreachable(world: World) -> None:
    p = await add_product(world, "p")
    for target in ("UAT", "PROD"):
        st = states("1.0.0", "1.0.0", "1.0.0", "1.0.0")
        st[target] = ("1.0.0", "ERROR")
        a = await add_app(world, p, f"err-{target}", None, st)
        r = await row(world, a)
        assert "UNREACHABLE" in r.flags, target
        assert "ALIGNED" not in r.flags, target


async def test_never_synced_app_has_null_cells_and_no_flags(world: World) -> None:
    p = await add_product(world, "p")
    a = await add_app(world, p, "fresh", world.name("obj"))
    r = await row(world, a)
    assert r.repo is None
    assert all(r.envs[t] is None for t in ("DEV", "UAT", "PROD"))
    assert "UNREACHABLE" not in r.flags
    assert "ALIGNED" not in r.flags


async def test_runs_of_non_deployed_versions_are_not_loaded(
    world: World, monkeypatch: pytest.MonkeyPatch, match_version: None
) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.1.0", "1.0.0", "1.0.0", "1.0.0"))
    deployed = await add_run(world, obj, "v1.0.0", "UAT")
    await add_run(world, obj, "0.9.0", "UAT", minutes=5)
    await add_run(world, obj, "1.1.0", "UAT", minutes=6)  # REPO only, not deployed

    loaded: list[Any] = []
    original = read_model._fetch_runs

    async def spy(*args: Any, **kwargs: Any) -> Any:
        index = await original(*args, **kwargs)
        loaded.extend(index.items())
        return index

    monkeypatch.setattr(read_model, "_fetch_runs", spy)
    r = await row(world, a)
    assert [t.run_id for t in r.envs["UAT"].tests] == [deployed]
    mine = {key: runs for key, runs in loaded if key[0] == obj}
    assert set(mine) == {(obj, "1.0.0", "UAT")}
    assert [run.id for run in mine[(obj, "1.0.0", "UAT")]] == [deployed]


async def test_detail_loads_runs_of_event_versions_only(
    world: World, monkeypatch: pytest.MonkeyPatch, match_version: None
) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.2.0", "1.2.0", "1.2.0", "1.2.0"))
    async with TestSession() as db:
        db.add(
            SpDeployEvent(
                application_id=a,
                target="UAT",
                previous_version="1.1.0",
                version="1.2.0",
                detected_at=T0,
                is_baseline=False,
            )
        )
        db.add(
            SpDeployEvent(
                application_id=a,
                target="UAT",
                previous_version=None,
                version="1.1.0",
                detected_at=T0 - timedelta(days=1),
                is_baseline=True,
            )
        )
        await db.commit()
    old = await add_run(world, obj, "1.1.0", "UAT")
    await add_run(world, obj, "1.0.0", "UAT")  # never deployed

    loaded: list[Any] = []
    original = read_model._fetch_runs

    async def spy(*args: Any, **kwargs: Any) -> Any:
        index = await original(*args, **kwargs)
        loaded.extend(index)
        return index

    monkeypatch.setattr(read_model, "_fetch_runs", spy)
    async with TestSession() as db:
        detail = await get_application_detail(db, a)
    assert detail is not None
    assert [r.run_id for r in detail.history["UAT"][1].runs] == [old]
    assert {key[1] for key in loaded} == {"1.1.0"}


# ------------------------------------------------- runs linked without test_object_version


async def test_cell_links_runs_of_any_version_within_the_deploy_window(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.4.2", "1.4.2", "1.4.2", "1.4.2"))
    await add_event(a, "UAT", "1.4.1", detected=0)
    await add_event(a, "UAT", "1.4.2", detected=20, previous="1.4.1", previous_seen=10)
    await add_run(world, obj, "1.4.2", "UAT", origin="QG", minutes=5)  # before the window
    in_window = await add_run(world, obj, "unknown-version", "UAT", minutes=15)
    dev = await add_run(world, obj, "0.0.1", "DEV", minutes=1)  # DEV has no events: unbounded
    r = await row(world, a)
    assert [t.run_id for t in r.envs["UAT"].tests] == [in_window]
    assert [t.run_id for t in r.envs["DEV"].tests] == [dev]
    assert r.envs["PROD"].tests == []
    assert "UNTESTED" in r.flags  # PROD has no run


async def test_untested_when_no_run_since_the_current_deploy(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.1.0", "1.1.0", "1.1.0", "1.1.0"))
    for target in ("UAT", "PROD"):
        await add_event(a, target, "1.1.0", detected=60, previous="1.0.0", previous_seen=50)
        await add_run(world, obj, "1.1.0", target, minutes=30)  # older deploy
    r = await row(world, a)
    assert r.envs["UAT"].tests == [] and r.envs["PROD"].tests == []
    assert "UNTESTED" in r.flags
    await add_run(world, obj, "whatever", "UAT", minutes=55)
    await add_run(world, obj, "whatever", "PROD", minutes=70)
    assert "UNTESTED" not in (await row(world, a)).flags


async def test_detail_splits_runs_by_deploy_window(world: World) -> None:
    p = await add_product(world, "p")
    obj = world.name("obj")
    a = await add_app(world, p, "a", obj, states("1.4.2", "1.4.2", "1.4.2", "1.4.2"))
    await add_event(a, "UAT", "1.4.1", detected=0)
    await add_event(a, "UAT", "1.4.2", detected=20, previous="1.4.1", previous_seen=10)
    old = await add_run(world, obj, "x", "UAT", minutes=5)
    uncertain = await add_run(world, obj, "x", "UAT", minutes=15)  # between seen and detected
    new = await add_run(world, obj, "x", "UAT", minutes=25)
    async with TestSession() as db:
        detail = await get_application_detail(db, a)
    assert detail is not None
    upgrade, baseline = detail.history["UAT"]
    assert [r.run_id for r in upgrade.runs] == [new, uncertain]
    assert [r.run_id for r in baseline.runs] == [old]
    assert [t.run_id for t in detail.application.envs["UAT"].tests] == [new]


async def test_get_version_returns_state_and_keeps_last_good_on_error(world: World) -> None:
    pid = await add_product(world, "ver")
    await add_app(world, pid, "ver-app", states={"UAT": ("1.2.3", "ERROR")})
    async with TestSession() as db:
        out = await get_version(db, world.name("ver-app"), "UAT")
    assert out.version == "1.2.3"
    assert out.read_status == "ERROR"
    assert out.env == "UAT"
    assert out.checked_at == T0


async def test_get_version_unknown_application_or_environment(world: World) -> None:
    pid = await add_product(world, "ver2")
    await add_app(world, pid, "ver2-app", states={"DEV": ("1.0.0", "OK")})
    async with TestSession() as db:
        with pytest.raises(VersionNotFoundError, match="Applicazione non trovata"):
            await get_version(db, world.name("missing"), "DEV")
        with pytest.raises(VersionNotFoundError, match="non ancora rilevata"):
            await get_version(db, world.name("ver2-app"), "UAT")
