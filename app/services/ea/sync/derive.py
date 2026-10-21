"""From repository facts and Confluence pages to nodes, edges, QA signals and doc links.

Pure functions mirroring ``build_kb.py`` of the discovery scripts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.ea.sync.rules import (
    CENTRAL_SUITES,
    CONF_KW,
    CONFLUENCE_BASE,
    EXTRA_CONF,
    NODO_BDD,
    NRT_SUITES,
    STORE_MAP,
    TAS_REPO,
    URL_MAP,
)

_BAD_NS_PREFIXES = ("<", "your", "TODO", "project")


def deps_for(repo: str, facts: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Edges read from the Helm configuration: ``{target: (type, evidence url)}``."""
    out: dict[str, tuple[str, str]] = {}
    for url in facts.get("env_urls") or []:
        for rx, target in URL_MAP:
            if re.search(rx, url) and target != repo:
                out[target] = ("api", url)
                break
        for rx, store_id, _name in STORE_MAP:
            if re.search(rx, url):
                out[store_id] = ("data", url)
    return out


def central_suites_for(repo: str) -> list[str]:
    return [suite for suite, comps in CENTRAL_SUITES.items() if repo in comps]


def qa_observed(repo: str, facts: Mapping[str, Any]) -> dict[str, Any]:
    """The QA signals owned by the sync (column names of ``ea_qa_signals``)."""
    tests = facts.get("tests") or {}
    wf = facts.get("workflows") or {}
    central = central_suites_for(repo)
    return {
        "unit_files": int(tests.get("unit_files") or 0),
        "it_features": int(tests.get("features") or 0),
        "it_workflow": any(v.get("it") for v in wf.values()),
        "perf": int(tests.get("perf_files") or 0) > 0,
        "e2e": int(tests.get("e2e_files") or 0) > 0,
        "sonar": any(v.get("sonar") for v in wf.values()),
        "central_suites": central,
        "nrt_suites": [k for k, suite in NRT_SUITES.items() if suite in central],
        "nodo_bdd": repo in NODO_BDD,
        "tas_ci_observed": bool(facts.get("tas_ci")) and repo != TAS_REPO,
    }


def component_attrs(facts: Mapping[str, Any]) -> dict[str, Any]:
    """The component attributes owned by the sync (keys of ``ea_nodes.attrs``)."""
    ns = facts.get("ns")
    if not ns or str(ns).startswith(_BAD_NS_PREFIXES):
        ns = facts.get("id_domain")
    ingress = facts.get("ingress")
    wf: dict[str, Any] = facts.get("workflows") or {}
    return {
        "lang": list(facts.get("lang") or []),
        "fw": list(facts.get("fw") or []),
        "ns": ns,
        "ingress": list(ingress) if ingress else None,
        "owners": [o.replace("@pagopa/", "") for o in facts.get("owners") or []],
        "apis": [{"f": a["file"], "t": a.get("title")} for a in facts.get("apis") or []][:12],
        "readme": facts.get("readme") or "",
        "workflows": [
            {"f": k, "n": wf[k].get("name"), "cron": wf[k].get("cron")}
            for k in (facts.get("workflow_order") or list(wf))
            if k in wf
        ][:30],
    }


def guess_ctype(repo: str, facts: Mapping[str, Any]) -> str:
    name = repo.lower()
    fw = set(facts.get("fw") or [])
    if "mock" in name:
        return "mock"
    if re.search(r"(^|-)(tests?|e2e|qa)(-|$)", name):
        return "test"
    if "Azure Functions" in fw or name.endswith(("-function", "-functions")):
        return "fn"
    if fw & {"React", "Next.js"} or name.endswith(("-fe", "-frontend")):
        return "fe"
    if int(facts.get("infra_tf") or 0) > 10 and not facts.get("dockerfile"):
        return "infra"
    if facts.get("dockerfile") or facts.get("helm"):
        return "be"
    return "lib"


def guess_product(
    repo: str,
    facts: Mapping[str, Any],
    product_domains: Mapping[str, str],
    component_products: Mapping[str, str],
) -> str | None:
    """Proposed product for a new repository.

    The product of the known component sharing the longest name prefix (at least two
    dash-separated words, e.g. ``pagopa-gpd-…``) wins when it is compatible with the
    Helm namespace / identity domain; otherwise the product owning that domain; otherwise
    the name match alone.
    """
    words = repo.split("-")
    best: tuple[int, str] | None = None
    for comp, product in component_products.items():
        common = 0
        for a, b in zip(words, comp.split("-"), strict=False):
            if a != b:
                break
            common += 1
        if common >= 2 and (best is None or common > best[0]):
            best = (common, product)
    by_name = best[1] if best else None
    domains = {v for v in (facts.get("ns"), facts.get("id_domain")) if v}
    by_domain = sorted(p for p, d in product_domains.items() if d in domains)
    if by_name and (not by_domain or by_name in by_domain):
        return by_name
    if by_domain:
        return by_domain[0]
    return by_name


_ACRONYMS = frozenset(
    "aca afm api arc be bdi cdc cup ec emd fdr fe fn gpd gps io iuv jwt kpi mbd ndp npg nrt "
    "pdf pos psp qa qi rtp sert srtp tas ui".split()
)


def humanize(repo: str) -> str:
    words = repo.removeprefix("pagopa-").replace("_", "-").split("-")
    return " ".join(w.upper() if w in _ACRONYMS else w.capitalize() for w in words if w)


# ---- Confluence ------------------------------------------------------------------------


@dataclass
class SpaceIndex:
    """Level-1/2 pages of a space with the size of their subtree."""

    key: str
    pages: list[dict[str, str]]
    _kids: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    levels: dict[str, tuple[dict[str, str], int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        ids = {p["id"] for p in self.pages}
        for p in self.pages:
            self._kids.setdefault(p.get("par") or "", []).append(p)
        for root in (p for p in self.pages if (p.get("par") or "") not in ids):
            for a in self._kids.get(root["id"], []):
                self.levels[a["id"]] = (a, 1)
                for b in self._kids.get(a["id"], []):
                    self.levels[b["id"]] = (b, 2)

    def subtree(self, page_id: str) -> int:
        stack, n = [page_id], 0
        while stack:
            for kid in self._kids.get(stack.pop(), []):
                n += 1
                stack.append(kid["id"])
        return n


def conf_links(product: str, spaces: Mapping[str, SpaceIndex]) -> list[dict[str, Any]]:
    """Confluence pages linked to a product by the title rules (max 8, largest first)."""
    out: list[dict[str, Any]] = []
    for space, rx in CONF_KW.get(product, []):
        idx = spaces.get(space)
        if idx is None:
            continue
        for page, _level in idx.levels.values():
            if re.search(rx, page["t"]):
                out.append(
                    {
                        "space": space,
                        "id": page["id"],
                        "title": page["t"],
                        "pages": idx.subtree(page["id"]) + 1,
                        "url": f"{CONFLUENCE_BASE}/{space}/pages/{page['id']}",
                    }
                )
    for space, page_id, title in EXTRA_CONF.get(product, []):
        out.append(
            {
                "space": space,
                "id": page_id,
                "title": title,
                "pages": 1,
                "url": f"{CONFLUENCE_BASE}/{space}/pages/{page_id}",
            }
        )
    seen: set[str] = set()
    res = []
    for o in sorted(out, key=lambda o: -o["pages"]):
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        res.append(o)
    return res[:8]


def owners_overlap(owners: Iterable[str], known_teams: set[str]) -> bool:
    return any(o.replace("@pagopa/", "") in known_teams for o in owners)
