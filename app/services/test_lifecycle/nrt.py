"""Collegamento scenari ↔ esecuzioni NRT (fase 4, spec §9).

Il match avviene sul tag identità dello scenario senza `@` (o su un suo alias) contro
`test_executions.tags`, colonna generata dalle label Allure `tag`.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.flaky import FlakyRegistry
from app.models.test_lifecycle import (
    TlFeature,
    TlFeatureStatus,
    TlScenario,
    TlScenarioAlias,
    TlScenarioContent,
)
from app.models.test_metrics import TestExecution, TestRun
from app.schemas.test_lifecycle import (
    TlFeatureNrt,
    TlNrtEnvResult,
    TlNrtRunLink,
    TlScenarioNrt,
)

COUNTED = {"PASSED", "FAILED", "BROKEN"}


def bare(tag: str) -> str:
    return tag.lstrip("@")


def run_status(statuses: list[str]) -> str:
    """Esito di uno scenario in una run (outline: più esecuzioni)."""
    for worst in ("FAILED", "BROKEN"):
        if worst in statuses:
            return worst
    return "PASSED" if "PASSED" in statuses else "SKIPPED"


@dataclass
class _RunResult:
    run_id: uuid.UUID
    env: str
    run_at: datetime | None
    origin: str | None
    allure_url: str | None
    statuses: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return run_status(self.statuses)


def _value(enum_or_str: Any) -> str | None:
    return None if enum_or_str is None else getattr(enum_or_str, "value", enum_or_str)


async def _tag_map(db: AsyncSession, scenario_ids: list[uuid.UUID]) -> dict[str, uuid.UUID]:
    """Tag (senza @) e alias → scenario."""
    if not scenario_ids:
        return {}
    out: dict[str, uuid.UUID] = {}
    stmt = select(TlScenario.id, TlScenario.tag).where(TlScenario.id.in_(scenario_ids))
    for sid, tag in (await db.execute(stmt)).all():
        out[bare(tag)] = sid
    for sid, alias in (
        await db.execute(
            select(TlScenarioAlias.scenario_id, TlScenarioAlias.alias).where(
                TlScenarioAlias.scenario_id.in_(scenario_ids)
            )
        )
    ).all():
        out.setdefault(bare(alias), sid)
    return out


async def _executions(
    db: AsyncSession,
    tags: list[str],
    *,
    run_ids: list[uuid.UUID] | None = None,
    since: datetime | None = None,
) -> list[Any]:
    """Esecuzioni con almeno uno dei tag, dalla più recente."""
    if not tags:
        return []
    stmt = (
        select(
            TestExecution.id,
            TestExecution.tags,
            TestExecution.status,
            TestExecution.scenario_name,
            TestExecution.allure_url,
            TestRun.id.label("run_id"),
            TestRun.env,
            TestRun.timestamp_start,
            TestRun.test_origin,
            TestRun.allure_url.label("run_allure_url"),
        )
        .join(TestRun, TestRun.id == TestExecution.run_id)
        .where(TestExecution.tags.has_any(array(tags)))
        .order_by(TestRun.timestamp_start.desc().nulls_last(), TestRun.id)
    )
    if run_ids is not None:
        stmt = stmt.where(TestRun.id.in_(run_ids))
    if since is not None:
        stmt = stmt.where(TestRun.timestamp_start >= since)
    return list((await db.execute(stmt)).all())


def _group(rows: list[Any], tag_map: dict[str, uuid.UUID]) -> dict[uuid.UUID, list[_RunResult]]:
    """Per scenario, le run (dalla più recente) con gli stati delle sue esecuzioni."""
    per: dict[uuid.UUID, dict[uuid.UUID, _RunResult]] = defaultdict(dict)
    for row in rows:
        sids = {tag_map[t] for t in (row.tags or []) if t in tag_map}
        for sid in sids:
            res = per[sid].get(row.run_id)
            if res is None:
                res = per[sid][row.run_id] = _RunResult(
                    run_id=row.run_id,
                    env=_value(row.env) or "—",
                    run_at=row.timestamp_start,
                    origin=row.test_origin,
                    allure_url=row.allure_url or row.run_allure_url,
                )
            status = _value(row.status)
            if status:
                res.statuses.append(status)
    return {sid: list(runs.values()) for sid, runs in per.items()}  # ordine d'inserimento = recenti


def _window_counts(runs: list[_RunResult]) -> tuple[int, int, int]:
    """(run nella finestra, passate, contate) sulle ultime `tl_nrt_window` run."""
    window = runs[: settings.tl_nrt_window]
    statuses = [r.status for r in window]
    counted = [s for s in statuses if s in COUNTED]
    return len(window), counted.count("PASSED"), len(counted)


def _rate(passed: int, counted: int) -> float | None:
    return passed / counted if counted else None


async def _scenario_rows(db: AsyncSession, feature: TlFeature) -> list[Any]:
    revision_id = feature.published_revision_id or feature.working_revision_id
    if revision_id is None:
        return []
    return list(
        (
            await db.execute(
                select(TlScenario.id, TlScenario.number, TlScenario.tag, TlScenarioContent.title)
                .join(TlScenarioContent, TlScenarioContent.scenario_id == TlScenario.id)
                .where(TlScenarioContent.revision_id == revision_id)
                .order_by(TlScenarioContent.position)
            )
        ).all()
    )


async def _flaky_names(db: AsyncSession, names: set[str]) -> set[str]:
    if not names:
        return set()
    rows = await db.execute(
        select(FlakyRegistry.scenario_name).where(
            FlakyRegistry.scenario_name.in_(names), FlakyRegistry.is_flaky.is_(True)
        )
    )
    return set(rows.scalars())


async def feature_nrt(db: AsyncSession, feature_id: uuid.UUID) -> TlFeatureNrt:
    """Esiti NRT per scenario della revisione pubblicata (altrimenti di lavoro)."""
    from app.services.test_lifecycle import features as features_svc

    feature = await db.get(TlFeature, feature_id)
    if feature is None:
        raise features_svc.FeatureNotFoundError(str(feature_id))
    scenarios = await _scenario_rows(db, feature)
    tag_map = await _tag_map(db, [s.id for s in scenarios])
    rows = await _executions(db, list(tag_map))
    runs_by = _group(rows, tag_map)
    names: dict[uuid.UUID, set[str]] = defaultdict(set)
    for row in rows:
        for t in row.tags or []:
            if t in tag_map and row.scenario_name:
                names[tag_map[t]].add(row.scenario_name)
    flaky = await _flaky_names(db, {n for ns in names.values() for n in ns})

    out: list[TlScenarioNrt] = []
    total_passed = total_counted = 0
    last: datetime | None = None
    for sc in scenarios:
        runs = runs_by.get(sc.id, [])
        latest: dict[str, _RunResult] = {}
        for r in runs:
            latest.setdefault(r.env, r)
        n_runs, passed, counted = _window_counts(runs)
        total_passed += passed
        total_counted += counted
        sc_last = runs[0].run_at if runs else None
        if sc_last and (last is None or sc_last > last):
            last = sc_last
        out.append(
            TlScenarioNrt(
                scenario_id=sc.id,
                number=sc.number,
                tag=sc.tag,
                title=sc.title,
                envs=[
                    TlNrtEnvResult(
                        env=r.env, status=r.status, run_id=r.run_id, run_at=r.run_at,
                        allure_url=r.allure_url, origin=r.origin,
                    )
                    for r in sorted(latest.values(), key=lambda r: r.env)
                ],
                pass_rate=_rate(passed, counted),
                runs=n_runs,
                flaky=bool(names.get(sc.id, set()) & flaky),
                last_run_at=sc_last,
            )
        )
    return TlFeatureNrt(
        feature_id=feature.id, last_run_at=last, pass_rate=_rate(total_passed, total_counted),
        scenarios=out,
    )


async def _all_tag_map(db: AsyncSession) -> tuple[dict[str, uuid.UUID], dict[uuid.UUID, Any]]:
    """Tutti i tag/alias → scenario, e scenario → (feature_id, code, scope_id, tag)."""
    rows = (
        await db.execute(
            select(TlScenario.id, TlScenario.tag, TlFeature.id, TlFeature.code, TlFeature.scope_id)
            .join(TlFeature, TlFeature.id == TlScenario.feature_id)
        )
    ).all()
    info = {r[0]: r for r in rows}
    tag_map = {bare(r[1]): r[0] for r in rows}
    aliases = await db.execute(select(TlScenarioAlias.scenario_id, TlScenarioAlias.alias))
    for sid, alias in aliases.all():
        tag_map.setdefault(bare(alias), sid)
    return tag_map, info


async def run_feature_links(db: AsyncSession, run_id: uuid.UUID) -> list[TlNrtRunLink]:
    """Esecuzioni di una run collegate a una feature dell'hub."""
    tag_map, info = await _all_tag_map(db)
    rows = await _executions(db, list(tag_map), run_ids=[run_id])
    links: list[TlNrtRunLink] = []
    for row in rows:
        for t in row.tags or []:
            sid = tag_map.get(t)
            if sid is None:
                continue
            _sid, tag, fid, code, _scope = info[sid]
            links.append(
                TlNrtRunLink(
                    execution_id=row.id, feature_id=fid, feature_code=code, scenario_tag=tag
                )
            )
            break
    return links


