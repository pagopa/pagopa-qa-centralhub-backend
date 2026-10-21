from __future__ import annotations

# ruff: noqa: S101
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.main import app
from app.models.ea import EaChange, EaDocLink, EaEdge, EaNode, EaQaSignal, EaRepoSnapshot, EaSyncRun
from app.services.ea import edit
from app.services.ea.importer import import_kb
from app.services.ea.kb import build_kb
from app.services.ea.sync import review
from app.services.ea.sync.engine import EaSyncBusyError, create_run, edge_key, execute_run
from app.services.ea.sync.sources import RepoInfo, RepoTree, SourceError
from tests._db import TestSession, get_test_db
from tests.test_ea_service import _wipe

NOW = datetime.now(UTC)
WISP, GPD, PAY, OLD, NODO = (
    "pagopa-wisp-converter",
    "pagopa-debt-position",
    "pagopa-gpd-payments",
    "pagopa-old-service",
    "pagopa-nodo4-nodo-dei-pagamenti",
)
NEW = "pagopa-gpd-new-thing"


def _comp(cid: str, parent: str, owners: list[str], private: bool = False) -> dict[str, Any]:
    return {
        "id": cid,
        "type": "component",
        "parent": parent,
        "name": cid,
        "desc": "",
        "kind": "be",
        "repo": f"https://github.com/pagopa/{cid}",
        "private": private,
        "lang": [],
        "fw": [],
        "ns": None,
        "ingress": None,
        "owners": owners,
        "apis": [],
        "readme": "",
        "workflows": [],
        "confRepo": [],
        "qa": {"unit": 0, "it_repo": 0, "central": [], "nrt": []},
    }


def _kb() -> dict[str, Any]:
    return {
        "meta": {"generated": "2026-10-01", "sources": {}},
        "nodes": [
            {"id": "area:gps", "type": "area", "name": "GPS", "desc": ""},
            {"id": "area:core", "type": "area", "name": "Core", "desc": ""},
            {
                "id": "prod:gpd",
                "type": "product",
                "parent": "area:gps",
                "name": "GPD",
                "desc": "",
                "domain": "gps",
                "conf": [],
            },
            {
                "id": "prod:wisp",
                "type": "product",
                "parent": "area:core",
                "name": "WISP",
                "desc": "",
                "domain": "nodo",
                "conf": [],
            },
            {
                "id": "prod:nodo",
                "type": "product",
                "parent": "area:core",
                "name": "Nodo",
                "desc": "",
                "domain": "nodo",
                "conf": [],
            },
            _comp(WISP, "prod:wisp", ["team-core"]),
            _comp(GPD, "prod:gpd", ["team-gps"]),
            _comp(PAY, "prod:gpd", ["team-gps"]),
            _comp(OLD, "prod:gpd", ["team-gps"]),
            _comp(NODO, "prod:nodo", ["team-core"], private=True),
            {"id": "ds-gps", "type": "datastore", "name": "Cosmos GPS", "desc": ""},
        ],
        "edges": [
            {"from": WISP, "to": GPD, "type": "api", "origin": "helm"},
            {"from": WISP, "to": PAY, "type": "api", "origin": "helm"},
            {"from": GPD, "to": "ds-gps", "type": "data", "origin": "helm"},
        ],
        "blueprint": {},
    }


def _repo(name: str, pushed: datetime = NOW, **kw: Any) -> RepoInfo:
    return RepoInfo(
        name=name,
        archived=kw.get("archived", False),
        fork=False,
        private=False,
        default_branch="main",
        pushed_at=pushed,
        description=kw.get("description"),
    )


def _helm(env: dict[str, str]) -> str:
    lines = ["microservice-chart:", "  namespace: gps", "  envConfig:"]
    lines += [f'    {k}: "{v}"' for k, v in env.items()]
    return "\n".join(lines)


