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
from time import monotonic
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
from app.services.ea.contracts import (
    CONSUMER_NODES,
    CONTRACTS_PATH,
    CONTRACTS_REPO,
    build_graph,
    propose_producer,
)
from app.services.ea.infra import sync as infra_sync
from app.services.ea.sync import derive
from app.services.ea.sync.extract import extract_repo, wanted_files
from app.services.ea.sync.rules import (
    CANDIDATE_MAX_AGE_DAYS,
    CONFLUENCE_SPACES,
    GITHUB_ORG,
    REPO_PERIMETER,
    in_perimeter,
)
from app.services.ea.sync.sources import (
    ConfluenceSource,
    GitHubSource,
    RepoInfo,
    SourceError,
    list_all_pages,
)
from app.services.ea.sync.tracing import error_fields, trace_phase

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
    # data contracts (docs/ea-map/DESIGN-data-lake.md §5): graph as stored in the snapshot
    contracts: dict[str, Any] | None = None
    contracts_fresh: tuple[str, str] | None = None  # (branch, sha) when read in this run
    contracts_error: str | None = None
    contracts_skipped: str | None = None
    # infrastructure view (docs/ea-map/DESIGN-infra.md §5)
    infra: infra_sync.InfraCollected = field(default_factory=infra_sync.InfraCollected)


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
    if not in_perimeter(info.name):
        return False
    if info.pushed_at is None or info.pushed_at < now - timedelta(days=CANDIDATE_MAX_AGE_DAYS):
        return False
    return state.decided.get(("node", "add", info.name)) not in ("rejected", "approved")


async def _collect(
    state: _State,
    github: GitHubSource,
    confluence: ConfluenceSource | None,
    now: datetime,
    *,
    run_id: uuid.UUID | None = None,
) -> _Collected:
    log = logger.bind(run_id=str(run_id) if run_id else None)
    col = _Collected()
    try:
        with trace_phase(log, "github_census"):
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
                with trace_phase(log, "github_tree", repo=name):
                    tree = await github.get_tree(name, info.default_branch)
                if snap is not None and snap.head_sha == tree.sha:
                    col.facts[name] = snap.extracted
                    col.touched.add(name)
                    return
                with trace_phase(log, "github_files", repo=name):
                    files = await github.get_files(
                        name, info.default_branch, wanted_files(tree.paths)
                    )
            except SourceError as exc:
                col.failures[name] = str(exc)
                if snap is not None:
                    col.facts[name] = snap.extracted
                return
        facts = extract_repo(name, tree.paths, files)
        facts["truncated"] = tree.truncated
        col.facts[name] = facts
        col.fresh[name] = (info.default_branch, tree.sha, facts)

    with trace_phase(log, "github_repositories", targets=len(targets)):
        await asyncio.gather(*(read(n) for n in targets))
    with trace_phase(log, "data_contracts"):
        await _collect_contracts(state, github, col, run_id=run_id)
    with trace_phase(log, "infra"):
        col.infra = await infra_sync.collect(state.snapshots, github, col.listing, log)
    log.info(
        "ea_sync_sources_collected",
        repos_in_org=len(col.listing),
        repos_read=len(col.fresh),
        repos_failed=len(col.failures),
        contracts_failed=col.contracts_error is not None,
        contracts_skipped=col.contracts_skipped is not None,
    )

    if confluence is not None:
        try:
            with trace_phase(log, "confluence_pages"):
                col.pages = await list_all_pages(confluence)
        except SourceError as exc:
            col.confluence_error = str(exc)
    else:
        log.info("ea_sync_source_skipped", source="confluence", reason="not_configured")
    return col


def _graph_dict(files: dict[str, str]) -> dict[str, Any]:
    g = build_graph(files)
    return {
        "nodes": g.nodes,
        "edges": g.edges,
        "quality_tables": g.quality_tables,
        "errors": g.errors,
        "files": len([p for p in files if p.endswith((".yaml", ".yml"))]),
    }


