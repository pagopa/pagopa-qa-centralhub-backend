"""Read side of the EA Map: assemble the knowledge base served to the frontend.

The payload keeps the shape of the original ``kb.json`` (``meta``, ``nodes``, ``edges``,
``blueprint``) so the map views can be ported as they are, and adds ``origin``/``status``
to every edge for the editing features.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ea import (
    BLUEPRINT_EDGE_TYPES,
    VISIBLE_EDGE_STATUSES,
    EaDocLink,
    EaEdge,
    EaMeta,
    EaNode,
    EaQaSignal,
)
from app.services.ea.scoring import KIND_LABELS, QaFacts, maturity_score

_GRAPH_KINDS = ("area", "product", "component", "external", "datastore")


def _qa_payload(sig: EaQaSignal | None) -> dict[str, Any]:
    if sig is None:
        sig = EaQaSignal(
            unit_files=0,
            it_features=0,
            it_workflow=False,
            perf=False,
            e2e=False,
            sonar=False,
            central_suites=[],
            nrt_suites=[],
            nodo_bdd=False,
            tas_ci_observed=False,
        )
    central = list(sig.central_suites or [])
    nrt = list(sig.nrt_suites or [])
    facts = QaFacts(
        unit_files=sig.unit_files or 0,
        it_features=sig.it_features or 0,
        it_workflow=bool(sig.it_workflow),
        perf=bool(sig.perf),
        central_suites=tuple(central),
        nrt_suites=tuple(nrt),
        nodo_bdd=bool(sig.nodo_bdd),
    )
    tas_ci = sig.tas_ci_override if sig.tas_ci_override is not None else bool(sig.tas_ci_observed)
    return {
        "unit": facts.unit_files,
        "it_repo": facts.it_features,
        "it_wf": facts.it_workflow,
        "perf_repo": facts.perf,
        "e2e_repo": bool(sig.e2e),
        "sonar": bool(sig.sonar),
        "central": central,
        "nrt": nrt,
        "tas": bool(central),
        "tas_ci": tas_ci,
        "tas_ci_override": sig.tas_ci_override,
        "nodo_bdd": facts.nodo_bdd,
        "under_qa": sig.under_qa,
        "qa_owner": sig.qa_owner,
        "qa_notes": sig.qa_notes,
        "score": maturity_score(facts),
    }


def _doc_payload(d: EaDocLink) -> dict[str, Any]:
    return {
        "space": d.space,
        "id": None,
        "title": d.title,
        "pages": d.pages,
        "url": d.url,
        "source": d.source,
    }


async def build_kb(db: AsyncSession) -> dict[str, Any]:
    nodes = list(await db.scalars(select(EaNode).order_by(EaNode.sort_order, EaNode.id)))
    edges = list(
        await db.scalars(
            select(EaEdge)
            .where(EaEdge.status.in_(VISIBLE_EDGE_STATUSES))
            .order_by(EaEdge.sort_order, EaEdge.created_at)
        )
    )
    qa = {s.node_id: s for s in await db.scalars(select(EaQaSignal))}
    docs: dict[str, list[EaDocLink]] = defaultdict(list)
    for d in await db.scalars(select(EaDocLink).order_by(EaDocLink.sort_order)):
        docs[d.node_id].append(d)
    meta = {m.key: m.value for m in await db.scalars(select(EaMeta))}

    out_nodes: list[dict[str, Any]] = []
    for n in nodes:
        if n.kind not in _GRAPH_KINDS or n.status == "archived":
            continue
        base: dict[str, Any] = {
            "id": n.id,
            "type": n.kind,
            "name": n.name,
            "desc": n.description or "",
            "source": n.source,
            "verified": n.verified_at is not None,
        }
        a = n.attrs or {}
        if n.kind == "area":
            base["short"] = a.get("short")
        elif n.kind == "product":
            base.update(
                parent=n.parent_id,
                domain=a.get("domain"),
                conf=[
                    _doc_payload(d) for d in docs.get(n.id, []) if d.source in ("rule", "manual")
                ],
            )
        elif n.kind == "component":
            ctype = a.get("ctype") or "be"
            base.update(
                parent=n.parent_id,
                kind=ctype,
                kindLabel=KIND_LABELS.get(ctype, ctype),
                repo=a.get("repo"),
                private=bool(a.get("private")),
                lang=a.get("lang") or [],
                fw=a.get("fw") or [],
                ns=a.get("ns"),
                ingress=a.get("ingress"),
                owners=a.get("owners") or [],
                apis=a.get("apis") or [],
                readme=a.get("readme") or "",
                workflows=a.get("workflows") or [],
                confRepo=[d.url for d in docs.get(n.id, []) if d.source == "readme"],
                qa=_qa_payload(qa.get(n.id)),
            )
        out_nodes.append(base)

    visible = {n["id"] for n in out_nodes}
    tech_edges = [
        {
            "id": str(e.id),
            "from": e.from_id,
            "to": e.to_id,
            "type": e.type,
            "label": e.label,
            "origin": e.origin,
            "status": e.status,
        }
        for e in edges
        if e.type not in BLUEPRINT_EDGE_TYPES and e.from_id in visible and e.to_id in visible
    ]

    # blueprint
    bp_edges: dict[str, list[EaEdge]] = defaultdict(list)
    for e in edges:
        if e.type in BLUEPRINT_EDGE_TYPES:
            bp_edges[e.type].append(e)

    def kind_nodes(kind: str) -> list[EaNode]:
        return [n for n in nodes if n.kind == kind and n.status != "archived"]

    serves_to: dict[str, list[str]] = defaultdict(list)
    for e in bp_edges["serves"]:
        serves_to[e.to_id].append(e.from_id)
    realizes: dict[str, list[str]] = defaultdict(list)
    for e in bp_edges["realizes"]:
        realizes[e.from_id].append(e.to_id)
    group_of = {e.to_id: e.from_id for e in bp_edges["groups"]}
    uses: dict[str, list[str]] = defaultdict(list)
    for e in bp_edges["uses"]:
        uses[e.from_id].append(e.to_id.removeprefix("prod:"))
    runs_on: dict[str, list[str]] = defaultdict(list)
    for e in bp_edges["runs_on"]:
        runs_on[e.from_id.removeprefix("prod:")].append(e.to_id)

    risk: dict[str, str] = {}
    for n in nodes:
        r = (n.attrs or {}).get("risk")
        if r:
            risk[n.id.removeprefix("prod:")] = r

    blueprint = {
        "segments": [
            {
                "id": n.id,
                "name": n.name,
                "who": (n.attrs or {}).get("who") or [],
                "actors": (n.attrs or {}).get("actors") or [],
            }
            for n in kind_nodes("segment")
        ],
        "services": [
            {
                "id": n.id,
                "name": n.name,
                "seg": serves_to.get(n.id, []),
                "proc": realizes.get(n.id, []),
            }
            for n in kind_nodes("service")
        ],
        "functions": [{"id": n.id, "name": n.name} for n in kind_nodes("function")],
        "processes": [
            {
                "id": n.id,
                "name": n.name,
                "fn": group_of.get(n.id),
                "prod": uses.get(n.id, []),
                "ref": (n.attrs or {}).get("ref"),
                "suites": (n.attrs or {}).get("suites") or [],
            }
            for n in kind_nodes("process")
        ],
        "infra": [
            {
                "id": n.id,
                "name": n.name,
                "group": (n.attrs or {}).get("group"),
                "desc": n.description or "",
            }
            for n in kind_nodes("infra")
        ],
        "prodInfra": {k: sorted(v) for k, v in runs_on.items()},
        "risk": risk,
        "riskSrc": meta.get("risk_source"),
        "panels": meta.get("blueprint_panels") or {},
    }

    kb_meta = dict(meta.get("kb") or {})
    sources = dict(kb_meta.get("sources") or {})
    sources["github_repos"] = sum(1 for n in out_nodes if n["type"] == "component")
    kb_meta["sources"] = sources
    last = await db.scalar(text("SELECT max(updated_at) FROM ea_nodes"))
    if last is not None:
        kb_meta["updated_at"] = last.isoformat()
    return {"meta": kb_meta, "nodes": out_nodes, "edges": tech_edges, "blueprint": blueprint}


async def get_node_detail(db: AsyncSession, node_id: str) -> dict[str, Any] | None:
    node = await db.get(EaNode, node_id)
    if node is None:
        return None
    out_edges = list(await db.scalars(select(EaEdge).where(EaEdge.from_id == node_id)))
    in_edges = list(await db.scalars(select(EaEdge).where(EaEdge.to_id == node_id)))
    sig = await db.get(EaQaSignal, node_id)
    docs = list(await db.scalars(select(EaDocLink).where(EaDocLink.node_id == node_id)))

    def edge(e: EaEdge) -> dict[str, Any]:
        return {
            "id": str(e.id),
            "from": e.from_id,
            "to": e.to_id,
            "type": e.type,
            "label": e.label,
            "origin": e.origin,
            "status": e.status,
            "confidence": e.confidence,
            "note": e.note,
            "evidence": e.evidence,
            "created_by": e.created_by,
            "verified_by": e.verified_by,
            "verified_at": e.verified_at,
        }

    return {
        "id": node.id,
        "kind": node.kind,
        "parent": node.parent_id,
        "name": node.name,
        "desc": node.description,
        "attrs": node.attrs,
        "source": node.source,
        "status": node.status,
        "verified_at": node.verified_at,
        "verified_by": node.verified_by,
        "qa": _qa_payload(sig) if node.kind == "component" else None,
        "docs": [_doc_payload(d) for d in docs],
        "out": [edge(e) for e in out_edges],
        "in": [edge(e) for e in in_edges],
    }