class FakeGitHub:
    def __init__(self) -> None:
        self.listing = [
            _repo(WISP),
            _repo(GPD),
            _repo(PAY),
            _repo(OLD, archived=True),
            _repo(NEW, description="Nuovo servizio GPD"),
            _repo("pagopa-other-team"),
            _repo("io-something"),
        ]
        self.trees: dict[str, list[str]] = {
            WISP: ["helm/values-prod.yaml", "CODEOWNERS"],
            GPD: ["helm/values-prod.yaml", "src/test/A.java", "src/test/B.java", "CODEOWNERS"],
            PAY: ["CODEOWNERS"],
            NEW: ["helm/values-prod.yaml", "Dockerfile", "CODEOWNERS", "README.md"],
            "pagopa-other-team": ["CODEOWNERS"],
        }
        self.files: dict[str, dict[str, str]] = {
            WISP: {
                "helm/values-prod.yaml": _helm(
                    {"COSMOS": "https://pagopa-p-gps-cosmos.documents.azure.com"}
                ),
                "CODEOWNERS": "* @pagopa/team-core",
            },
            GPD: {
                "helm/values-prod.yaml": _helm(
                    {"COSMOS": "https://pagopa-p-gps-cosmos.documents.azure.com"}
                ),
                "CODEOWNERS": "* @pagopa/team-gps",
            },
            PAY: {"CODEOWNERS": "* @pagopa/team-gps"},
            NEW: {
                "helm/values-prod.yaml": _helm({}),
                "CODEOWNERS": "* @pagopa/team-gps",
                "README.md": "Nuovo servizio\n\nhttps://pagopa.atlassian.net/wiki/x/new",
            },
            "pagopa-other-team": {"CODEOWNERS": "* @pagopa/someone-else"},
        }
        self.tree_calls: list[str] = []
        self.fail: set[str] = set()

    async def list_repos(self) -> list[RepoInfo]:
        return self.listing

    async def get_tree(self, repo: str, branch: str) -> RepoTree:
        self.tree_calls.append(repo)
        if repo in self.fail:
            raise SourceError(f"boom {repo}")
        paths = self.trees.get(repo, [])
        content = repr(sorted(self.files.get(repo, {}).items()))
        sha = hashlib.sha1(f"{paths}{content}".encode()).hexdigest()  # noqa: S324
        return RepoTree(sha=sha, paths=paths, truncated=False)

    async def get_files(self, repo: str, branch: str, paths: list[str]) -> dict[str, str]:
        return {p: t for p, t in self.files.get(repo, {}).items() if p in paths}

    def rate_limit_remaining(self) -> int | None:
        return 4321


class FakeConfluence:
    async def list_pages(self, space_key: str) -> list[dict[str, str]]:
        if space_key != "IQCGJ":
            return []
        return [
            {"id": "1", "t": "Home", "par": ""},
            {"id": "2", "t": "[Gestione Posizioni Debitorie]", "par": "1"},
            {"id": "3", "t": "Dettaglio", "par": "2"},
        ]


@pytest.fixture(autouse=True)
def _test_engine_for_api() -> Iterator[None]:
    """API calls use the NullPool test engine (no connections shared across event loops)."""
    app.dependency_overrides[get_db] = get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with TestSession() as session:
        await _wipe(session)
        await import_kb(session, _kb())
        yield session
        await session.rollback()
        await _wipe(session)


async def _sync(
    db: AsyncSession, gh: FakeGitHub, conf: FakeConfluence | None = None
) -> dict[str, Any]:
    run = await create_run(db, trigger="manual", actor="tester")
    return await execute_run(db, run.id, gh, conf or FakeConfluence())


async def _open(db: AsyncSession) -> dict[tuple[str, str, str], EaChange]:
    rows = await db.scalars(select(EaChange).where(EaChange.status.in_(("pending", "conflict"))))
    return {(c.entity, c.op, c.target_id): c for c in rows}


async def test_first_sync_applies_metadata_and_queues_structure(db: AsyncSession) -> None:
    edge = await db.scalar(select(EaEdge).where(EaEdge.from_id == WISP, EaEdge.to_id == PAY))
    assert edge is not None
    await edit.verify_edge(db, edge.id, note="confermato", actor="qa")

    summary = await _sync(db, FakeGitHub())
    assert summary["status"] == "success", summary
    queued = await _open(db)

    assert set(queued) == {
        ("edge", "add", edge_key(WISP, "ds-gps", "data")),
        ("edge", "remove", edge_key(WISP, GPD, "api")),
        ("edge", "remove", edge_key(WISP, PAY, "api")),
        ("node", "update", OLD),
        ("node", "add", NEW),
    }
    assert queued[("edge", "remove", edge_key(WISP, PAY, "api"))].status == "conflict"
    assert queued[("edge", "remove", edge_key(WISP, GPD, "api"))].status == "pending"
    new = queued[("node", "add", NEW)].after or {}
    assert new["parent_id"] == "prod:gpd" and new["ctype"] == "be"
    # the private Nodo repository is not visible to the token: no archive proposal
    assert ("node", "update", NODO) not in queued

    qa = await db.get(EaQaSignal, GPD)
    assert qa is not None and qa.unit_files == 2
    node = await db.get(EaNode, GPD)
    assert node is not None and node.attrs["ns"] == "gps" and node.attrs["owners"] == ["team-gps"]
    docs = list(await db.scalars(select(EaDocLink).where(EaDocLink.node_id == "prod:gpd")))
    assert [d.title for d in docs] == ["[Gestione Posizioni Debitorie]"] and docs[0].pages == 2
    assert summary["stats"]["pending"] == 4 and summary["stats"]["conflicts"] == 1
    assert summary["stats"]["auto_applied"] >= 3
    # nothing structural changed in the visible map until someone decides
    kb = await build_kb(db)
    assert any(e["from"] == WISP and e["to"] == GPD for e in kb["edges"])
    assert await db.get(EaRepoSnapshot, WISP) is not None


