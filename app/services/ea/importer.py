"""Bootstrap import of an EA Map knowledge base (``kb.json``) into PostgreSQL.

The file is produced by the offline discovery scripts (GitHub + Confluence). Everything
imported is marked as ``curated`` (or ``helm`` for edges read from Helm values), so later
manual edits and verifications can be told apart and are never overwritten by a sync.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import delete, func, select
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

logger = structlog.get_logger(__name__)

# runs_on targets derived by rule (namespace, functions, APIM, Cosmos); the rest is curated
_RULE_INFRA = frozenset({"in-fn", "in-apim", "in-cosmos"})
_CONFIDENCE = {"helm": 90, "infra_rule": 70, "curated": 60, "manual": 100}


class EaImportError(RuntimeError):
    """Raised when the knowledge base cannot be imported."""


class _Collector:
    def __init__(self) -> None:
        try:
            self.nodes: dict[str, EaNode] = {}
            self.edges: dict[tuple[str, str, str], EaEdge] = {}
            self.qa: list[EaQaSignal] = []
            self.docs: dict[tuple[str, str], EaDocLink] = {}
        except Exception:
            logger.exception("_Collector.__init__ failed")
            raise

    def node(self, **kwargs: Any) -> None:
        try:
            node_id = kwargs["id"]
            if node_id in self.nodes:
                raise EaImportError(f"Duplicate node id: {node_id}")
            kwargs.setdefault("sort_order", len(self.nodes))
            kwargs.setdefault("source", "curated")
            kwargs.setdefault("status", "active")
            self.nodes[node_id] = EaNode(**kwargs)
        except Exception:
            logger.exception("_Collector.node failed")
            raise

    def edge(
        self, from_id: str, to_id: str, type_: str, origin: str, label: str | None = None
    ) -> None:
        try:
            key = (from_id, to_id, type_)
            if key in self.edges:
                return
            self.edges[key] = EaEdge(
                from_id=from_id,
                to_id=to_id,
                type=type_,
                origin=origin,
                label=label or None,
                status="active",
                confidence=_CONFIDENCE[origin],
                evidence={"source": "kb_import"},
                sort_order=len(self.edges),
            )
        except Exception:
            logger.exception("_Collector.edge failed")
            raise

    def doc(
        self, node_id: str, url: str, title: str, space: str | None, pages: int, source: str
    ) -> None:
        try:
            key = (node_id, url)
            if key in self.docs:
                return
            self.docs[key] = EaDocLink(
                node_id=node_id,
                url=url,
                title=title[:500],
                space=space,
                pages=pages,
                source=source,
                sort_order=len(self.docs),
            )
        except Exception:
            logger.exception("_Collector.doc failed")
            raise


def _api(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize an API contract reference to ``{"f": path, "t": title}``."""
    try:
        return {"f": item.get("f") or item.get("file"), "t": item.get("t") or item.get("title")}
    except Exception:
        logger.exception("_api failed")
        raise


