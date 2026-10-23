"""Turn the per-repository extracts into one infrastructure graph.

- every resource gets a stable id ``<repo>:<stack>:<address>``;
- a ``data`` block is matched, per environment, to the resource with the same Azure type and
  evaluated name in any stack of any repository (also when the resource is created through a
  library module); matched references become ``inferred`` links (rule ``data-name``), the
  others point to one shared *external* resource per (type, name);
- references through ``resource_group_name`` / ``subnet_id`` / ... set the container
  (``parent_id``) instead of drawing an arrow;
- resources whose name matches a component of the map become link proposals for review.

See docs/ea-map/DESIGN-infra.md §3.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.ea.infra.config import (
    ALERT_TYPES,
    BLUEPRINT_CATEGORIES,
    CDN_TYPES,
    CONTAINER_ATTRS,
    CONTAINER_LTYPES,
    LINKABLE_TF_TYPES,
    MODULE_AZURE_TYPES,
    TYPE_ALIASES,
)
from app.services.ea.infra.parser import classify

_MONITOR_ATTRS = ("data_source_id", "scopes", "target_resource_id", "target_resource_ids")
_NOTIFY_ATTRS = ("action", "action_group", "action_group_id", "action_group_ids")
# parent preference when a block references several containers: the most specific wins
_PARENT_RANK = {attr: i for i, attr in enumerate(CONTAINER_ATTRS)}


@dataclass
class InfraGraph:
    resources: dict[str, dict[str, Any]] = field(default_factory=dict)
    links: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)


def canonical(tf_type: str) -> str:
    """Azure types that name the same thing (function_app / linux_function_app...)."""
    return TYPE_ALIASES.get(tf_type, tf_type)


def azure_type(res: dict[str, Any]) -> str | None:
    if res["kind"] == "module":
        t = MODULE_AZURE_TYPES.get(res["tf_type"])
        return canonical(t) if t else None
    return canonical(str(res["tf_type"]))


def _norm(name: str) -> str:
    return name.lower().replace("-", "").replace("_", "")


def _label(res: dict[str, Any]) -> str:
    hosts = (res.get("attrs") or {}).get("hostnames") or []
    if hosts:
        return str(hosts[0])  # a web frontend: its public address
    names = res.get("names") or {}
    for env in ("prod", "uat", "dev"):
        if names.get(env):
            return str(names[env])
    addr = str(res["addr"])
    key = re.search(r'\["([^"]+)"\]$', addr)  # an instance of a for_each block: its key
    return key.group(1) if key else addr.rsplit(".", 1)[-1]


_ENV_PREFIX = re.compile(r"^pagopa-[dup]-(?:(?:weu|itn|neu)-)?")
_KIND_SUFFIX = re.compile(r"(?:-(?:api|apis|fn|function|wa|webapp|web-app|app|mock|v\d+))+$")
_COMPONENT_SUFFIX = re.compile(r"-(?:service|be|fn|function)$")
_FRONTEND_SUFFIX = re.compile(r"-(?:fe|frontend|app)$")
_CDN_WORDS = re.compile(r"(?:^|[-_])(?:cdn|frontdoor|fe|frontend|pagopa)(?=$|[-_])")


def frontend_key(node_id: str) -> str:
    """'pagopa-checkout-fe' -> 'checkout', 'pagopa-selfcare-frontend' -> 'selfcare'."""
    return _FRONTEND_SUFFIX.sub("", node_id.removeprefix("pagopa-")).replace("-", "")


def cdn_keys(res: dict[str, Any], domain: str) -> list[str]:
    """Comparable names of a front door: block name, prefix and first hostname label,
    stack domain ('checkout_cdn_frontdoor' -> 'checkout')."""
    raw = [
        str(res["addr"]).rsplit(".", 1)[-1],
        *(str(v) for v in (res.get("names") or {}).values()),
    ]
    hosts = (res.get("attrs") or {}).get("hostnames") or []
    raw += [str(h).split(".", 1)[0] for h in hosts]
    out = []
    for name in raw:
        core = _ENV_PREFIX.sub("", name.lower().replace("_", "-"))
        core = _CDN_WORDS.sub("-", core).strip("-")
        if core:
            out.append(core.replace("-", ""))
    out.append(domain.replace("-", ""))
    return out


def name_variants(name: str) -> set[str]:
    """Comparable forms of an Azure name: 'pagopa-p-weu-apiconfig-apiconfig-cache-api'
    -> {'apiconfigapiconfigcache', 'apiconfigcache'}."""
    core = _KIND_SUFFIX.sub("", _ENV_PREFIX.sub("", name.lower().replace("_", "-")))
    tokens = [t for t in core.split("-") if t]
    if not tokens:
        return set()
    out = {"".join(tokens)}
    if len(tokens) > 1:
        out.add("".join(tokens[1:]))  # without the domain prefix
    return out


def component_key(node_id: str) -> str:
    return _COMPONENT_SUFFIX.sub("", node_id.removeprefix("pagopa-")).replace("-", "")


def _match_component(
    res: dict[str, Any],
    components: set[str],
    namespaces: dict[str, str],
    frontends: set[str] | None = None,
    domain: str = "",
) -> tuple[str, str] | None:
    """(EA node id, rule) for a resource that names a component, or None."""
    if res["tf_type"] in CDN_TYPES:
        keys = {frontend_key(f): f for f in sorted(frontends or set())}
        for k in cdn_keys(res, domain):
            if len(k) >= 4 and k in keys:
                return keys[k], "frontend"
        return None
    if res["tf_type"] == "kubernetes_namespace":
        ns = (res.get("attrs") or {}).get("namespace") or res["addr"].rsplit(".", 1)[-1]
        node = namespaces.get(str(ns))
        return (node, "namespace") if node else None
    attrs = res.get("attrs") or {}
    image = attrs.get("image")
    if attrs.get("web_frontend") or image:
        # an app running a container: its image names the repository it was built from
        if image in components:
            return str(image), "image"
        compact = {c.replace("-", ""): c for c in sorted(components)}
        if str(image).replace("-", "") in compact:
            return compact[str(image).replace("-", "")], "image"
        if attrs.get("web_frontend") and image:
            keys = {frontend_key(f): f for f in sorted(frontends or set())}
            hit = keys.get(frontend_key(str(image)))
            if hit:
                return hit, "image"
    if res["tf_type"] not in LINKABLE_TF_TYPES:
        return None
    names = {str(v) for v in (res.get("names") or {}).values()}
    chart = (res.get("attrs") or {}).get("chart")
    if chart:
        names.add(str(chart))
    for name in sorted(names):
        if name in components:
            return name, "name"
    keys = {component_key(c): c for c in sorted(components)}
    for name in sorted(names):
        if "mock" in name.lower():
            continue  # a mock of a service is not the service
        for variant in sorted(name_variants(name)):
            if len(variant) >= 4 and variant in keys:
                return keys[variant], "name-pattern"
    return None


def _stack_domain(stack: str) -> str:
    """'src/domains/checkout-app' -> 'checkout'."""
    m = re.search(r"domains/([^/]+)", stack)
    return re.sub(r"-(app|common|secrets?|cron|legacy)$", "", m.group(1)) if m else ""


def build(
    extracts: dict[str, dict[str, Any]],
    *,
    components: set[str] | None = None,
    namespaces: dict[str, str] | None = None,
    blueprint: set[str] | None = None,
    frontends: set[str] | None = None,
) -> InfraGraph:
    """Graph of all repositories. ``components``: ids of the map components;
    ``namespaces``: AKS namespace -> blueprint infra node id (e.g. "nodo" -> "ns-nodo");
    ``blueprint``: ids of the blueprint infra nodes, for the category of each resource."""
    blueprint = blueprint or set()
    components = components or set()
    namespaces = namespaces or {}
    g = InfraGraph()
    by_addr: dict[tuple[str, str, str], str] = {}  # (repo, stack, addr) -> resource id
    by_base: dict[tuple[str, str, str], list[str]] = {}  # for_each block -> its instances
    index: dict[tuple[str, str, str], str] = {}  # (env, azure type, name) -> resource id

    for repo, ext in sorted(extracts.items()):
        for stack, st in sorted((ext.get("stacks") or {}).items()):
            for res in st.get("resources") or []:
                rid = f"{repo}:{stack}:{res['addr']}"
                by_addr[(repo, stack, res["addr"])] = rid
                if res.get("base"):
                    by_base.setdefault((repo, stack, res["base"]), []).append(rid)
                g.resources[rid] = {
                    "id": rid,
                    "repo": repo,
                    "stack": stack,
                    "address": res["addr"],
                    "tf_kind": res["kind"],
                    "tf_type": res["tf_type"],
                    "ltype": res["ltype"],
                    "vendor": res["vendor"],
                    "label": _label(res)[:300],
                    "file": res.get("file"),
                    "envs": list(res.get("envs") or []),
                    "names": dict(res.get("names") or {}),
                    "attrs": {
                        **(res.get("attrs") or {}),
                        **({"location": st["location"]} if st.get("location") else {}),
                    },
                    "parent_id": None,
                    "external": False,
                }
                atype = azure_type(res)
                if atype:
                    for env, name in (res.get("names") or {}).items():
                        if env in res.get("envs", []):
                            index.setdefault((env, atype, name), rid)
                            index.setdefault((env, atype, _norm(name)), rid)
                # resources created inside the module (storage of a front door, event hubs)
                for inner in res.get("inner") or []:
                    for env, name in (inner.get("names") or {}).items():
                        index.setdefault((env, canonical(inner["type"]), name), rid)
                        index.setdefault((env, canonical(inner["type"]), _norm(name)), rid)

    def resolve_data(repo: str, stack: str, envs: list[str], d: dict[str, Any]) -> tuple[str, str]:
        """(resource id, origin) of a data block; external resource when nothing matches."""
        atype = canonical(d["tf_type"])
        hits = {
            index.get((env, atype, name)) or index.get((env, atype, _norm(name)))
            for env, name in (d.get("names") or {}).items()
            if env in envs
        }
        hits.discard(None)
        if hits:
            return sorted(h for h in hits if h)[0], "inferred"
        name = next(iter((d.get("names") or {}).values()), None) or d["addr"].rsplit(".", 1)[-1]
        ext_id = f"external:{d['tf_type']}:{name}"
        if ext_id not in g.resources:
            ltype, vendor = classify("data", d["tf_type"]) or ("other", "generic")
            g.resources[ext_id] = {
                "id": ext_id,
                "repo": repo,
                "stack": stack,
                "address": d["addr"],
                "tf_kind": "data",
                "tf_type": d["tf_type"],
                "ltype": ltype,
                "vendor": vendor,
                "label": str(name)[:300],
                "file": None,
                "envs": list(envs),
                "names": dict(d.get("names") or {}),
                # unresolved: the name cannot be computed from the Terraform; no_match: nobody
                # in the repositories read creates a resource with this name
                "attrs": {"reason": "no_match" if d.get("names") else "unresolved_name"},
                "parent_id": None,
                "external": True,
            }
        else:
            known = g.resources[ext_id]["envs"]
            known.extend(e for e in envs if e not in known)
        return ext_id, "explicit"

    matched = unmatched = 0
    for repo, ext in sorted(extracts.items()):
        for stack, st in sorted((ext.get("stacks") or {}).items()):
            envs = st.get("envs") or []
            data = {d["addr"]: d for d in st.get("data") or []}
            parents: dict[str, tuple[int, str]] = {}
            for ref in st.get("refs") or []:
                src = by_addr.get((repo, stack, ref["from"]))
                if src is None:
                    continue
                origin, rule = "explicit", "reference"
                dst: str | None
                if ref["to"] in data:
                    d = data[ref["to"]]
                    if classify("data", d["tf_type"]) is None:
                        continue  # client config, secrets, AD groups...
                    dst, origin = resolve_data(repo, stack, envs, d)
                    if origin == "inferred":
                        matched += 1
                        rule = "data-name"
                    else:
                        unmatched += 1
                    dsts = [dst]
                else:
                    base = ref["to"].split("[", 1)[0]
                    one = by_addr.get((repo, stack, ref["to"])) or by_addr.get((repo, stack, base))
                    # a reference to a for_each block points to its instances
                    dsts = [one] if one else by_base.get((repo, stack, base), [])[:10]
                for dst in dsts:
                    _link(g, parents, src, dst, ref["attr"], origin, rule)
            for src, (_rank, dst) in parents.items():
                g.resources[src]["parent_id"] = dst
    _finish(g, components, namespaces, blueprint, frontends, matched, unmatched)
    return g


def _link(
    g: InfraGraph,
    parents: dict[str, tuple[int, str]],
    src: str,
    dst: str | None,
    attr: str,
    origin: str,
    rule: str,
) -> None:
    """A reference becomes the container of ``src`` or an arrow."""
    if dst is None or dst == src:
        return
    target = g.resources[dst]
    if attr in CONTAINER_ATTRS and target["ltype"] in CONTAINER_LTYPES:
        rank = _PARENT_RANK[attr]
        if src not in parents or rank < parents[src][0]:
            parents[src] = (rank, dst)
        return
    source = g.resources[src]
    if source["tf_type"] in ALERT_TYPES and attr in _MONITOR_ATTRS:
        kind = "monitors"
    elif attr in _NOTIFY_ATTRS and target["ltype"] == "action_group":
        kind = "notifies"
    else:
        kind = "ref"
    key = (src, dst, kind)
    link_envs = [e for e in source["envs"] if e in target["envs"]]
    if key not in g.links:
        g.links[key] = {
            "from_id": src,
            "to_id": dst,
            "kind": kind,
            "origin": origin,
            "envs": link_envs,
            "evidence": {"file": source["file"], "attr": attr, "rule": rule},
        }


def _finish(
    g: InfraGraph,
    components: set[str],
    namespaces: dict[str, str],
    blueprint: set[str],
    frontends: set[str] | None,
    matched: int,
    unmatched: int,
) -> None:
    """Blueprint category, link proposals and statistics."""
    for rid, res in g.resources.items():
        category = BLUEPRINT_CATEGORIES.get(res["tf_type"]) or (
            BLUEPRINT_CATEGORIES.get("*aws") if res["vendor"] == "aws" else None
        )
        if category in blueprint:
            res["blueprint_id"] = category
        if res["external"]:
            continue
        hit = _match_component(
            {
                "addr": res["address"],
                "tf_type": res["tf_type"],
                "names": res["names"],
                "attrs": res["attrs"],
            },
            components,
            namespaces,
            frontends,
            _stack_domain(res["stack"]),
        )
        if hit:
            g.proposals.append({"resource_id": rid, "node_id": hit[0], "rule": hit[1]})

    g.stats = {
        "resources": sum(1 for r in g.resources.values() if not r["external"]),
        "external": sum(1 for r in g.resources.values() if r["external"]),
        "links": len(g.links),
        "data_matched": matched,
        "data_unmatched": unmatched,
        "alerts": sum(1 for r in g.resources.values() if r["tf_type"] in ALERT_TYPES),
        "proposals": len(g.proposals),
    }
