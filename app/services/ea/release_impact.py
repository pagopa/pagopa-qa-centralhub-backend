"""Impact of a release on the platform, from a GitHub pull request or comparison.

Steps: read the changed files of a repository of the map → classify them (contract,
configuration, data, code, tests…) → walk the graph from the changed component (who calls
it, what it calls, which datasets it feeds) → rate the risk for every node reached → list
the test suites that cover them and the gaps → the SANP processes and services involved.

Read-only: nothing is written, the analysis is computed on demand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from app.services.ea.graph_index import GraphIndex, Hop
from app.services.ea.sync.rules import GITHUB_ORG
from app.services.ea.sync.sources import GitHubClient, SourceError

MAX_FILES = 3000

MAX_URL = 500
# names of GitHub owners/repositories and git refs: anchored, linear-time patterns only
_NAME_RX = re.compile(r"[\w.-]{1,100}")
_REF_RX = re.compile(r"[\w./-]{1,200}")

# (category, label, weight, regex) – first match wins
CATEGORIES: list[tuple[str, str, int, re.Pattern[str]]] = [
    (
        "test",
        "Test",
        0,
        re.compile(
            r"(^|/)(src/test|tests?|__tests__|e2e|integration-test|performance-test|load-test)/|\.(test|spec)\.[jt]sx?$|_test\.go$|\.feature$",
            re.I,
        ),
    ),
    (
        "contract",
        "Contratto API",
        3,
        re.compile(r"(openapi|swagger|asyncapi)[^/]*\.(json|ya?ml)$|\.proto$|\.wsdl$|\.xsd$", re.I),
    ),
    (
        "data",
        "Dati e schema",
        3,
        re.compile(r"(migration|liquibase|flyway|changelog|alembic/versions)|\.sql$", re.I),
    ),
    (
        "config",
        "Configurazione e deploy",
        2,
        re.compile(
            r"(^|/)(helm|\.identity|infra|charts?)/|values-[\w-]+\.ya?ml$|\.tf(vars)?$|(^|/)Dockerfile|application[\w-]*\.(ya?ml|properties)$|(^|/)\.env",
            re.I,
        ),
    ),
    ("ci", "Pipeline", 1, re.compile(r"^\.github/|^\.devops/|azure-pipelines", re.I)),
    (
        "docs",
        "Documentazione",
        0,
        re.compile(r"\.(md|adoc|txt|png|jpe?g|svg)$|^docs?/|(^|/)(CODEOWNERS|LICENSE)$", re.I),
    ),
    ("code", "Codice", 2, re.compile(r".*")),
]
RISK_ORDER = {"nessuno": 0, "basso": 1, "medio": 2, "alto": 3}
_LOWER = {"alto": "medio", "medio": "basso", "basso": "basso", "nessuno": "nessuno"}


class ImpactError(ValueError):
    """The request cannot be analysed (bad link, repository not in the map, GitHub error)."""


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    additions: int = 0
    deletions: int = 0


@dataclass
class ChangeSet:
    repo: str
    title: str
    url: str
    ref: str  # "PR #12" or "base...head"
    state: str | None
    files: list[ChangedFile] = field(default_factory=list)


class ChangeSource(Protocol):
    async def pull_request(self, repo: str, number: int) -> ChangeSet: ...

    async def compare(self, repo: str, base: str, head: str) -> ChangeSet: ...


class GitHubChanges:
    """Pull requests and comparisons of the org repositories, over the GitHub REST API."""

    def __init__(self, client: GitHubClient) -> None:
        self._gh = client
        self._api = "https://api.github.com"

    async def _json(self, url: str, **params: Any) -> Any:
        resp = await self._gh._request("GET", url, params=params or None)
        if resp.status_code == 404:
            raise ImpactError(
                "Non trovato su GitHub: controlla repository, numero della PR o riferimenti"
            )
        if resp.status_code != 200:
            raise ImpactError(f"GitHub ha risposto HTTP {resp.status_code}")
        return resp

    async def pull_request(self, repo: str, number: int) -> ChangeSet:
        base = f"{self._api}/repos/{GITHUB_ORG}/{repo}/pulls/{number}"
        pr = (await self._json(base)).json()
        state = "merged" if pr.get("merged_at") else pr.get("state")
        cs = ChangeSet(
            repo=repo,
            title=pr.get("title") or "",
            url=pr.get("html_url") or "",
            ref=f"PR #{number}",
            state=state,
        )
        url: str | None = f"{base}/files?per_page=100"
        while url and len(cs.files) < MAX_FILES:
            resp = await self._json(url)
            for f in resp.json():
                cs.files.append(
                    ChangedFile(
                        f["filename"],
                        f.get("status", ""),
                        f.get("additions", 0),
                        f.get("deletions", 0),
                    )
                )
            url = resp.links.get("next", {}).get("url")
        return cs

    async def compare(self, repo: str, base: str, head: str) -> ChangeSet:
        url = f"{self._api}/repos/{GITHUB_ORG}/{repo}/compare/{base}...{head}"
        data = (await self._json(url)).json()
        cs = ChangeSet(
            repo=repo,
            title=f"{base} → {head} · {data.get('total_commits', 0)} commit",
            url=data.get("html_url")
            or f"https://github.com/{GITHUB_ORG}/{repo}/compare/{base}...{head}",
            ref=f"{base}...{head}",
            state=data.get("status"),
        )
        for f in data.get("files") or []:
            cs.files.append(
                ChangedFile(
                    f["filename"], f.get("status", ""), f.get("additions", 0), f.get("deletions", 0)
                )
            )
        return cs


@dataclass(frozen=True)
class ImpactRequest:
    repo: str
    number: int | None = None
    base: str | None = None
    head: str | None = None


def parse_request(
    url: str | None = None,
    repo: str | None = None,
    base: str | None = None,
    head: str | None = None,
) -> ImpactRequest:
    if url:
        return _parse_url(url)
    if repo and base and head:
        return ImpactRequest(
            repo=repo.strip().rsplit("/", 1)[-1], base=base.strip(), head=head.strip()
        )
    raise ImpactError("Indica il link di una pull request, oppure repository, base e head")


def _parse_url(url: str) -> ImpactRequest:
    """A GitHub pull request or comparison link, parsed by its path segments.

    No regular expression runs on the whole user-provided text (CodeQL py/polynomial-redos):
    the URL is split by ``urlsplit`` and each segment is checked with an anchored pattern."""
    bad = ImpactError(
        "Link non riconosciuto: incolla l’URL di una pull request o di un confronto GitHub"
    )
    text = url.strip()
    if len(text) > MAX_URL:
        raise bad
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
        raise bad
    segs = [x for x in parts.path.split("/") if x]
    if len(segs) < 4 or not (_NAME_RX.fullmatch(segs[0]) and _NAME_RX.fullmatch(segs[1])):
        raise bad
    owner, name, kind = segs[0], segs[1], segs[2]
    if kind == "pull" and segs[3].isdigit() and len(segs[3]) <= 9:
        _check_owner(owner)
        return ImpactRequest(repo=name, number=int(segs[3]))
    if kind == "compare":
        spec = "/".join(segs[3:])  # refs may contain "/" (e.g. release/1.2)
        base, sep, head = spec.partition("...")
        if sep and _REF_RX.fullmatch(base) and _REF_RX.fullmatch(head):
            _check_owner(owner)
            return ImpactRequest(repo=name, base=base, head=head)
    raise bad


def _check_owner(owner: str) -> None:
    if owner.lower() != GITHUB_ORG:
        raise ImpactError(f"Sono analizzabili solo i repository dell’organizzazione {GITHUB_ORG}")


def classify(files: list[ChangedFile]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        for key, label, weight, rx in CATEGORIES:
            if rx.search(f.path):
                c = out.setdefault(
                    key, {"key": key, "label": label, "weight": weight, "files": 0, "examples": []}
                )
                c["files"] += 1
                if len(c["examples"]) < 5:
                    c["examples"].append(f.path)
                break
    return out


def change_risk(cats: dict[str, dict[str, Any]]) -> str:
    """Risk of the change itself: what kind of files it touches."""
    if not cats:
        return "nessuno"
    if "contract" in cats or "data" in cats:
        return "alto"
    if "code" in cats or "config" in cats:
        return "medio"
    if "ci" in cats:
        return "basso"
    return "nessuno"


def _hop_out(g: GraphIndex, hop: Hop, relation: str, risk: str, reason: str) -> dict[str, Any]:
    n = g.nodes[hop.id]
    prod = g.product_of(hop.id)
    return {
        "id": hop.id,
        "name": n["name"],
        "type": n["type"],
        "product": prod["name"] if prod and prod["id"] != hop.id else None,
        "relation": relation,
        "depth": hop.depth,
        "via": hop.via,
        "path": list(hop.path),
        "risk": risk,
        "reason": reason,
        "qa_score": (n.get("qa") or {}).get("score") if n["type"] == "component" else None,
    }


def _merge_duplicates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A node reached in more than one way (e.g. it calls the changed component and is also
    called by it) is listed once, with the highest risk and every relation."""
    merged: dict[str, dict[str, Any]] = {}
    for x in items:
        cur = merged.get(x["id"])
        if cur is None:
            merged[x["id"]] = {**x, "relations": [x["relation"]]}
            continue
        if x["relation"] not in cur["relations"]:
            cur["relations"].append(x["relation"])
        if (RISK_ORDER[x["risk"]], -x["depth"]) > (RISK_ORDER[cur["risk"]], -cur["depth"]):
            cur.update({k: x[k] for k in ("relation", "depth", "via", "path", "risk", "reason")})
    return list(merged.values())


