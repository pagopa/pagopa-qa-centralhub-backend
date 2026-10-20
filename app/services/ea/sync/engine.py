"""Weekly EA sync: read GitHub and Confluence, compare with the map, apply or queue.

Phases (docs/ea-map/DESIGN.md §5):

1. *read state* – components, edges, snapshots and open review items from the DB;
2. *collect* – org census, trees and metadata files of the repositories that changed
   since the last snapshot, Confluence page titles (network only, no transaction open);
3. *apply* – in a single transaction: observed metadata, QA signals and rule-based doc
   links are applied immediately (``auto_applied``); new or archived repositories and new
   or vanished Helm dependencies go to the review queue (``pending``); a vanished edge
   that a person verified becomes a ``conflict``.

The sync never changes what people own: names, descriptions, products, component types,
manual QA fields, manual or verified edges, rejected proposals.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import (
    EaChange,
    EaDocLink,
    EaEdge,
    EaMeta,
    EaNode,
    EaQaSignal,
    EaRepoSnapshot,
    EaSyncRun,
    EaSyncStatus,
)
from app.services.ea.sync import derive
from app.services.ea.sync.extract import extract_repo, wanted_files
from app.services.ea.sync.rules import (
    CANDIDATE_MAX_AGE_DAYS,
    CANDIDATE_PREFIXES,
    CONFLUENCE_SPACES,
    GITHUB_ORG,
)
from app.services.ea.sync.sources import (
    ConfluenceSource,
    GitHubSource,
    RepoInfo,
    SourceError,
    list_all_pages,
)

logger = structlog.get_logger(__name__)

SYNC_LOCK_ID = 0x0EA5_1C  # advisory lock taken while creating a run
STALE_AFTER = timedelta(hours=2)
CONCURRENCY = 8
OPEN_STATUSES = ("pending", "conflict")
HELM_CONFIDENCE = 90


class EaSyncBusyError(RuntimeError):
    """Another EA sync is already running."""


def edge_key(from_id: str, to_id: str, edge_type: str) -> str:
    return f"{from_id}>{to_id}:{edge_type}"


def _now() -> datetime:
    return datetime.now(UTC)


# ---- run lifecycle ---------------------------------------------------------------------


async def running_run(db: AsyncSession, now: datetime | None = None) -> EaSyncRun | None:
    now = now or _now()
    run: EaSyncRun | None = await db.scalar(
        select(EaSyncRun)
        .where(EaSyncRun.status == "running", EaSyncRun.started_at > now - STALE_AFTER)
        .order_by(EaSyncRun.started_at.desc())
        .limit(1)
    )
    return run


async def create_run(db: AsyncSession, *, trigger: str, actor: str | None) -> EaSyncRun:
    """Register a new run, refusing if one is already in progress (stale ones are closed)."""
    await db.execute(select(func.pg_advisory_xact_lock(SYNC_LOCK_ID)))
    now = _now()
    if await running_run(db, now) is not None:
        await db.rollback()
        raise EaSyncBusyError("Una sincronizzazione della EA Map è già in corso")
    for stale in await db.scalars(select(EaSyncRun).where(EaSyncRun.status == "running")):
        stale.status = "failed"
        stale.finished_at = now
        stale.error = "Interrotta: nessun avanzamento per più di due ore"
    run = EaSyncRun(trigger=trigger, triggered_by=actor, status="running", started_at=now)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


# ---- collected data --------------------------------------------------------------------


@dataclass
class _State:
    components: dict[str, EaNode]
    product_domains: dict[str, str]
    snapshots: dict[str, EaRepoSnapshot]
    known_teams: set[str]
    decided: dict[tuple[str, str, str], str]  # (entity, op, target) -> status of last decision


@dataclass
class _Collected:
    listing: dict[str, RepoInfo] = field(default_factory=dict)
    census_ok: bool = False
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)
    fresh: dict[str, tuple[str, str, dict[str, Any]]] = field(
        default_factory=dict
    )  # repo -> (branch, sha, facts)
    touched: set[str] = field(default_factory=set)  # snapshots confirmed unchanged
    failures: dict[str, str] = field(default_factory=dict)
    pages: dict[str, list[dict[str, str]]] | None = None
    confluence_error: str | None = None
    candidates: list[str] = field(default_factory=list)


async def _read_state(db: AsyncSession) -> _State:
    nodes = list(await db.scalars(select(EaNode).where(EaNode.kind.in_(("component", "product")))))
    components = {n.id: n for n in nodes if n.kind == "component"}
    product_domains = {
        n.id: str((n.attrs or {}).get("domain"))
        for n in nodes
        if n.kind == "product" and (n.attrs or {}).get("domain")
    }
    snapshots = {s.repo: s for s in await db.scalars(select(EaRepoSnapshot))}
    teams = {o for n in components.values() for o in (n.attrs or {}).get("owners") or []}
    decided: dict[tuple[str, str, str], str] = {}
    rows = await db.execute(
        select(EaChange.entity, EaChange.op, EaChange.target_id, EaChange.status)
        .where(EaChange.status.in_(("pending", "conflict", "rejected", "approved")))
        .order_by(EaChange.created_at)
    )
    for entity, op, target, status in rows:
        decided[(entity, op, target)] = status
    await db.commit()
    return _State(components, product_domains, snapshots, teams, decided)


def _is_candidate(info: RepoInfo, state: _State, now: datetime) -> bool:
    if info.name in state.components or info.archived or info.fork:
        return False
    if not info.name.startswith(CANDIDATE_PREFIXES):
        return False
    if info.pushed_at is None or info.pushed_at < now - timedelta(days=CANDIDATE_MAX_AGE_DAYS):
        return False
    return state.decided.get(("node", "add", info.name)) not in ("rejected", "approved")


async def _collect(
    state: _State,
    github: GitHubSource,
    confluence: ConfluenceSource | None,
    now: datetime,
) -> _Collected:
    col = _Collected()
    try:
        col.listing = {r.name: r for r in await github.list_repos()}
        col.census_ok = True
    except SourceError as exc:
        col.failures["*census*"] = str(exc)
        return col

    targets: list[str] = []
    for name, node in state.components.items():
        info = col.listing.get(name)
        if node.status == "active" and info is not None and not info.archived:
            targets.append(name)
    col.candidates = [n for n, i in col.listing.items() if _is_candidate(i, state, now)]
    targets += col.candidates

    sem = asyncio.Semaphore(CONCURRENCY)

    async def read(name: str) -> None:
        info = col.listing[name]
        snap = state.snapshots.get(name)
        if snap is not None and info.pushed_at is not None and snap.fetched_at >= info.pushed_at:
            col.facts[name] = snap.extracted
            return
        async with sem:
            try:
                tree = await github.get_tree(name, info.default_branch)
                if snap is not None and snap.head_sha == tree.sha:
                    col.facts[name] = snap.extracted
                    col.touched.add(name)
                    return
                files = await github.get_files(name, info.default_branch, wanted_files(tree.paths))
            except SourceError as exc:
                col.failures[name] = str(exc)
                if snap is not None:
                    col.facts[name] = snap.extracted
                return
        facts = extract_repo(name, tree.paths, files)
        facts["truncated"] = tree.truncated
        col.facts[name] = facts
        col.fresh[name] = (info.default_branch, tree.sha, facts)

    await asyncio.gather(*(read(n) for n in targets))

    if confluence is not None:
        try:
            col.pages = await list_all_pages(confluence)
        except SourceError as exc:
            col.confluence_error = str(exc)
    return col


# ---- apply -----------------------------------------------------------------------------


@dataclass
class _Applier:
    db: AsyncSession
    run: EaSyncRun
    state: _State
    emitted: set[tuple[str, str, str]] = field(default_factory=set)
    scopes: set[str] = field(default_factory=set)
    stats: dict[str, int] = field(
        default_factory=lambda: {"auto_applied": 0, "pending": 0, "conflicts": 0}
    )

    def auto(self, entity: str, target: str, before: dict[str, Any], after: dict[str, Any]) -> None:
        self.db.add(
            EaChange(
                run_id=self.run.id,
                entity=entity,
                op="update",
                target_id=target,
                before=before,
                after=after,
                structural=False,
                status="auto_applied",
            )
        )
        self.stats["auto_applied"] += 1

    def propose(
        self,
        entity: str,
        op: str,
        target: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        *,
        conflict: bool = False,
    ) -> None:
        sig = (entity, op, target)
        self.emitted.add(sig)
        previous = self.state.decided.get(sig)
        if previous in ("pending", "conflict", "rejected"):
            return  # already queued, or a person said no: never propose it again
        self.db.add(
            EaChange(
                run_id=self.run.id,
                entity=entity,
                op=op,
                target_id=target,
                before=before,
                after=after,
                structural=True,
                status="conflict" if conflict else "pending",
            )
        )
        self.state.decided[sig] = "conflict" if conflict else "pending"
        self.stats["conflicts" if conflict else "pending"] += 1


def _changed(current: dict[str, Any], new: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    before = {k: current.get(k) for k in new if current.get(k) != new[k]}
    after = {k: new[k] for k in before}
    return before, after


async def _apply_component(ap: _Applier, node: EaNode, facts: dict[str, Any]) -> None:
    attrs = dict(node.attrs or {})
    new_attrs = derive.component_attrs(facts)
    before, after = _changed(attrs, new_attrs)
    if after:
        node.attrs = {**attrs, **after}
        ap.auto("node", node.id, before, after)

    if facts.get("truncated"):
        return  # GitHub returned a partial tree: counts would be wrong, keep the last ones
    sig = await ap.db.get(EaQaSignal, node.id)
    if sig is None:
        sig = EaQaSignal(node_id=node.id, central_suites=[], nrt_suites=[])
        ap.db.add(sig)
    observed = derive.qa_observed(node.id, facts)
    current = {k: getattr(sig, k) for k in observed}
    before, after = _changed(current, observed)
    if after:
        for k, v in after.items():
            setattr(sig, k, v)
        ap.auto("qa", node.id, before, after)
    sig.observed_at = _now()

    links = list(facts.get("readme_links") or [])
    docs = list(
        await ap.db.scalars(
            select(EaDocLink).where(EaDocLink.node_id == node.id, EaDocLink.source == "readme")
        )
    )
    if sorted(d.url for d in docs) != sorted(links):
        for d in docs:
            await ap.db.delete(d)
        await ap.db.flush()  # deletes first: the new links may reuse the same URLs
        for i, url in enumerate(links):
            ap.db.add(
                EaDocLink(
                    node_id=node.id,
                    url=url,
                    title=url.removeprefix("https://pagopa.atlassian.net/wiki/")[:500],
                    pages=1,
                    source="readme",
                    sort_order=i,
                )
            )
        ap.auto("doc", node.id, {"readme": sorted(d.url for d in docs)}, {"readme": links})


async def _apply_helm_edges(
    ap: _Applier, node: EaNode, facts: dict[str, Any], known: set[str]
) -> None:
    ap.scopes.add(node.id)
    wanted = {
        (target, edge_type): url
        for target, (edge_type, url) in derive.deps_for(node.id, facts).items()
        if target in known
    }
    edges = list(await ap.db.scalars(select(EaEdge).where(EaEdge.from_id == node.id)))
    pairs = {(e.to_id) for e in edges if e.status in ("active", "verified", "removed", "rejected")}
    for (target, edge_type), url in wanted.items():
        if target in pairs:
            continue  # a person or the import already describes this dependency
        ap.propose(
            "edge",
            "add",
            edge_key(node.id, target, edge_type),
            None,
            {
                "from": node.id,
                "to": target,
                "type": edge_type,
                "origin": "helm",
                "evidence": {"repo": node.id, "file": "helm/values-*.yaml", "value": url},
            },
        )
    wanted_targets = {target for target, _type in wanted}
    for e in edges:
        if e.origin != "helm" or e.status not in ("active", "verified"):
            continue
        if e.to_id in wanted_targets:
            continue  # still configured (a person may have corrected its type)
        before = {
            "from": e.from_id,
            "to": e.to_id,
            "type": e.type,
            "status": e.status,
            "id": str(e.id),
        }
        ap.propose(
            "edge",
            "remove",
            edge_key(e.from_id, e.to_id, e.type),
            before,
            None,
            conflict=e.status == "verified",
        )


def _candidate_payload(
    name: str, info: RepoInfo, facts: dict[str, Any], state: _State
) -> dict[str, Any]:
    component_products = {c.id: c.parent_id or "" for c in state.components.values() if c.parent_id}
    return {
        "id": name,
        "kind": "component",
        "name": derive.humanize(name),
        "description": info.description,
        "parent_id": derive.guess_product(name, facts, state.product_domains, component_products),
        "ctype": derive.guess_ctype(name, facts),
        "private": info.private,
        "pushed_at": info.pushed_at.isoformat() if info.pushed_at else None,
        "attrs": derive.component_attrs(facts),
        "qa": derive.qa_observed(name, facts),
        "readme_links": list(facts.get("readme_links") or []),
    }


async def _apply_product_docs(ap: _Applier, pages: dict[str, list[dict[str, str]]]) -> None:
    spaces = {k: derive.SpaceIndex(k, v) for k, v in pages.items()}
    products = list(await ap.db.scalars(select(EaNode).where(EaNode.kind == "product")))
    for p in products:
        links = derive.conf_links(p.id.removeprefix("prod:"), spaces)
        docs = list(
            await ap.db.scalars(
                select(EaDocLink).where(EaDocLink.node_id == p.id, EaDocLink.source == "rule")
            )
        )
        current = [(d.url, d.title, d.pages) for d in sorted(docs, key=lambda d: d.sort_order)]
        new = [(x["url"], x["title"][:500], int(x["pages"])) for x in links]
        if current == new:
            continue
        for d in docs:
            await ap.db.delete(d)
        await ap.db.flush()  # deletes first: the new links may reuse the same URLs
        for i, x in enumerate(links):
            ap.db.add(
                EaDocLink(
                    node_id=p.id,
                    url=x["url"],
                    title=x["title"][:500],
                    space=x["space"],
                    pages=int(x["pages"]),
                    source="rule",
                    sort_order=i,
                )
            )
        ap.auto(
            "doc",
            p.id,
            {"confluence": [c[1] for c in current]},
            {"confluence": [n[1] for n in new]},
        )


async def _apply(
    db: AsyncSession, run: EaSyncRun, state: _State, col: _Collected
) -> dict[str, Any]:
    ap = _Applier(db, run, state)
    now = _now()

    for repo, (branch, sha, facts) in col.fresh.items():
        snap = await db.get(EaRepoSnapshot, repo)
        if snap is None:
            db.add(
                EaRepoSnapshot(
                    repo=repo, default_branch=branch, head_sha=sha, extracted=facts, fetched_at=now
                )
            )
        else:
            snap.default_branch, snap.head_sha, snap.extracted, snap.fetched_at = (
                branch,
                sha,
                facts,
                now,
            )
    for repo in col.touched:
        snap = await db.get(EaRepoSnapshot, repo)
        if snap is not None:
            snap.fetched_at = now

    known = set((await db.scalars(select(EaNode.id).where(EaNode.status == "active"))).all())
    components = {
        n.id: n
        for n in await db.scalars(
            select(EaNode).where(EaNode.kind == "component", EaNode.status == "active")
        )
    }
    for name, node in components.items():
        comp_facts = col.facts.get(name)
        if comp_facts is None:
            continue
        await _apply_component(ap, node, comp_facts)
        await _apply_helm_edges(ap, node, comp_facts, known)

    if col.census_ok:
        ap.scopes.add("*census*")
        for name, node in components.items():
            info = col.listing.get(name)
            if info is None and (node.attrs or {}).get("private"):
                continue  # private repository the token cannot see: nothing to say
            if info is None or info.archived:
                reason = (
                    "archiviato" if info is not None else "non più presente nell'organizzazione"
                )
                ap.propose(
                    "node",
                    "update",
                    name,
                    {"status": "active"},
                    {"status": "archived", "reason": f"Repository {reason} su GitHub"},
                )
        for name in col.candidates:
            cand_facts = col.facts.get(name)
            if cand_facts is None or not derive.owners_overlap(
                cand_facts.get("owners") or [], state.known_teams
            ):
                continue
            ap.propose(
                "node",
                "add",
                name,
                None,
                _candidate_payload(name, col.listing[name], cand_facts, state),
            )

    if col.pages is not None:
        await _apply_product_docs(ap, col.pages)

    superseded = 0
    open_changes = await db.scalars(
        select(EaChange).where(EaChange.status.in_(OPEN_STATUSES), EaChange.run_id != run.id)
    )
    for ch in open_changes:
        sig = (ch.entity, ch.op, ch.target_id)
        if sig in ap.emitted:
            continue
        scope = "*census*" if ch.entity == "node" else (ch.after or ch.before or {}).get("from")
        if scope in ap.scopes:
            ch.status = "superseded"
            ch.review_note = "Non più rilevato dalla sincronizzazione"
            superseded += 1

    meta = await db.get(EaMeta, "kb")
    kb_meta = dict(meta.value) if meta is not None else {}
    kb_meta["generated"] = now.date().isoformat()
    sources = dict(kb_meta.get("sources") or {})
    if col.pages is not None:
        sources["confluence_pages"] = sum(len(v) for v in col.pages.values())
        sources["confluence_spaces"] = list(CONFLUENCE_SPACES)
    kb_meta["sources"] = sources
    if meta is None:
        db.add(EaMeta(key="kb", value=kb_meta))
    else:
        meta.value = kb_meta

    return {**ap.stats, "superseded": superseded}


# ---- entry point -----------------------------------------------------------------------


async def execute_run(
    db: AsyncSession,
    run_id: uuid.UUID,
    github: GitHubSource,
    confluence: ConfluenceSource | None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a registered sync to completion. Returns a summary (also when it fails).

    With ``dry_run`` everything is computed and then rolled back: the summary lists the
    proposals the sync would make.
    """
    run = await db.get(EaSyncRun, run_id)
    if run is None:
        raise ValueError(f"EA sync run {run_id} not found")
    now = _now()
    try:
        state = await _read_state(db)
        col = await _collect(state, github, confluence, now)
        if not col.census_ok:
            raise SourceError(col.failures.get("*census*", "Censimento GitHub non riuscito"))
        result = await _apply(db, run, state, col)
        repo_failures = {k: v for k, v in col.failures.items() if k != "*census*"}
        run.stats = {
            "repos_in_org": len(col.listing),
            "repos_read": len(col.fresh),
            "repos_unchanged": len(col.facts) - len(col.fresh),
            "repos_failed": len(repo_failures),
            "candidates_checked": len(col.candidates),
            "confluence_pages": sum(len(v) for v in col.pages.values()) if col.pages else 0,
            "github_rate_remaining": github.rate_limit_remaining(),
            **result,
        }
        run.source_versions = {
            "repos": {k: v[1] for k, v in col.fresh.items()},
            "failures": repo_failures,
            "confluence_error": col.confluence_error,
        }
        partial = bool(repo_failures or col.confluence_error or confluence is None)
        run.status = "partial" if partial else "success"
        run.error = col.confluence_error
        run.finished_at = _now()
        summary = _summary(run)
        if dry_run:
            await db.flush()
            summary["proposals"] = [
                {
                    "entity": c.entity,
                    "op": c.op,
                    "target": c.target_id,
                    "status": c.status,
                    "after": c.after,
                }
                for c in await db.scalars(
                    select(EaChange).where(
                        EaChange.run_id == run.id, EaChange.status != "auto_applied"
                    )
                )
            ]
            await db.rollback()
            await _discard_run(db, run_id)
            return summary
        await _record_status(db, run)
        await db.commit()
        logger.info("ea_sync_done", run_id=str(run_id), status=summary["status"])
        return summary
    except Exception as exc:  # noqa: BLE001 - every failure must close the run
        await db.rollback()
        failed = await db.get(EaSyncRun, run_id)
        if failed is None:
            raise
        run = failed
        run.status = "failed"
        run.error = str(exc)[:2000]
        run.finished_at = _now()
        summary = _summary(run)
        if dry_run:
            await db.rollback()
            await _discard_run(db, run_id)
        else:
            await _record_status(db, run)
            await db.commit()
        logger.warning("ea_sync_failed", run_id=str(run_id), error=str(exc))
        return summary


async def _discard_run(db: AsyncSession, run_id: uuid.UUID) -> None:
    """A dry run leaves no trace, not even its run row."""
    row = await db.get(EaSyncRun, run_id)
    if row is not None:
        await db.delete(row)
        await db.commit()


def _summary(run: EaSyncRun) -> dict[str, Any]:
    return {
        "run_id": str(run.id),
        "status": run.status,
        "error": run.error,
        "stats": dict(run.stats or {}),
        "failures": dict((run.source_versions or {}).get("failures") or {}),
    }


async def _record_status(db: AsyncSession, run: EaSyncRun) -> None:
    status = await db.get(EaSyncStatus, 1)
    if status is None:
        status = EaSyncStatus(id=1, last_attempt_at=run.started_at)
        db.add(status)
    status.last_attempt_at = run.started_at
    status.last_run_id = run.id
    if run.status in ("success", "partial"):
        status.last_success_at = run.finished_at
        status.last_error = run.error
    else:
        status.last_error = run.error


__all__ = [
    "GITHUB_ORG",
    "EaSyncBusyError",
    "create_run",
    "edge_key",
    "execute_run",
    "running_run",
]