async def scope_pass_rates(db: AsyncSession) -> dict[uuid.UUID, float | None]:
    """Pass rate NRT per ambito sulle finestre dei suoi scenari (ultimi `lookback` giorni)."""
    tag_map, info = await _all_tag_map(db)
    since = datetime.now(UTC) - timedelta(days=settings.tl_nrt_lookback_days)
    runs_by = _group(await _executions(db, list(tag_map), since=since), tag_map)
    acc: dict[uuid.UUID, list[int]] = defaultdict(lambda: [0, 0])
    for sid, runs in runs_by.items():
        _n, passed, counted = _window_counts(runs)
        scope_id = info[sid][4]
        acc[scope_id][0] += passed
        acc[scope_id][1] += counted
    return {scope_id: _rate(p, c) for scope_id, (p, c) in acc.items()}


# ── RUNNING automatico ────────────────────────────────────────────────────────

RUNNING_LOCK_KEY = "tl_nrt_running"


async def promote_running(
    db: AsyncSession, *, run_ids: list[uuid.UUID] | None = None, limit: int = 500
) -> int:
    """IMPLEMENTED → RUNNING per le feature con almeno uno scenario eseguito (spec §6).

    `run_ids` restringe alle run indicate (ingest); senza, guarda tutte le esecuzioni.
    Mai all'indietro; un evento `tl.feature.running` per feature. Commit per feature.
    """
    from app.services.test_lifecycle import features as features_svc
    from app.services.test_lifecycle import history

    rows = (
        await db.execute(
            select(TlScenario.id, TlScenario.tag, TlScenario.feature_id)
            .join(TlFeature, TlFeature.id == TlScenario.feature_id)
            .where(TlFeature.status == TlFeatureStatus.IMPLEMENTED)
        )
    ).all()
    if not rows:
        return 0
    feature_of = {r.id: r.feature_id for r in rows}
    tag_map = await _tag_map(db, list(feature_of))
    first: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
    for ex in await _executions(db, list(tag_map), run_ids=run_ids):
        for t in ex.tags or []:
            sid = tag_map.get(t)
            if sid is not None:
                first.setdefault(feature_of[sid], (ex.run_id, t))
    await db.rollback()

    promoted = 0
    for feature_id, (run_id, tag) in list(first.items())[:limit]:
        try:
            feature = await features_svc._lock_feature(db, feature_id)
        except features_svc.FeatureNotFoundError:
            await db.rollback()
            continue
        if feature.status != TlFeatureStatus.IMPLEMENTED:  # già promossa o cambiata nel frattempo
            await db.rollback()
            continue
        feature.status = TlFeatureStatus.RUNNING
        feature.running_at = datetime.now(UTC)
        history.record_event(
            db, feature.id, "tl.feature.running", None, {"run_id": str(run_id), "tag": tag}
        )
        await db.commit()
        promoted += 1
    return promoted
