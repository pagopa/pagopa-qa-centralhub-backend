"""«Mappa viva»: recent outcomes of the automated test runs (NRT, TAS, CI…) on the map.

The runs are those the Hub already stores (``qachub.test_runs`` with their suite and
``test_object``). A test object is tied to the components that the map already knows it
covers: the NRT suites and central suites of each component's QA signals, or the component
or product with the same name. Objects that match nothing are returned apart, so a
missing link is visible instead of silently dropped.

Read-only and computed on demand; :func:`summarise` is pure and tested on its own.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.test_metrics import EnvEnum, TestRun, TestSuite
from app.services.ea.graph_index import GraphIndex

FRESH_HOURS = 24
HISTORY = 10
FEED = 15
STATUS_ORDER = {"failed": 3, "partial": 2, "passed": 1, "none": 0}


@dataclass(frozen=True)
class RunRow:
    test_object: str
    test_type: str | None
    trigger: str | None
    env: str | None
    started_at: datetime | None
    total: int
    passed: int
    failed: int
    broken: int
    skipped: int
    duration_ms: int | None
    allure_url: str | None


def run_status(r: RunRow) -> str:
    if r.failed + r.broken > 0:
        return "failed"
    if r.passed > 0 and r.passed >= r.total - r.skipped:
        return "passed"
    return "partial"


def object_nodes(g: GraphIndex, test_object: str) -> list[str]:
    """Components (or the product) a test object covers, from what the map already knows."""
    key = test_object.strip().lower()
    hits: list[str] = []
    for n in g.nodes.values():
        if n["type"] != "component":
            continue
        qa = n.get("qa") or {}
        nrt = [x.lower() for x in qa.get("nrt") or []]
        central = [x.lower() for x in qa.get("central") or []]
        if key in nrt or key in central or any(c.rsplit("/", 1)[-1] == key for c in central):
            hits.append(n["id"])
    if hits:
        return sorted(hits)
    node = g.resolve(key)
    return [node] if node is not None and g.nodes[node]["type"] in ("component", "product") else []


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: STATUS_ORDER[s]) if statuses else "none"


def summarise(g: GraphIndex, rows: list[RunRow], now: datetime) -> dict[str, Any]:
    by_object: dict[str, list[RunRow]] = defaultdict(list)
    for r in rows:
        by_object[r.test_object].append(r)

    objects: list[dict[str, Any]] = []
    unmapped: list[str] = []
    node_objects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    fresh_limit = now - timedelta(hours=FRESH_HOURS)
    for obj, runs in by_object.items():
        runs.sort(key=lambda r: r.started_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        last = runs[0]
        statuses = [run_status(r) for r in runs]
        nodes = object_nodes(g, obj)
        entry = {
            "test_object": obj,
            "test_type": last.test_type,
            "trigger": last.trigger,
            "status": statuses[0],
            "at": last.started_at.isoformat() if last.started_at else None,
            "fresh_failure": statuses[0] == "failed"
            and bool(last.started_at and last.started_at >= fresh_limit),
            "runs": len(runs),
            "pass_rate": round(statuses.count("passed") / len(runs), 2),
            "history": list(reversed(statuses[:HISTORY])),
            "last": {
                "total": last.total,
                "passed": last.passed,
                "failed": last.failed,
                "broken": last.broken,
                "skipped": last.skipped,
                "duration_ms": last.duration_ms,
                "allure_url": last.allure_url,
            },
            "nodes": nodes,
        }
        objects.append(entry)
        if not nodes:
            unmapped.append(obj)
        for nid in nodes:
            node_objects[nid].append(entry)

    nodes_out = {
        nid: {
            "status": _worst([e["status"] for e in ents]),
            "fresh_failure": any(e["fresh_failure"] for e in ents),
            "at": max((e["at"] for e in ents if e["at"]), default=None),
            "objects": sorted(e["test_object"] for e in ents),
        }
        for nid, ents in node_objects.items()
    }
    feed = sorted(
        (
            {
                "test_object": r.test_object,
                "status": run_status(r),
                "trigger": r.trigger,
                "at": r.started_at.isoformat() if r.started_at else None,
                "failed": r.failed + r.broken,
                "total": r.total,
                "allure_url": r.allure_url,
                "nodes": object_nodes(g, r.test_object),
            }
            for r in rows
        ),
        key=lambda x: x["at"] or "",
        reverse=True,
    )[:FEED]
    counts = {
        s: sum(1 for o in objects if o["status"] == s) for s in ("passed", "partial", "failed")
    }
    objects.sort(key=lambda o: (-STATUS_ORDER[o["status"]], o["test_object"]))
    return {
        "objects": objects,
        "nodes": nodes_out,
        "unmapped": sorted(unmapped),
        "feed": feed,
        "counts": counts,
    }


async def run_columns(db: AsyncSession) -> set[str]:
    """Columns of ``qachub.test_runs`` in this database. Databases created before the NRT
    persistence migration have no ``allure_url``: the live view still works without it."""
    rows = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'qachub' AND table_name = 'test_runs'"
        )
    )
    return {r[0] for r in rows}


async def load_runs(db: AsyncSession, env: str, since: datetime) -> list[RunRow]:
    cols = [
        TestRun.trigger_type,
        TestRun.env,
        TestRun.timestamp_start,
        TestRun.scenario_qty,
        TestRun.passed_scenario,
        TestRun.failed_scenario,
        TestRun.broken_scenario,
        TestRun.skipped_scenario,
        TestRun.duration_ms,
    ]
    with_allure = "allure_url" in await run_columns(db)
    if with_allure:
        cols.append(TestRun.allure_url)
    stmt = (
        select(TestSuite.test_object, TestSuite.test_type, *cols)
        .join(TestSuite, TestRun.suite_id == TestSuite.id)
        .where(TestRun.env == EnvEnum(env), TestRun.timestamp_start >= since)
        .order_by(TestRun.timestamp_start.desc())
        .limit(2000)
    )
    out: list[RunRow] = []
    for row in (await db.execute(stmt)).all():
        m = row._mapping
        out.append(
            RunRow(
                test_object=m["test_object"],
                test_type=m["test_type"],
                trigger=m["trigger_type"].value if m["trigger_type"] else None,
                env=m["env"].value if m["env"] else None,
                started_at=m["timestamp_start"],
                total=m["scenario_qty"] or 0,
                passed=m["passed_scenario"] or 0,
                failed=m["failed_scenario"] or 0,
                broken=m["broken_scenario"] or 0,
                skipped=m["skipped_scenario"] or 0,
                duration_ms=m["duration_ms"],
                allure_url=m["allure_url"] if with_allure else None,
            )
        )
    return out


async def live_status(db: AsyncSession, g: GraphIndex, env: str, days: int) -> dict[str, Any]:
    now = datetime.now(UTC)
    rows = await load_runs(db, env, now - timedelta(days=days))
    return {"env": env, "days": days, "generated_at": now.isoformat(), **summarise(g, rows, now)}