def _has_integration(n: dict[str, Any]) -> bool:
    qa = n.get("qa") or {}
    return bool(qa.get("it_repo") or qa.get("it_wf") or qa.get("central") or qa.get("nodo_bdd"))


def analyse(g: GraphIndex, cs: ChangeSet) -> dict[str, Any]:
    comp_id = g.resolve(cs.repo)
    if comp_id is None or g.nodes[comp_id]["type"] != "component":
        raise ImpactError(
            f"Il repository {cs.repo} non è nella mappa: non posso calcolarne l’impatto"
        )
    comp = g.nodes[comp_id]
    cats = classify(cs.files)
    risk = change_risk(cats)
    contract = "contract" in cats or "data" in cats

    impacted: list[dict[str, Any]] = []
    if risk != "nessuno":
        for hop in g.callers(comp_id, depth=3):
            r = risk if hop.depth == 1 else _LOWER[risk] if hop.depth == 2 else "basso"
            why = (
                "chiama direttamente il componente modificato"
                if hop.depth == 1
                else f"dipende dal componente modificato attraverso {hop.depth - 1} passaggi"
            )
            if contract and hop.depth == 1:
                why += ", il cui contratto o schema cambia"
            impacted.append(_hop_out(g, hop, "chiamante", r, why))
        for hop in g.downstream_data(comp_id, depth=3):
            r = "alto" if contract and hop.depth == 1 else "medio" if hop.depth == 1 else "basso"
            impacted.append(
                _hop_out(
                    g, hop, "dato a valle", r, "riceve dati prodotti dal componente modificato"
                )
            )
        cfg = "config" in cats
        for hop in g.dependencies(comp_id, depth=1):
            impacted.append(
                _hop_out(
                    g,
                    hop,
                    "dipendenza",
                    "medio" if cfg else "basso",
                    "il componente modificato lo chiama"
                    + (" e la sua configurazione cambia" if cfg else ""),
                )
            )
    impacted = _merge_duplicates(impacted)
    impacted.sort(key=lambda x: (-RISK_ORDER[x["risk"]], x["depth"], x["name"]))

    # suites that cover the changed component and the impacted ones (medio/alto first)
    suites: dict[str, dict[str, Any]] = {}

    def add(key: str, name: str, kind: str, node_id: str, prio: int) -> None:
        s = suites.setdefault(key, {"name": name, "kind": kind, "covers": [], "priority": prio})
        if node_id not in s["covers"]:
            s["covers"].append(node_id)
        s["priority"] = min(s["priority"], prio)

    targets = [(comp_id, 0)] + [
        (x["id"], 1 if x["risk"] in ("alto", "medio") else 2) for x in impacted
    ]
    for nid, prio in targets:
        n = g.nodes[nid]
        if n["type"] != "component":
            continue
        qa = n.get("qa") or {}
        for c in qa.get("central") or []:
            add(f"central:{c}", c, "suite centrale", nid, prio)
        for c in qa.get("nrt") or []:
            add(f"nrt:{c}", f"NRT {c}", "regressione notturna", nid, prio)
        if qa.get("nodo_bdd"):
            add("bdd:nodo", "Nodo BDD", "BDD del Nodo", nid, prio)
        if qa.get("it_repo") or qa.get("it_wf"):
            add(f"repo:{nid}", f"Integration test di {n['name']}", "test nel repository", nid, prio)
        if nid == comp_id and qa.get("perf_repo") and risk in ("medio", "alto"):
            add(f"perf:{nid}", f"Performance test di {n['name']}", "performance", nid, prio)
    suite_list = sorted(
        suites.values(), key=lambda s: (s["priority"], -len(s["covers"]), s["name"])
    )

    gaps = []
    for nid in [comp_id] + [x["id"] for x in impacted if x["risk"] in ("alto", "medio")]:
        n = g.nodes[nid]
        if (
            n["type"] == "component"
            and n.get("kind") in ("be", "fn", "fe")
            and not _has_integration(n)
        ):
            gaps.append(
                {
                    "id": nid,
                    "name": n["name"],
                    "qa_score": (n.get("qa") or {}).get("score", 0),
                    "reason": "nessun test di integrazione trovato",
                }
            )

    # business view: products touched (changed + high risk) → SANP processes → services
    products: list[str] = []
    for nid in [comp_id] + [x["id"] for x in impacted if x["risk"] == "alto"]:
        p = g.product_of(nid)
        if p and p["id"] not in products:
            products.append(p["id"])
    processes: dict[str, dict[str, Any]] = {}
    for pid in products:
        for b in g.processes_of(pid):
            e = processes.setdefault(
                b.id, {"id": b.id, "name": b.name, "services": b.services, "ref": b.ref, "via": []}
            )
            if g.label(pid) not in e["via"]:
                e["via"].append(g.label(pid))

    level = risk
    if (
        risk == "medio"
        and sum(1 for x in impacted if x["relation"] == "chiamante" and x["depth"] == 1) >= 5
    ):
        level = "alto"

    prod = g.product_of(comp_id)
    return {
        "change": {
            "repo": cs.repo,
            "title": cs.title,
            "url": cs.url,
            "ref": cs.ref,
            "state": cs.state,
            "files": len(cs.files),
            "additions": sum(f.additions for f in cs.files),
            "deletions": sum(f.deletions for f in cs.files),
        },
        "component": {
            "id": comp_id,
            "name": comp["name"],
            "product": prod["name"] if prod else None,
            "qa_score": (comp.get("qa") or {}).get("score", 0),
        },
        "risk": level,
        "categories": sorted(cats.values(), key=lambda c: (-c["weight"], -c["files"])),
        "impacted": impacted,
        "suites": suite_list,
        "gaps": gaps,
        "processes": sorted(processes.values(), key=lambda p: p["name"]),
        "highlight": list(
            dict.fromkeys([comp_id] + [x["id"] for x in impacted if x["risk"] in ("alto", "medio")])
        ),
    }


async def release_impact(g: GraphIndex, source: ChangeSource, req: ImpactRequest) -> dict[str, Any]:
    if g.resolve(req.repo) is None:
        raise ImpactError(
            f"Il repository {req.repo} non è nella mappa: non posso calcolarne l’impatto"
        )
    try:
        if req.number is not None:
            cs = await source.pull_request(req.repo, req.number)
        else:
            cs = await source.compare(req.repo, req.base or "", req.head or "")
    except SourceError as exc:
        raise ImpactError(str(exc)) from exc
    return analyse(g, cs)
