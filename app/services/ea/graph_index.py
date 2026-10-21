"""In-memory view of the EA knowledge base, for analysis rather than display.

Built from the payload of :func:`app.services.ea.kb.build_kb`, so release impact and the
map assistant see exactly what the map shows (visible nodes, active and verified edges).

Direction of the edges:

* technical edges (``api``, ``soap``, ``event``, ``ui``, ``data``) go from the caller to
  what it calls: a change in a node affects its *callers* (incoming edges);
* Data Lake edges (``produces``, ``derives``, ``feeds``) follow the data: a change in a
  node affects what is *downstream* of it (outgoing edges).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

CALL_EDGES = frozenset({"api", "soap", "event", "ui", "data"})
FLOW_EDGES = frozenset({"produces", "derives", "feeds"})
SEARCH_KINDS = ("component", "product", "area", "external", "datastore", "dataset")


@dataclass(frozen=True)
class Hop:
    """A node reached from a start node: shortest distance and the path that reaches it."""

    id: str
    depth: int
    via: str
    path: tuple[str, ...]


@dataclass
class BpItem:
    id: str
    level: str  # "service" | "process"
    name: str
    products: list[str] = field(default_factory=list)  # product ids (prod:…)
    ref: dict[str, Any] | None = None
    services: list[str] = field(default_factory=list)  # for processes: service names
    processes: list[str] = field(default_factory=list)  # for services: process ids


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class GraphIndex:
    def __init__(self, kb: dict[str, Any]) -> None:
        self.kb = kb
        self.nodes: dict[str, dict[str, Any]] = {n["id"]: n for n in kb.get("nodes", [])}
        self.out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.inc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for e in kb.get("edges", []):
            if e.get("from") in self.nodes and e.get("to") in self.nodes:
                self.out[e["from"]].append(e)
                self.inc[e["to"]].append(e)
        self.by_repo: dict[str, str] = {}
        for n in self.nodes.values():
            if n.get("type") != "component":
                continue
            self.by_repo[n["id"].lower()] = n["id"]
            repo = (n.get("repo") or "").rstrip("/")
            if repo:
                self.by_repo[repo.rsplit("/", 1)[-1].lower()] = n["id"]
        self.bp: dict[str, BpItem] = {}
        bp = kb.get("blueprint") or {}
        services = bp.get("services") or []
        for p in bp.get("processes") or []:
            self.bp[p["id"]] = BpItem(
                id=p["id"],
                level="process",
                name=p["name"],
                products=[f"prod:{x}" for x in p.get("prod") or []],
                ref=p.get("ref"),
                services=[s["name"] for s in services if p["id"] in (s.get("proc") or [])],
            )
        for s in services:
            prods: list[str] = []
            for pid in s.get("proc") or []:
                if pid in self.bp:
                    prods += [x for x in self.bp[pid].products if x not in prods]
            self.bp[s["id"]] = BpItem(
                id=s["id"],
                level="service",
                name=s["name"],
                products=prods,
                processes=list(s.get("proc") or []),
            )

    # ── lookups ──────────────────────────────────────────────────────────────

    def node(self, node_id: str) -> dict[str, Any] | None:
        return self.nodes.get(node_id)

    def resolve(self, ref: str) -> str | None:
        """A node id from an id, a repository name or an exact name (case-insensitive)."""
        if ref in self.nodes:
            return ref
        low = ref.strip().lower()
        if low in self.by_repo:
            return self.by_repo[low]
        if f"prod:{low}" in self.nodes:
            return f"prod:{low}"
        for n in self.nodes.values():
            if n["name"].lower() == low:
                return str(n["id"])
        return None

    def product_of(self, node_id: str) -> dict[str, Any] | None:
        n = self.nodes.get(node_id)
        if n is None:
            return None
        if n["type"] == "product":
            return n
        if n["type"] == "component":
            return self.nodes.get(n.get("parent") or "")
        return None

    def area_of(self, node_id: str) -> dict[str, Any] | None:
        p = self.product_of(node_id)
        if p is None:
            n = self.nodes.get(node_id)
            return n if n and n["type"] == "area" else None
        return self.nodes.get(p.get("parent") or "")

    def components_of(self, product_id: str) -> list[dict[str, Any]]:
        return [
            n
            for n in self.nodes.values()
            if n["type"] == "component" and n.get("parent") == product_id
        ]

    def processes_of(self, product_id: str) -> list[BpItem]:
        return [b for b in self.bp.values() if b.level == "process" and product_id in b.products]

    # ── search ───────────────────────────────────────────────────────────────

    def search(
        self, query: str, limit: int = 10, include_blueprint: bool = True
    ) -> list[dict[str, Any]]:
        q = _norm(query)
        if not q:
            return []
        words = q.split()
        scored: list[tuple[int, str, dict[str, Any]]] = []

        def score(*texts: str) -> int:
            best = 0
            for t in texts:
                t = _norm(t)
                if not t:
                    continue
                if t == q:
                    best = max(best, 100)
                elif t.startswith(q):
                    best = max(best, 80)
                elif f" {q}" in f" {t}":
                    best = max(best, 60)
                elif q in t:
                    best = max(best, 40)
                elif all(w in t for w in words):
                    best = max(best, 30)
            return best

        for n in self.nodes.values():
            if n["type"] not in SEARCH_KINDS:
                continue
            s = score(n["name"], n["id"].removeprefix("prod:"), n.get("repo") or "")
            if s:
                bonus = {"product": 6, "area": 5, "component": 3}.get(n["type"], 0)
                scored.append(
                    (s + bonus, n["name"], {"id": n["id"], "type": n["type"], "name": n["name"]})
                )
        if include_blueprint:
            for b in self.bp.values():
                s = score(b.name)
                if s:
                    scored.append((s + 4, b.name, {"id": b.id, "type": b.level, "name": b.name}))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [x[2] for x in scored[:limit]]

    # ── walks ────────────────────────────────────────────────────────────────

    def _walk(
        self, start: str, depth: int, step: Callable[[str], list[tuple[str, str]]]
    ) -> list[Hop]:
        seen: dict[str, Hop] = {}
        queue: deque[Hop] = deque([Hop(start, 0, "", (start,))])
        while queue:
            cur = queue.popleft()
            if cur.depth >= depth:
                continue
            for nxt, via in step(cur.id):
                if nxt == start or nxt in seen:
                    continue
                hop = Hop(nxt, cur.depth + 1, via, cur.path + (nxt,))
                seen[nxt] = hop
                queue.append(hop)
        return sorted(seen.values(), key=lambda h: (h.depth, self.nodes[h.id]["name"]))

    def callers(self, node_id: str, depth: int = 2) -> list[Hop]:
        """Who calls this node, directly or through others (they feel its changes)."""
        return self._walk(
            node_id,
            depth,
            lambda i: [(e["from"], e["type"]) for e in self.inc[i] if e["type"] in CALL_EDGES],
        )

    def dependencies(self, node_id: str, depth: int = 1) -> list[Hop]:
        """What this node calls or reads."""
        return self._walk(
            node_id,
            depth,
            lambda i: [(e["to"], e["type"]) for e in self.out[i] if e["type"] in CALL_EDGES],
        )

    def downstream_data(self, node_id: str, depth: int = 3) -> list[Hop]:
        """Datasets and consumers fed, directly or not, by this node (Data Lake lineage)."""
        return self._walk(
            node_id,
            depth,
            lambda i: [(e["to"], e["type"]) for e in self.out[i] if e["type"] in FLOW_EDGES],
        )

    # ── summaries (compact, for the assistant) ──────────────────────────────

    def label(self, node_id: str) -> str:
        n = self.nodes.get(node_id)
        if n is not None:
            return str(n["name"])
        b = self.bp.get(node_id)
        return b.name if b else node_id

    def qa_brief(self, n: dict[str, Any]) -> dict[str, Any]:
        qa = n.get("qa") or {}
        return {
            "maturita": qa.get("score", 0),
            "test_unitari": qa.get("unit", 0),
            "integration_repo": bool(qa.get("it_repo")) or bool(qa.get("it_wf")),
            "suite_centrali": qa.get("central") or [],
            "nrt": qa.get("nrt") or [],
            "bdd_nodo": bool(qa.get("nodo_bdd")),
            "tas_in_ci": bool(qa.get("tas_ci")),
            "performance": bool(qa.get("perf_repo")),
            "sotto_qa": qa.get("under_qa"),
        }

    def summary(self, node_id: str) -> dict[str, Any] | None:
        n = self.nodes.get(node_id)
        if n is None:
            b = self.bp.get(node_id)
            if b is None:
                return None
            out: dict[str, Any] = {"id": b.id, "tipo": b.level, "nome": b.name}
            out["prodotti"] = [{"id": p, "nome": self.label(p)} for p in b.products]
            if b.level == "process":
                out["servizi"] = b.services
                if b.ref:
                    out["pagina_confluence"] = {
                        "titolo": b.ref.get("title"),
                        "url": b.ref.get("url"),
                    }
            else:
                out["processi"] = [{"id": p, "nome": self.label(p)} for p in b.processes]
            return out
        out = {
            "id": n["id"],
            "tipo": n["type"],
            "nome": n["name"],
            "descrizione": (n.get("desc") or "")[:300],
        }
        prod, area = self.product_of(node_id), self.area_of(node_id)
        if prod and prod["id"] != node_id:
            out["prodotto"] = {"id": prod["id"], "nome": prod["name"]}
        if area and area["id"] != node_id:
            out["area"] = {"id": area["id"], "nome": area["name"]}
        if n["type"] == "component":
            out.update(
                tipo_componente=n.get("kindLabel"),
                repository=n.get("repo"),
                linguaggi=n.get("lang") or [],
                framework=n.get("fw") or [],
                namespace=n.get("ns"),
                owner=n.get("owners") or [],
                api=len(n.get("apis") or []),
                qa=self.qa_brief(n),
            )
        elif n["type"] == "product":
            comps = self.components_of(n["id"])
            out["componenti"] = [
                {
                    "id": c["id"],
                    "nome": c["name"],
                    "maturita_qa": (c.get("qa") or {}).get("score", 0),
                }
                for c in comps
            ][:40]
            out["processi"] = [{"id": b.id, "nome": b.name} for b in self.processes_of(n["id"])]
            out["documentazione"] = [
                {"titolo": d.get("title"), "url": d.get("url")} for d in n.get("conf") or []
            ][:8]
        elif n["type"] == "area":
            out["prodotti"] = [
                {"id": p["id"], "nome": p["name"]}
                for p in self.nodes.values()
                if p["type"] == "product" and p.get("parent") == n["id"]
            ]
        calls = [
            {"id": e["to"], "nome": self.label(e["to"]), "tipo": e["type"]}
            for e in self.out[node_id]
            if e["type"] in CALL_EDGES
        ]
        called = [
            {"id": e["from"], "nome": self.label(e["from"]), "tipo": e["type"]}
            for e in self.inc[node_id]
            if e["type"] in CALL_EDGES
        ]
        if calls:
            out["chiama"] = calls[:40]
        if called:
            out["chiamato_da"] = called[:40]
        return out