async def _collect_contracts(
    state: _State,
    github: GitHubSource,
    col: _Collected,
    *,
    run_id: uuid.UUID | None = None,
) -> None:
    """Read the data contracts repository, unless nothing was pushed since the snapshot."""
    log = logger.bind(run_id=str(run_id) if run_id else None)
    info = col.listing.get(CONTRACTS_REPO)
    snap = state.snapshots.get(CONTRACTS_REPO)
    if info is None:
        # the token does not see the repository: keep what we know, do not fail the run
        col.contracts_skipped = "Repository dei data contract non visibile al token GitHub"
        if snap is not None:
            col.contracts = snap.extracted
        return
    if snap is not None and info.pushed_at is not None and snap.fetched_at >= info.pushed_at:
        col.contracts = snap.extracted
        return
    try:
        with trace_phase(log, "data_contracts_tree", repo=CONTRACTS_REPO):
            tree = await github.get_tree(CONTRACTS_REPO, info.default_branch)
        if snap is not None and snap.head_sha == tree.sha:
            col.contracts = snap.extracted
            col.touched.add(CONTRACTS_REPO)
            return
        paths = [
            p
            for p in tree.paths
            if p.startswith(CONTRACTS_PATH + "/") and p.endswith((".yaml", ".yml"))
        ]
        with trace_phase(log, "data_contracts_files", repo=CONTRACTS_REPO):
            files = await github.get_files(CONTRACTS_REPO, info.default_branch, paths)
    except SourceError as exc:
        col.contracts_error = f"Data contract: {exc}"
        if snap is not None:
            col.contracts = snap.extracted  # keep the last known picture
        return
    col.contracts = _graph_dict(files)
    col.contracts_fresh = (info.default_branch, tree.sha)


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

    def auto(
        self,
        entity: str,
        target: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        op: str = "update",
    ) -> None:
        self.db.add(
            EaChange(
                run_id=self.run.id,
                entity=entity,
                op=op,
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


OUT_OF_PERIMETER = (
    f"Fuori dal perimetro della mappa: il nome del repository non contiene «{REPO_PERIMETER}»"
)


async def _propose_out_of_perimeter(ap: _Applier, components: dict[str, EaNode]) -> None:
    """Components whose repository is outside the perimeter, and the products they leave
    empty, go to review for archiving (a rejection keeps them for good)."""
    out = {
        name
        for name, node in components.items()
        if not in_perimeter(name) and node.source != "manual"
    }
    for name in sorted(out):
        ap.propose(
            "node",
            "update",
            name,
            {"status": "active"},
            {"status": "archived", "reason": OUT_OF_PERIMETER},
        )
    by_product: dict[str, list[str]] = {}
    for name, node in components.items():
        if node.parent_id:
            by_product.setdefault(node.parent_id, []).append(name)
    for product_id, names in sorted(by_product.items()):
        if names and all(n in out for n in names):
            ap.propose(
                "node",
                "update",
                product_id,
                {"status": "active"},
                {
                    "status": "archived",
                    "reason": "Tutti i suoi componenti sono fuori dal perimetro della mappa",
                },
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


DATA_EDGE_CONFIDENCE = {"contract": 80, "curated": 60}
# dataset attributes a person owns (the sync never overwrites them)
DATASET_MANUAL_ATTRS = ("layer_override", "doc_url_override", "slack_url_override")


async def _ensure_consumers(ap: _Applier) -> None:
    for node_id, spec in CONSUMER_NODES.items():
        if await ap.db.get(EaNode, node_id) is not None:
            continue
        ap.db.add(
            EaNode(
                id=node_id,
                kind="external",
                name=spec["name"],
                description=spec["description"],
                source="curated",
                status="active",
                attrs={},
            )
        )
        ap.auto("node", node_id, None, {"kind": "external", "name": spec["name"]}, op="add")


async def _propose_producers(ap: _Applier, wanted: dict[str, dict[str, Any]]) -> None:
    """Queue "component produces dataset" for ingestions nobody has linked yet (PR2)."""
    db = ap.db
    candidates = {
        n.id: n.name
        for n in await db.scalars(
            select(EaNode).where(
                EaNode.kind.in_(("component", "product")), EaNode.status == "active"
            )
        )
    }
    decided = {
        e.to_id
        for e in await db.scalars(
            select(EaEdge).where(
                EaEdge.type == "produces",
                EaEdge.status.in_(("active", "verified", "removed", "rejected")),
            )
        )
    }
    for dataset_id, payload in wanted.items():
        attrs = payload.get("attrs") or {}
        if attrs.get("layer") != "ingestion" or dataset_id in decided:
            continue  # someone already linked (or refused) a producer
        guess = propose_producer(attrs.get("topic"), candidates)
        if guess is None:
            continue
        producer, confidence, rule = guess
        ap.scopes.add(producer)
        ap.propose(
            "edge",
            "add",
            edge_key(producer, dataset_id, "produces"),
            None,
            {
                "from": producer,
                "to": dataset_id,
                "type": "produces",
                "origin": "contract_rule",
                "confidence": confidence,
                "evidence": {
                    "topic": attrs.get("topic"),
                    "rule": rule,
                    "contract": (attrs.get("contract") or {}).get("path"),
                },
            },
        )


async def _apply_data_lake(ap: _Applier, graph: dict[str, Any]) -> None:
    """Datasets from the data contracts, and the derives/feeds edges among them.

    Dataset content is a fact of the contract and is applied immediately; a dataset that
    is no longer in the contracts goes to review. Contract edges no longer declared are
    removed, unless a person verified them (conflict).
    """
    db = ap.db
    await _ensure_consumers(ap)
    wanted: dict[str, dict[str, Any]] = graph.get("nodes") or {}
    existing = {n.id: n for n in await db.scalars(select(EaNode).where(EaNode.kind == "dataset"))}
    for node_id, payload in wanted.items():
        node = existing.get(node_id)
        attrs = dict(payload.get("attrs") or {})
        if node is None:
            db.add(
                EaNode(
                    id=node_id,
                    kind="dataset",
                    name=payload["name"],
                    description=payload.get("description") or None,
                    attrs=attrs,
                    source="sync",
                    status="active",
                )
            )
            ap.auto(
                "node",
                node_id,
                None,
                {"kind": "dataset", "name": payload["name"], "layer": attrs.get("layer")},
                op="add",
            )
            continue
        current = dict(node.attrs or {})
        merged = {**attrs, **{k: current[k] for k in DATASET_MANUAL_ATTRS if k in current}}
        before, after = _changed(current, merged)
        removed = [k for k in current if k not in merged]
        if removed:
            before.update({k: current[k] for k in removed})
            after.update({k: None for k in removed})
        if node.source != "manual" and node.verified_at is None:
            for key, value in (
                ("name", payload["name"]),
                ("description", payload.get("description") or None),
            ):
                if getattr(node, key) != value:
                    before[key], after[key] = getattr(node, key), value
                    setattr(node, key, value)
        if node.status == "archived":
            before["status"], after["status"] = "archived", "active"
            node.status = "active"
        if after:
            node.attrs = merged
            ap.auto("node", node_id, before, after)

    ap.scopes.add("*census*")
    for node_id, node in existing.items():
        if node_id in wanted or node.status == "archived" or node.source == "manual":
            continue
        ap.propose(
            "node",
            "update",
            node_id,
            {"status": node.status},
            {"status": "archived", "reason": "Data contract non più presente nel repository"},
        )
    await db.flush()

    known = set((await db.scalars(select(EaNode.id).where(EaNode.status == "active"))).all())
    want_edges = {
        (e["from"], e["to"], e["type"]): e
        for e in graph.get("edges") or []
        if e["from"] in known and e["to"] in known
    }
    dataset_ids = set(wanted) | set(existing)
    current_edges = list(
        await db.scalars(
            select(EaEdge).where(
                EaEdge.type.in_(("derives", "feeds")),
                (EaEdge.from_id.in_(dataset_ids)) | (EaEdge.to_id.in_(dataset_ids)),
            )
        )
    )
    by_key = {(e.from_id, e.to_id, e.type): e for e in current_edges}
    for ekey, spec in want_edges.items():
        if ekey in by_key:
            continue  # present, or a person removed or rejected it: leave it alone
        db.add(
            EaEdge(
                from_id=spec["from"],
                to_id=spec["to"],
                type=spec["type"],
                origin=spec["origin"],
                status="active",
                confidence=DATA_EDGE_CONFIDENCE.get(spec["origin"], 60),
                evidence=spec.get("evidence") or {},
            )
        )
        ap.auto(
            "edge",
            edge_key(*ekey),
            None,
            {"from": ekey[0], "to": ekey[1], "type": ekey[2], "origin": spec["origin"]},
            op="add",
        )
    for ekey, e in by_key.items():
        if ekey in want_edges or e.origin not in ("contract", "curated"):
            continue
        if e.status == "active":
            e.status = "removed"
            ap.auto(
                "edge",
                edge_key(*ekey),
                {"from": e.from_id, "to": e.to_id, "type": e.type, "status": "active"},
                {"status": "removed", "reason": "Non più dichiarato nei data contract"},
                op="remove",
            )
        elif e.status == "verified":
            ap.scopes.add(e.from_id)
            ap.propose(
                "edge",
                "remove",
                edge_key(*ekey),
                {
                    "from": e.from_id,
                    "to": e.to_id,
                    "type": e.type,
                    "status": e.status,
                    "id": str(e.id),
                },
                None,
                conflict=True,
            )

    await _propose_producers(ap, wanted)

    meta = await db.get(EaMeta, "dq_result_tables")
    tables = graph.get("quality_tables") or []
    if meta is None:
        db.add(EaMeta(key="dq_result_tables", value=tables))
    elif meta.value != tables:
        meta.value = tables


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
    if col.contracts_fresh is not None and col.contracts is not None:
        branch, sha = col.contracts_fresh
        snap = await db.get(EaRepoSnapshot, CONTRACTS_REPO)
        if snap is None:
            db.add(
                EaRepoSnapshot(
                    repo=CONTRACTS_REPO,
                    default_branch=branch,
                    head_sha=sha,
                    extracted=col.contracts,
                    fetched_at=now,
                )
            )
        else:
            snap.default_branch, snap.head_sha, snap.extracted, snap.fetched_at = (
                branch,
                sha,
                col.contracts,
                now,
            )
    for repo, (branch, sha) in col.infra.fresh.items():
        key = infra_sync.snapshot_key(repo)
        extract = col.infra.extracts[repo]
        snap = await db.get(EaRepoSnapshot, key)
        if snap is None:
            db.add(
                EaRepoSnapshot(
                    repo=key, default_branch=branch, head_sha=sha, extracted=extract, fetched_at=now
                )
            )
        else:
            snap.default_branch, snap.head_sha, snap.extracted, snap.fetched_at = (
                branch,
                sha,
                extract,
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
        await _propose_out_of_perimeter(ap, components)
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

    if col.contracts is not None:
        await _apply_data_lake(ap, col.contracts)

    infra_stats: dict[str, int] = {}
    if col.infra.extracts:
        infra_stats = await infra_sync.apply(ap, col.infra)

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
    if col.contracts is not None:
        sources["data_contracts"] = col.contracts.get("files", 0)
        sources["data_contracts_repo"] = f"{GITHUB_ORG}/{CONTRACTS_REPO}"
    if col.infra.extracts:
        sources["infra_repos"] = [f"{GITHUB_ORG}/{r}" for r in sorted(col.infra.extracts)]
        sources["infra_resources"] = infra_stats.get("infra_resources", 0)
    kb_meta["sources"] = sources
    if meta is None:
        db.add(EaMeta(key="kb", value=kb_meta))
    else:
        meta.value = kb_meta

    return {**ap.stats, **infra_stats, "superseded": superseded}


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
    started = monotonic()
    log = logger.bind(run_id=str(run_id), dry_run=dry_run)
    with trace_phase(log, "load_run"):
        run = await db.get(EaSyncRun, run_id)
        if run is None:
            raise ValueError(f"EA sync run {run_id} not found")
    now = _now()
    phase = "read_state"
    try:
        with trace_phase(log, phase):
            state = await _read_state(db)
        phase = "collect"
        with trace_phase(log, phase):
            col = await _collect(state, github, confluence, now, run_id=run_id)
        if not col.census_ok:
            raise SourceError(col.failures.get("*census*", "Censimento GitHub non riuscito"))
        phase = "apply"
        with trace_phase(log, phase):
            result = await _apply(db, run, state, col)
        repo_failures = {k: v for k, v in col.failures.items() if k != "*census*"}
        run.stats = {
            "repos_in_org": len(col.listing),
            "repos_read": len(col.fresh),
            "repos_unchanged": len(col.facts) - len(col.fresh),
            "repos_failed": len(repo_failures),
            "candidates_checked": len(col.candidates),
            "confluence_pages": sum(len(v) for v in col.pages.values()) if col.pages else 0,
            "data_contracts": (col.contracts or {}).get("files", 0),
            "data_contracts_read": col.contracts_fresh is not None,
            "datasets": len((col.contracts or {}).get("nodes") or {}),
            "infra_repos_read": len(col.infra.fresh),
            "github_rate_remaining": github.rate_limit_remaining(),
            **result,
        }
        run.source_versions = {
            "repos": {k: v[1] for k, v in col.fresh.items()},
            "failures": repo_failures,
            "confluence_error": col.confluence_error,
            "data_contracts": col.contracts_fresh[1] if col.contracts_fresh else None,
            "data_contracts_error": col.contracts_error or col.contracts_skipped,
            "data_contracts_invalid": (col.contracts or {}).get("errors") or {},
            "infra": {k: v[1] for k, v in col.infra.fresh.items()},
            "infra_errors": {**col.infra.skipped, **col.infra.errors},
            "infra_invalid_files": {
                repo: ext.get("errors") or {}
                for repo, ext in col.infra.extracts.items()
                if ext.get("errors")
            },
        }
        partial = bool(
            repo_failures
            or col.confluence_error
            or confluence is None
            or col.contracts_error
            or col.infra.errors
        )
        run.status = "partial" if partial else "success"
        run.error = col.confluence_error or col.contracts_error
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
        phase = "commit"
        with trace_phase(log, phase):
            await _record_status(db, run)
            await db.commit()
        log.info(
            "ea_sync_done",
            status=summary["status"],
            duration_ms=round((monotonic() - started) * 1000),
        )
        return summary
    except Exception as exc:  # noqa: BLE001 - every failure must close the run
        log.error(
            "ea_sync_failed",
            phase=phase,
            duration_ms=round((monotonic() - started) * 1000),
            **error_fields(exc),
        )
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
