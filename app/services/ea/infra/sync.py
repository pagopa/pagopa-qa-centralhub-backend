"""Infrastructure view in the EA sync: collect the infra repositories, apply the graph.

Called by the sync engine in the same run as components, Confluence and data contracts
(docs/ea-map/DESIGN-infra.md §5). Terraform facts are applied immediately (one summary
change per repository); links resource → map node are proposals for review.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from app.config import settings
from app.models.ea import EaInfraLink, EaInfraResource, EaNode, EaRepoSnapshot
from app.services.ea.infra.config import INFRA_REPOS, SNAPSHOT_PREFIX
from app.services.ea.infra.parser import PARSER_VERSION, parse_repo, wanted_paths
from app.services.ea.infra.resolve import build
from app.services.ea.sync.sources import GitHubSource, RepoInfo, SourceError
from app.services.ea.sync.tracing import trace_phase

if TYPE_CHECKING:
    from app.services.ea.sync.engine import _Applier

INFRA_SCOPE = "*infra*"
# resource columns the sync owns (ea_node_id is a human decision)
_SYNCED = (
    "repo",
    "stack",
    "address",
    "tf_kind",
    "tf_type",
    "ltype",
    "vendor",
    "label",
    "file",
    "envs",
    "names",
    "attrs",
    "parent_id",
    "blueprint_id",
    "external",
)
_SAMPLE = 30  # labels listed in the summary change of a repository


@dataclass
class InfraCollected:
    extracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    fresh: dict[str, tuple[str, str]] = field(default_factory=dict)  # repo -> (branch, sha)
    errors: dict[str, str] = field(default_factory=dict)  # read failures: partial run
    skipped: dict[str, str] = field(default_factory=dict)  # not visible to the token


def snapshot_key(repo: str) -> str:
    return f"{SNAPSHOT_PREFIX}{repo}"


async def collect(
    snapshots: dict[str, EaRepoSnapshot],
    github: GitHubSource,
    listing: dict[str, RepoInfo],
    log: Any,
) -> InfraCollected:
    """Read the infra repositories pushed after their snapshot; reuse the others."""
    out = InfraCollected()
    for repo in INFRA_REPOS:
        info = listing.get(repo)
        snap = snapshots.get(snapshot_key(repo))
        if info is None:
            out.skipped[repo] = "Repository non visibile al token GitHub"
            if snap is not None:
                out.extracts[repo] = snap.extracted
            continue
        current = snap is not None and (snap.extracted or {}).get("parser") == PARSER_VERSION
        if (
            current
            and snap is not None
            and info.pushed_at is not None
            and snap.fetched_at >= info.pushed_at
        ):
            out.extracts[repo] = snap.extracted
            continue
        try:
            with trace_phase(log, "infra_tree", repo=repo):
                tree = await github.get_tree(repo, info.default_branch)
            if current and snap is not None and snap.head_sha == tree.sha:
                out.extracts[repo] = snap.extracted
                continue
            with trace_phase(log, "infra_files", repo=repo):
                files = await github.get_files(repo, info.default_branch, wanted_paths(tree.paths))
        except SourceError as exc:
            out.errors[repo] = str(exc)
            if snap is not None:
                out.extracts[repo] = snap.extracted  # keep the last known picture
            continue
        with trace_phase(log, "infra_parse", repo=repo):
            extract = await asyncio.to_thread(
                parse_repo, files, network_details=settings.ea_infra_include_network_details
            )
        extract["truncated"] = tree.truncated
        out.extracts[repo] = extract
        out.fresh[repo] = (info.default_branch, tree.sha)
    return out


def _row(res: dict[str, Any]) -> dict[str, Any]:
    return {k: res.get(k) for k in _SYNCED}


async def apply(ap: _Applier, collected: InfraCollected) -> dict[str, int]:
    """Bring ea_infra_resources / ea_infra_links in line with the extracts."""
    db = ap.db
    nodes = list(
        await db.execute(
            select(EaNode.id, EaNode.kind, EaNode.attrs).where(EaNode.status == "active")
        )
    )
    components = {i for i, k, _a in nodes if k == "component"}
    frontends = {i for i, k, a in nodes if k == "component" and (a or {}).get("ctype") == "fe"}
    blueprint = {i for i, k, _a in nodes if k == "infra"}
    namespaces = {i.removeprefix("ns-"): i for i in blueprint if i.startswith("ns-")}
    graph = build(
        collected.extracts,
        components=components,
        namespaces=namespaces,
        blueprint=blueprint,
        frontends=frontends,
    )
    # a repository we could not read at all keeps its resources as they are
    unread = {r for r in INFRA_REPOS if r not in collected.extracts}

    existing = {r.id: r for r in await db.scalars(select(EaInfraResource))}
    added: dict[str, list[str]] = {}
    removed: dict[str, list[str]] = {}
    updated: dict[str, int] = {}
    for rid, res in graph.resources.items():
        row = existing.get(rid)
        values = _row(res)
        if row is None:
            db.add(EaInfraResource(id=rid, status="active", **values))
            added.setdefault(res["repo"], []).append(res["label"])
            continue
        changed = row.status != "active" or any(getattr(row, k) != v for k, v in values.items())
        if changed:
            for k, v in values.items():
                setattr(row, k, v)
            row.status = "active"
            updated[res["repo"]] = updated.get(res["repo"], 0) + 1
    for rid, row in existing.items():
        if rid in graph.resources or row.status == "removed" or row.repo in unread:
            continue
        row.status = "removed"
        removed.setdefault(row.repo, []).append(row.label)
    await db.flush()

    # links are pure Terraform facts: rebuilt every time
    await db.execute(delete(EaInfraLink))
    for link in graph.links.values():
        db.add(EaInfraLink(**link))

    for repo in sorted(set(added) | set(removed) | set(updated)):
        a, r = added.get(repo, []), removed.get(repo, [])
        ap.auto(
            "infra",
            snapshot_key(repo),
            {"removed": sorted(r)[:_SAMPLE]} if r else None,
            {
                "added": len(a),
                "removed": len(r),
                "updated": updated.get(repo, 0),
                "sample_added": sorted(a)[:_SAMPLE],
            },
        )

    ap.scopes.add(INFRA_SCOPE)
    linked = {r.id for r in existing.values() if r.ea_node_id}
    for p in graph.proposals:
        if p["resource_id"] in linked:
            continue  # a person already linked this resource
        res = graph.resources[p["resource_id"]]
        ap.propose(
            "infra",
            "add",
            f"{p['resource_id']}>{p['node_id']}",
            None,
            {
                "from": INFRA_SCOPE,
                "resource_id": p["resource_id"],
                "node_id": p["node_id"],
                "label": res["label"],
                "ltype": res["ltype"],
                "rule": p["rule"],
                "evidence": {"repo": res["repo"], "file": res["file"], "names": res["names"]},
            },
        )
    return {
        "infra_resources": graph.stats["resources"],
        "infra_links": graph.stats["links"],
        "infra_alerts": graph.stats["alerts"],
        "infra_added": sum(len(v) for v in added.values()),
        "infra_removed": sum(len(v) for v in removed.values()),
    }