def _collect(kb: dict[str, Any]) -> _Collector:
    try:
        col = _Collector()
        nodes = kb.get("nodes") or []
        blueprint = kb.get("blueprint") or {}
        risk: dict[str, str] = blueprint.get("risk") or {}

        for n in nodes:
            t = n["type"]
            if t == "area":
                col.node(
                    id=n["id"],
                    kind="area",
                    name=n["name"],
                    description=n.get("desc"),
                    attrs={"short": n.get("short")},
                )
            elif t == "product":
                short = n["id"].removeprefix("prod:")
                attrs: dict[str, Any] = {"domain": n.get("domain")}
                if short in risk:
                    attrs["risk"] = risk[short]
                col.node(
                    id=n["id"],
                    kind="product",
                    parent_id=n.get("parent"),
                    name=n["name"],
                    description=n.get("desc"),
                    attrs=attrs,
                )
                for link in n.get("conf") or []:
                    col.doc(
                        n["id"],
                        link["url"],
                        link["title"],
                        link.get("space"),
                        int(link.get("pages") or 1),
                        "rule",
                    )
            elif t == "component":
                col.node(
                    id=n["id"],
                    kind="component",
                    parent_id=n.get("parent"),
                    name=n["name"],
                    description=n.get("desc"),
                    attrs={
                        "ctype": n.get("kind"),
                        "repo": n.get("repo"),
                        "private": bool(n.get("private")),
                        "lang": n.get("lang") or [],
                        "fw": n.get("fw") or [],
                        "ns": n.get("ns"),
                        "ingress": n.get("ingress"),
                        "owners": n.get("owners") or [],
                        "apis": [_api(a) for a in n.get("apis") or []],
                        "readme": n.get("readme") or "",
                        "workflows": n.get("workflows") or [],
                    },
                )
                for url in n.get("confRepo") or []:
                    col.doc(
                        n["id"],
                        url,
                        url.removeprefix("https://pagopa.atlassian.net/wiki/"),
                        None,
                        1,
                        "readme",
                    )
                qa = n.get("qa") or {}
                col.qa.append(
                    EaQaSignal(
                        node_id=n["id"],
                        unit_files=int(qa.get("unit") or 0),
                        it_features=int(qa.get("it_repo") or 0),
                        it_workflow=bool(qa.get("it_wf")),
                        perf=bool(qa.get("perf_repo")),
                        e2e=bool(qa.get("e2e_repo")),
                        sonar=bool(qa.get("sonar")),
                        central_suites=list(qa.get("central") or []),
                        nrt_suites=list(qa.get("nrt") or []),
                        nodo_bdd=bool(qa.get("nodo_bdd")),
                        tas_ci_observed=bool(qa.get("tas_ci")),
                        under_qa=qa.get("under_qa"),
                        observed_at=datetime.now(UTC),
                    )
                )
            elif t in ("external", "datastore"):
                col.node(id=n["id"], kind=t, name=n["name"], description=n.get("desc"), attrs={})
            else:
                raise EaImportError(f"Unknown node type: {t}")

        # blueprint elements
        for s in blueprint.get("segments") or []:
            col.node(
                id=s["id"],
                kind="segment",
                name=s["name"],
                attrs={"who": s.get("who") or [], "actors": s.get("actors") or []},
            )
        for f in blueprint.get("functions") or []:
            col.node(id=f["id"], kind="function", name=f["name"], attrs={})
        for p in blueprint.get("processes") or []:
            col.node(
                id=p["id"],
                kind="process",
                name=p["name"],
                attrs={"ref": p.get("ref"), "suites": p.get("suites") or []},
            )
        for s in blueprint.get("services") or []:
            col.node(id=s["id"], kind="service", name=s["name"], attrs={})
        for i in blueprint.get("infra") or []:
            attrs = {"group": i.get("group")}
            if i["id"] in risk:
                attrs["risk"] = risk[i["id"]]
            col.node(id=i["id"], kind="infra", name=i["name"], description=i.get("desc"), attrs=attrs)

        # technical edges
        for e in kb.get("edges") or []:
            col.edge(e["from"], e["to"], e["type"], e.get("origin") or "curated", e.get("label"))
        # blueprint edges
        for s in blueprint.get("services") or []:
            for seg in s.get("seg") or []:
                col.edge(seg, s["id"], "serves", "curated")
            for proc in s.get("proc") or []:
                col.edge(s["id"], proc, "realizes", "curated")
        for p in blueprint.get("processes") or []:
            col.edge(p["fn"], p["id"], "groups", "curated")
            for prod in p.get("prod") or []:
                col.edge(p["id"], f"prod:{prod}", "uses", "curated")
        for prod, infra_ids in (blueprint.get("prodInfra") or {}).items():
            for infra_id in infra_ids:
                origin = (
                    "infra_rule" if infra_id.startswith("ns-") or infra_id in _RULE_INFRA else "curated"
                )
                col.edge(f"prod:{prod}", infra_id, "runs_on", origin)

        missing = {
            endpoint
            for (src, dst, _t) in col.edges
            for endpoint in (src, dst)
            if endpoint not in col.nodes
        }
        missing |= {
            n.parent_id for n in col.nodes.values() if n.parent_id and n.parent_id not in col.nodes
        }
        if missing:
            raise EaImportError(f"References to unknown nodes: {sorted(missing)[:10]}")
        return col
    except Exception:
        logger.exception("_collect failed")
        raise


def _meta(kb: dict[str, Any]) -> dict[str, Any]:
    try:
        blueprint = kb.get("blueprint") or {}
        return {
            "kb": kb.get("meta") or {},
            "blueprint_panels": blueprint.get("panels") or {},
            "risk_source": blueprint.get("riskSrc"),
        }
    except Exception:
        logger.exception("_meta failed")
        raise


async def is_empty(db: AsyncSession) -> bool:
    try:
        count = await db.scalar(select(func.count()).select_from(EaNode))
        if count == 0:
            logger.info("is_empty count query succeeded with 0 matching records")
        return count == 0
    except Exception:
        logger.exception("is_empty failed")
        raise


async def import_kb(
    db: AsyncSession, kb: dict[str, Any], *, replace: bool = False
) -> dict[str, int]:
    """Load ``kb`` into the EA tables. Refuses to overwrite existing data unless ``replace``."""
    try:
        col = _collect(kb)
        if not await is_empty(db):
            if not replace:
                raise EaImportError("EA Map already contains data; use replace=True to reload it")
            for model in (
                EaChange,
                EaSyncStatus,
                EaSyncRun,
                EaRepoSnapshot,
                EaDocLink,
                EaQaSignal,
                EaEdge,
            ):
                await db.execute(delete(model))
            await db.execute(delete(EaNode).where(EaNode.parent_id.is_not(None)))
            await db.execute(delete(EaNode))
            await db.execute(delete(EaMeta))

        # parents before children: areas, then products, then everything else
        tiers = ({"area"}, {"product"}, None)
        for tier in tiers:
            db.add_all(
                n
                for n in col.nodes.values()
                if (n.kind in tier if tier is not None else n.kind not in {"area", "product"})
            )
            await db.flush()
        db.add_all(col.edges.values())
        db.add_all(col.qa)
        db.add_all(col.docs.values())
        for key, value in _meta(kb).items():
            db.add(EaMeta(key=key, value=value))
        db.add(
            EaSyncRun(
                trigger="import",
                status="success",
                finished_at=datetime.now(UTC),
                stats={"nodes": len(col.nodes), "edges": len(col.edges)},
            )
        )
        await db.commit()
        return {
            "nodes": len(col.nodes),
            "edges": len(col.edges),
            "qa": len(col.qa),
            "docs": len(col.docs),
        }
    except Exception:
        logger.exception("import_kb failed")
        raise