async def test_second_sync_reuses_snapshots_and_does_not_duplicate(db: AsyncSession) -> None:
    gh = FakeGitHub()
    await _sync(db, gh)
    first_calls = len(gh.tree_calls)
    summary = await _sync(db, gh)
    assert len(gh.tree_calls) == first_calls  # pushed_at older than the snapshots: no reads
    assert summary["stats"]["pending"] == 0 and summary["stats"]["auto_applied"] == 0
    assert len(await _open(db)) == 5


async def test_rejected_proposals_are_not_proposed_again(db: AsyncSession) -> None:
    gh = FakeGitHub()
    await _sync(db, gh)
    queued = await _open(db)
    await review.decide(
        db, queued[("node", "add", NEW)].id, approve=False, actor="qa", note="non è pagamenti"
    )
    await review.decide(
        db, queued[("edge", "add", edge_key(WISP, "ds-gps", "data"))].id, approve=False, actor="qa"
    )
    gh.listing = [
        _repo(r.name, NOW + timedelta(minutes=5), archived=r.archived) for r in gh.listing
    ]
    await _sync(db, gh)
    queued = await _open(db)
    assert ("node", "add", NEW) not in queued
    assert ("edge", "add", edge_key(WISP, "ds-gps", "data")) not in queued
    rejected = await db.scalar(
        select(EaEdge).where(EaEdge.from_id == WISP, EaEdge.to_id == "ds-gps")
    )
    assert rejected is not None and rejected.status == "rejected"


async def test_approvals_change_the_map(db: AsyncSession) -> None:
    await _sync(db, FakeGitHub())
    q = await _open(db)
    await review.decide(
        db,
        q[("node", "add", NEW)].id,
        approve=True,
        actor="qa",
        overrides={"name": "GPD New", "ctype": "fn"},
    )
    await review.decide(
        db, q[("edge", "add", edge_key(WISP, "ds-gps", "data"))].id, approve=True, actor="qa"
    )
    await review.decide(db, q[("node", "update", OLD)].id, approve=True, actor="qa")
    await review.decide(
        db, q[("edge", "remove", edge_key(WISP, PAY, "api"))].id, approve=True, actor="qa"
    )
    await review.decide(
        db, q[("edge", "remove", edge_key(WISP, GPD, "api"))].id, approve=False, actor="qa"
    )

    kb = await build_kb(db)
    nodes = {n["id"]: n for n in kb["nodes"]}
    assert (
        nodes[NEW]["name"] == "GPD New"
        and nodes[NEW]["kind"] == "fn"
        and nodes[NEW]["parent"] == "prod:gpd"
    )
    assert OLD not in nodes
    pairs = {(e["from"], e["to"]): e for e in kb["edges"]}
    assert pairs[(WISP, "ds-gps")]["status"] == "verified"
    assert (WISP, PAY) not in pairs
    assert pairs[(WISP, GPD)]["status"] == "verified"  # rejected removal = the edge is real
    history = await edit.node_history(db, NEW)
    assert history[0]["action"] == "ea.change.approve"
    with pytest.raises(edit.EaEditError):
        await review.decide(db, q[("node", "add", NEW)].id, approve=True, actor="qa")


async def test_stale_proposals_are_superseded(db: AsyncSession) -> None:
    gh = FakeGitHub()
    await _sync(db, gh)
    gh.files[WISP]["helm/values-prod.yaml"] = _helm(
        {
            "COSMOS": "https://pagopa-p-gps-cosmos.documents.azure.com",
            "GPD": "https://api/gpd/api/v1",
        }
    )
    gh.listing = [
        _repo(r.name, NOW + timedelta(minutes=5), archived=r.archived) for r in gh.listing
    ]
    summary = await _sync(db, gh)
    assert summary["stats"]["superseded"] == 1
    assert ("edge", "remove", edge_key(WISP, GPD, "api")) not in await _open(db)


async def test_repo_failures_make_a_partial_run_and_keep_proposals(db: AsyncSession) -> None:
    gh = FakeGitHub()
    await _sync(db, gh)
    gh.fail = {WISP}
    gh.listing = [
        _repo(r.name, NOW + timedelta(minutes=5), archived=r.archived) for r in gh.listing
    ]
    summary = await _sync(db, gh)
    assert summary["status"] == "partial" and WISP in summary["failures"]
    assert ("edge", "remove", edge_key(WISP, GPD, "api")) in await _open(db)


async def test_census_failure_fails_the_run(db: AsyncSession) -> None:
    gh = FakeGitHub()

    async def broken() -> list[RepoInfo]:
        raise SourceError("Token GitHub non valido")

    gh.list_repos = broken  # type: ignore[method-assign]
    summary = await _sync(db, gh)
    assert summary["status"] == "failed" and "Token" in (summary["error"] or "")
    assert len(await _open(db)) == 0


async def test_only_one_run_at_a_time(db: AsyncSession) -> None:
    await create_run(db, trigger="manual", actor="a")
    with pytest.raises(EaSyncBusyError):
        await create_run(db, trigger="manual", actor="b")
    old = await db.scalar(select(EaSyncRun).where(EaSyncRun.status == "running"))
    assert old is not None
    old.started_at = NOW - timedelta(hours=3)
    await db.commit()
    run = await create_run(db, trigger="manual", actor="b")
    await db.refresh(old)
    assert old.status == "failed" and run.status == "running"


async def test_api_sync_status_and_review(db: AsyncSession, client: AsyncClient) -> None:
    with patch("app.api.v1.ea._dispatch") as dispatch:
        started = await client.post("/api/v1/ea/sync", json={"actor": "qa@example.test"})
        assert started.status_code == 202, started.text
        dispatch.assert_called_once()
        busy = await client.post("/api/v1/ea/sync", json={"actor": "qa@example.test"})
        assert busy.status_code == 409
    run_id = uuid.UUID(started.json()["id"])
    await execute_run(db, run_id, FakeGitHub(), FakeConfluence())

    status = (await client.get("/api/v1/ea/sync-status")).json()
    assert status["running"] is None and status["last"]["status"] == "success"
    assert status["open_changes"] == 5

    changes = (await client.get("/api/v1/ea/changes")).json()
    new = next(c for c in changes if c["op"] == "add" and c["entity"] == "node")
    bad = await client.post(
        f"/api/v1/ea/changes/{new['id']}/approve", json={"actor": "qa", "parent_id": "area:gps"}
    )
    assert bad.status_code == 400
    ok = await client.post(f"/api/v1/ea/changes/{new['id']}/approve", json={"actor": "qa"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    rest = [c["id"] for c in changes if c["id"] != new["id"]]
    bulk = (
        await client.post(
            "/api/v1/ea/changes/bulk", json={"ids": rest, "decision": "reject", "actor": "qa"}
        )
    ).json()
    assert set(bulk.values()) == {"rejected"}
    assert (await client.get("/api/v1/ea/sync-status")).json()["open_changes"] == 0
    auto = (await client.get(f"/api/v1/ea/changes?status=auto_applied&run_id={run_id}")).json()
    assert auto and all(c["status"] == "auto_applied" for c in auto)


async def test_doc_links_can_change_while_keeping_the_same_urls(db: AsyncSession) -> None:
    """Regression: replacing links that keep their URL must not hit the unique constraint."""

    class GrowingConfluence(FakeConfluence):
        extra = 0

        async def list_pages(self, space_key: str) -> list[dict[str, str]]:
            pages = await super().list_pages(space_key)
            if space_key == "IQCGJ":
                pages += [
                    {"id": f"x{i}", "t": f"Figlia {i}", "par": "2"} for i in range(self.extra)
                ]
            return pages

    gh = FakeGitHub()
    gh.files[GPD]["README.md"] = "https://pagopa.atlassian.net/wiki/x/one"
    gh.trees[GPD].append("README.md")
    conf = GrowingConfluence()
    await _sync(db, gh, conf)

    conf.extra = 3  # same page, bigger subtree
    gh.files[GPD]["README.md"] = (
        "https://pagopa.atlassian.net/wiki/x/one https://pagopa.atlassian.net/wiki/x/two"
    )
    gh.listing = [
        _repo(r.name, NOW + timedelta(minutes=5), archived=r.archived) for r in gh.listing
    ]
    summary = await _sync(db, gh, conf)
    assert summary["status"] == "success", summary

    product_docs = list(await db.scalars(select(EaDocLink).where(EaDocLink.node_id == "prod:gpd")))
    assert [(d.title, d.pages) for d in product_docs] == [("[Gestione Posizioni Debitorie]", 5)]
    readme = await db.scalars(
        select(EaDocLink.url).where(EaDocLink.node_id == GPD, EaDocLink.source == "readme")
    )
    assert sorted(readme) == [
        "https://pagopa.atlassian.net/wiki/x/one",
        "https://pagopa.atlassian.net/wiki/x/two",
    ]
