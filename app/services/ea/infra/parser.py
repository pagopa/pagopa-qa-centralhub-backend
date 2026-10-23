"""Parse the Terraform of one repository into a JSON-serialisable extract.

The extract is what the sync stores in the repository snapshot: per stack (folder) the
resources with their logical type, the environments where they exist, their Azure names per
environment, the ``data`` blocks (to be matched across stacks) and the references between
blocks. No tfvars value is kept except the evaluated names; GUIDs and IP addresses are
masked everywhere (docs/ea-map/DESIGN-infra.md §3-4).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from posixpath import dirname
from typing import Any
from urllib.parse import urlparse

import hcl2

from app.services.ea.infra.config import ALERT_TYPES, CDN_TYPES, ENV_SHORT, ENVS, TYPE_RULES

_RULES = [(lt, vendor, re.compile(rx)) for lt, vendor, rx in TYPE_RULES]
_REF = re.compile(
    r"\b(?:(data)\.)?((?:azurerm|aws|kubernetes|helm|azuread|grafana)_[a-z0-9_]+)\.([A-Za-z0-9_-]+)"
    r'|\bmodule\.([A-Za-z0-9_-]+)(?:\[\\?"([^"\\]+)\\?"\])?'
)
_INTERP = re.compile(r"\$\{([^{}]+)\}")
_SIMPLE = re.compile(r"^(var|local)\.([A-Za-z0-9_]+)$")
_FIELD = re.compile(r"^(var|local)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$")
# App settings that only a web frontend has (Next.js, Create React App, Vite)
_WEB_SETTINGS = re.compile(r"^(NEXT_PUBLIC_|NEXTAUTH_|REACT_APP_|VITE_)")
APP_TYPES = (
    "app_service",
    "IDH/app_service_webapp",
    "azurerm_linux_web_app",
    "azurerm_windows_web_app",
    "azurerm_app_service",
    "function_app",
    "IDH/function_app",
    "azurerm_linux_function_app",
    "container_app",
)
_STRING = re.compile(r'^"([^"]*)"$')
_CALL = re.compile(r"^([a-z]+)\((.*)\)$", re.S)
_EACH = re.compile(r'^each\.(key|value)(?:\.([A-Za-z0-9_]+)|\["([^"]+)"\])?$')
# Expanded instances of a for_each block (more would be noise, not architecture)
MAX_FOR_EACH = 30
_TERNARY = re.compile(r"^(.+?)\s*\?\s*(\d+)\s*:\s*(\d+)$")
_COMPARE = re.compile(r"^(.+?)\s*(==|!=)\s*(.+)$")
_CHOICE = re.compile(r'^(.+?)\s*\?\s*("[^"]*"|\S+)\s*:\s*("[^"]*"|\S+)$')
_GUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b")
MASK = "•••"
# Bumped when the extract changes shape or content: snapshots of an older parser are re-read.
PARSER_VERSION = 5
_MAX_DEPTH = 12


def mask(value: str) -> str:
    """Hide subscription/tenant ids and IP addresses from any stored string."""
    return _IP.sub(MASK, _GUID.sub(MASK, value))


def unquote(obj: Any) -> Any:
    """python-hcl2 >= 7 keeps the quotes of labels and string literals: strip them."""
    if isinstance(obj, str):
        return obj[1:-1] if len(obj) >= 2 and obj[0] == obj[-1] == '"' else obj
    if isinstance(obj, list):
        return [unquote(x) for x in obj]
    if isinstance(obj, dict):
        return {unquote(k): unquote(v) for k, v in obj.items() if k != "__is_block__"}
    return obj


def module_library(source: str) -> str:
    """'./.terraform/modules/__v4__//kubernetes_cluster' -> 'kubernetes_cluster'."""
    s = re.sub(r"\?ref=.*", "", source)
    if "__v3__/" in s or "__v4__/" in s:
        return s.split("__/", 1)[1].lstrip("/")
    tail = s.split("://", 1)[-1]
    if "//" in tail:
        return tail.rsplit("//", 1)[1]
    # a remote module: compare the host exactly, never as a substring of the source
    host = urlparse(s.removeprefix("git::")).hostname or s.split("/", 1)[0]
    if s.startswith("git::") or host == "github.com":
        return s.rstrip("/").rsplit("/", 1)[1].removesuffix(".git")
    return s


def classify(kind: str, tf_type: str) -> tuple[str, str] | None:
    """Logical type and vendor, or None for blocks that are not components (noise)."""
    key = f"{kind}:{tf_type}"
    for ltype, vendor, rx in _RULES:
        if rx.search(key):
            return ltype, vendor
    return None


def env_of(folder: str) -> str | None:
    """'weu-prod' -> 'prod', 'dev' -> 'dev', anything else -> None."""
    tail = folder.rsplit("-", 1)[-1]
    return tail if tail in ENVS else None


def _balanced(expr: str) -> bool:
    """True when ``expr`` has no brace closing before it opens (``a ? "${b}" : c``)."""
    level = 0
    for ch in expr:
        level += ch == "{"
        level -= ch == "}"
        if level < 0:
            return False
    return level == 0


def _split_args(args: str) -> list[str]:
    """Top-level comma separated arguments of a call (quotes, brackets and calls kept)."""
    out, depth, quoted, cur = [], 0, False, ""
    for i, ch in enumerate(args):
        if ch == '"' and (i == 0 or args[i - 1] != "\\"):
            quoted = not quoted
        elif not quoted and ch in "([{":
            depth += 1
        elif not quoted and ch in ")]}":
            depth -= 1
        elif not quoted and depth == 0 and ch == ",":
            out.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


class Scope:
    """Best-effort evaluation of names: var.*, local.*, each.*, interpolation, conditionals
    and a few string functions (format, replace, lower, upper, trimspace, join)."""

    def __init__(
        self,
        variables: dict[str, Any],
        local_values: dict[str, Any],
        each: tuple[str, Any] | None = None,
        cache: dict[str, Any] | None = None,
    ):
        self.vars = variables
        self.locals = local_values
        self.each = each
        self._cache: dict[str, Any] = cache if cache is not None else {}

    def with_each(self, key: str, value: Any) -> Scope:
        """The scope of one instance of a for_each block (locals are shared)."""
        return Scope(self.vars, self.locals, (key, value), self._cache)

    def value(self, raw: Any, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            return None
        if isinstance(raw, bool | int | float):
            return raw
        if not isinstance(raw, str):
            return None
        whole = _INTERP.fullmatch(raw)
        if whole:
            return self.expr(whole.group(1).strip(), depth)
        if raw.startswith("${") and raw.endswith("}") and _balanced(raw[2:-1]):
            return self.expr(raw[2:-1].strip(), depth)  # one expression with nested strings
        failed = False

        def sub(m: re.Match[str]) -> str:
            nonlocal failed
            r = self.expr(m.group(1).strip(), depth)
            if isinstance(r, bool) or not isinstance(r, str | int | float):
                failed = True
                return ""
            return str(r)

        out = _INTERP.sub(sub, raw)
        return None if failed or "${" in out else out

    def expr(self, e: str, depth: int) -> Any:
        fld = _FIELD.match(e)
        if fld:  # var.x.attr / local.x.attr on a map
            obj = self.raw(f"${{{fld.group(1)}.{fld.group(2)}}}")
            return self.value(obj.get(fld.group(3)), depth + 1) if isinstance(obj, dict) else None
        m = _SIMPLE.match(e)
        if m:
            kind, name = m.groups()
            if kind == "var":
                return self.value(self.vars.get(name), depth + 1)
            if name not in self._cache:
                self._cache[name] = None  # guards against cycles
                self._cache[name] = self.value(self.locals.get(name), depth + 1)
            return self._cache[name]
        s = _STRING.match(e)
        if s:
            return self.value(s.group(1), depth + 1)
        if e in ("true", "false"):
            return e == "true"
        if e.isdigit():
            return int(e)
        c = _CHOICE.match(e)
        if c:
            truth = self.condition(c.group(1).strip())
            if truth is None:
                return None
            return self.expr(c.group(2) if truth else c.group(3), depth + 1)
        m2 = _EACH.match(e)
        if m2 and self.each is not None:
            key, value = self.each
            if m2.group(1) == "key":
                return key
            attr = m2.group(2) or m2.group(3)
            if attr is None:
                return self.value(value, depth + 1) if not isinstance(value, dict) else None
            return self.value(value.get(attr), depth + 1) if isinstance(value, dict) else None
        call = _CALL.match(e)
        if call:
            return self.call(call.group(1), _split_args(call.group(2)), depth)
        return None

    def call(self, fn: str, args: list[str], depth: int) -> Any:
        if fn == "join" and len(args) == 2 and args[1].startswith("[") and args[1].endswith("]"):
            sep = self.expr(args[0], depth + 1)
            items = [self.expr(a, depth + 1) for a in _split_args(args[1][1:-1])]
            if isinstance(sep, str) and all(isinstance(i, str) for i in items):
                return sep.join(items)
            return None
        vals = [self.expr(a, depth + 1) for a in args]
        if any(isinstance(v, bool) or not isinstance(v, str | int) for v in vals):
            return None
        strs = [str(v) for v in vals]
        try:
            if fn == "format" and strs:
                return re.sub(r"%[sd]", "{}", strs[0]).format(*strs[1:])
            if fn == "replace" and len(strs) == 3:
                old, new = strs[1], strs[2]
                if len(old) > 1 and old.startswith("/") and old.endswith("/"):
                    return re.sub(old[1:-1], new, strs[0])
                return strs[0].replace(old, new)
            if fn in ("lower", "upper", "trimspace") and len(strs) == 1:
                if fn == "lower":
                    return strs[0].lower()
                return strs[0].upper() if fn == "upper" else strs[0].strip()
        except (IndexError, ValueError, re.error):
            return None
        return None

    def raw(self, value: Any, depth: int = 0) -> Any:
        """The unevaluated object behind ``${local.x}`` / ``${var.x}`` (lists, maps)."""
        if depth > _MAX_DEPTH or not isinstance(value, str):
            return value
        whole = _INTERP.fullmatch(value)
        m = _SIMPLE.match(whole.group(1).strip()) if whole else None
        if not m:
            return value
        kind, name = m.groups()
        return self.raw((self.vars if kind == "var" else self.locals).get(name), depth + 1)

    def condition(self, cond: str) -> bool | None:
        """``a == b`` / ``a != b`` / a boolean; None when it cannot be evaluated."""
        c = _COMPARE.match(cond)
        if c:
            left, right = self.expr(c.group(1).strip(), 0), self.expr(c.group(3).strip(), 0)
            if left is None or right is None:
                return None
            return bool((left == right) == (c.group(2) == "=="))
        v = self.expr(cond, 0)
        return v if isinstance(v, bool) else None

    def present(self, count: Any) -> bool:
        """Whether a block with this ``count`` exists (unknown expressions: yes)."""
        if count is None:
            return True
        if isinstance(count, int) and not isinstance(count, bool):
            return count > 0
        if not isinstance(count, str):
            return True
        inner = _INTERP.fullmatch(count)
        e = inner.group(1).strip() if inner else count.strip()
        t = _TERNARY.match(e)
        if not t:
            v = self.expr(e, 0)
            return v > 0 if isinstance(v, int) and not isinstance(v, bool) else True
        cond, yes, no = t.group(1).strip(), int(t.group(2)), int(t.group(3))
        c = _COMPARE.match(cond)
        if c:
            left, right = self.expr(c.group(1).strip(), 0), self.expr(c.group(3).strip(), 0)
            if left is None or right is None:
                return True
            truth = (left == right) == (c.group(2) == "==")
        else:
            v = self.expr(cond, 0)
            if not isinstance(v, bool):
                return True
            truth = v
        return (yes if truth else no) > 0


def _strings(obj: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """(top-level attribute, string) for every string in a block body."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, path or k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, path)


def _refs(body: dict[str, Any]) -> list[tuple[str, str]]:
    """(attribute, address) of every block referenced by a body, in order, deduplicated."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for attr, s in _strings(body):
        if attr in ("count", "for_each", "depends_on", "tags", "provider", "providers"):
            continue
        for m in _REF.finditer(s):
            if m.group(4):
                # module.x["key"]: one instance of a for_each module
                addr = (
                    f'module.{m.group(4)}["{m.group(5)}"]' if m.group(5) else f"module.{m.group(4)}"
                )
            else:
                addr = ("data." if m.group(1) else "") + f"{m.group(2)}.{m.group(3)}"
            if (attr, addr) not in seen:
                seen.add((attr, addr))
                out.append((attr, addr))
    return out


def _channels(body: dict[str, Any]) -> list[str]:
    out: set[str] = set()
    for r in body.get("email_receiver") or []:
        out.add("slack" if "slack" in str(r).lower() else "email")
    for r in body.get("webhook_receiver") or []:
        u = str(r).lower()
        out.add("opsgenie" if "opsgenie" in u else "slack" if "slack" in u else "webhook")
    if body.get("sms_receiver"):
        out.add("sms")
    if body.get("logic_app_receiver"):
        out.add("logic_app")
    return sorted(out)


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def _attrs(
    tf_type: str, body: dict[str, Any], scope: Scope | None, network_details: bool
) -> dict[str, Any]:
    """Few, non-sensitive attributes per type (strings are masked)."""
    a: dict[str, Any] = {}

    def ev(key: str) -> Any:
        raw = body.get(key)
        v = scope.value(raw) if scope is not None else None
        return v if v is not None else (raw if isinstance(raw, int | bool) else None)

    if tf_type in ALERT_TYPES:
        for key in ("severity", "frequency", "evaluation_frequency", "time_window", "window_size"):
            v = ev(key)
            if v is not None:
                a[key] = v if isinstance(v, int) else mask(str(v))[:40]
        desc = ev("description")
        if isinstance(desc, str):
            a["description"] = mask(desc)[:300]
        if tf_type.startswith("azurerm_monitor_scheduled_query"):
            a["kind"] = "log"
        elif tf_type == "azurerm_monitor_metric_alert":
            a["kind"] = "metric"
            crit = _first(body.get("criteria"))
            if isinstance(crit, dict):
                for key in ("metric_name", "aggregation", "operator", "threshold"):
                    v = scope.value(crit.get(key)) if scope else None
                    if v is not None:
                        a[key] = v if isinstance(v, int | float) else mask(str(v))[:80]
        else:
            a["kind"] = "activity"
    elif tf_type == "azurerm_monitor_action_group":
        a["channels"] = _channels(body)
        short = ev("short_name")
        if isinstance(short, str):
            a["short_name"] = mask(short)[:40]
    elif tf_type in APP_TYPES:
        # what runs there: the container image (no registry, no tag) and whether the app
        # settings are those of a web frontend (only the keys are read, never the values)
        image = ev("docker_image")
        if isinstance(image, str) and image:
            a["image"] = mask(image.rsplit("/", 1)[-1].split(":", 1)[0])[:120]
        settings = body.get("app_settings")
        if isinstance(settings, dict) and any(_WEB_SETTINGS.match(str(k)) for k in settings):
            a["web_frontend"] = True
    elif tf_type == "helm_release":
        for key in ("chart", "namespace", "version"):
            v = ev(key)
            if isinstance(v, str):
                a[key] = mask(v)[:120]
    elif tf_type in ("kubernetes_namespace",):
        meta = _first(body.get("metadata"))
        if isinstance(meta, dict) and scope is not None:
            v = scope.value(meta.get("name"))
            if isinstance(v, str):
                a["namespace"] = mask(v)[:120]
    if tf_type in CDN_TYPES and scope is not None:
        # front doors of the web frontends: public hostnames and the storage of the static files
        hosts = []
        domains = scope.raw(body.get("custom_domains"))
        for d in domains if isinstance(domains, list) else []:
            if isinstance(d, dict):
                v = scope.value(d.get("domain_name"))
                if isinstance(v, str) and v:
                    hosts.append(mask(v)[:200])
        if hosts:
            a["hostnames"] = hosts
        sa = scope.value(body.get("storage_account_name"))
        if isinstance(sa, str) and sa:
            a["static_storage"] = mask(sa)[:120]
    if network_details and tf_type in (
        "azurerm_subnet",
        "azurerm_virtual_network",
        "subnet",
        "IDH/subnet",
        "virtual_network",
    ):
        for key in ("address_prefixes", "address_space"):
            raw = body.get(key)
            if raw is not None and scope is not None:
                vals = raw if isinstance(raw, list) else [raw]
                ok = [scope.value(v) for v in vals]
                a[key] = [v for v in ok if isinstance(v, str)]
    return a


def _name_of(body: dict[str, Any], tf_type: str) -> Any:
    if "name" in body:
        return body["name"]
    if tf_type in CDN_TYPES:
        return body.get("cdn_prefix_name")
    for key in ("product_id", "user_id"):  # API Management products and users
        if key in body:
            return body[key]
    meta = _first(body.get("metadata"))
    if tf_type.startswith("kubernetes_") and isinstance(meta, dict):
        return meta.get("name")
    return None


def _instances(body: dict[str, Any], scope: Scope) -> list[tuple[str, Any]] | None:
    """(key, value) of the instances of a for_each over a map or list of locals/vars, when
    the block's name depends on them; None: one block, as written."""
    raw = body.get("for_each")
    if not isinstance(raw, str) or "each." not in str(body.get("name", "")):
        return None
    inner = raw
    for fn in ("toset", "tomap"):
        inner = re.sub(rf"^\$\{{{fn}\((.+)\)\}}$", r"${\1}", inner)
    obj = scope.raw(inner)
    if isinstance(obj, dict):
        items = [(str(k), v) for k, v in obj.items()]
    elif isinstance(obj, list) and all(isinstance(x, str) for x in obj):
        items = [(x, x) for x in obj]
    else:
        return None
    return items[:MAX_FOR_EACH] if items else None


def _inner(tf_type: str, body: dict[str, Any], scopes: dict[str, Scope]) -> list[dict[str, Any]]:
    """Azure resources created inside a library module, matchable by other stacks' data
    blocks: the storage of a front door, the event hubs of an event hub configuration."""
    out: list[dict[str, Any]] = []
    if tf_type in CDN_TYPES and body.get("storage_account_name") is not None:
        names = {}
        for env, sc in scopes.items():
            v = sc.value(body.get("storage_account_name"))
            if isinstance(v, str) and v:
                names[env] = mask(v)[:200]
        if names:
            out.append({"type": "azurerm_storage_account", "names": names})
    if tf_type == "eventhub_configuration":
        sc = scopes.get("prod") or next(iter(scopes.values()))
        hubs = sc.raw(body.get("eventhubs"))
        for h in hubs if isinstance(hubs, list) else []:
            v = sc.value(h.get("name")) if isinstance(h, dict) else None
            if isinstance(v, str) and v:
                out.append(
                    {"type": "azurerm_eventhub", "names": {e: mask(v)[:200] for e in scopes}}
                )
    return out


def _stack_env_files(paths: list[str]) -> dict[str, dict[str, str]]:
    """stack -> {env: tfvars path}; several locations of the same env: the first one wins."""
    out: dict[str, dict[str, str]] = defaultdict(dict)
    for p in sorted(paths):
        parts = p.split("/")
        if len(parts) >= 3 and parts[-1] == "terraform.tfvars" and parts[-3] == "env":
            env = env_of(parts[-2])
            stack = "/".join(parts[:-3])
            if env is not None:
                out[stack].setdefault(env, p)
    return out


def wanted_paths(paths: list[str]) -> list[str]:
    """Files of an infra repository the parser needs."""
    return [
        p
        for p in paths
        if ".terraform/" not in p
        and (p.endswith(".tf") or (p.endswith("/terraform.tfvars") and "/env/" in p))
    ]


def parse_repo(files: dict[str, str], *, network_details: bool = False) -> dict[str, Any]:
    """Extract of one repository: {"stacks": {...}, "errors": {...}, "noise": n}."""
    docs: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    errors: dict[str, str] = {}
    for path, text in sorted(files.items()):
        if not path.endswith(".tf"):
            continue
        try:
            docs[dirname(path)].append((path, unquote(hcl2.loads(text))))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the repository
            errors[path] = mask(str(exc).splitlines()[0])[:200]
    tfvars: dict[str, dict[str, Any]] = {}
    for stack, by_env in _stack_env_files(list(files)).items():
        for env, path in by_env.items():
            try:
                tfvars[f"{stack}|{env}"] = unquote(hcl2.loads(files[path]))
            except Exception as exc:  # noqa: BLE001
                errors[path] = mask(str(exc).splitlines()[0])[:200]

    stacks: dict[str, Any] = {}
    noise = 0
    for stack, parsed in sorted(docs.items()):
        local_values: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        blocks: list[tuple[str, str, str, str, dict[str, Any]]] = []  # kind, type, name, file
        for path, doc in parsed:
            for loc in doc.get("locals") or []:
                local_values.update(loc)
            for var in doc.get("variable") or []:
                for name, body in var.items():
                    if isinstance(body, dict) and "default" in body:
                        defaults[name] = body["default"]
            for kind in ("resource", "data"):
                for entry in doc.get(kind) or []:
                    for tf_type, named in entry.items():
                        for name, body in named.items():
                            blocks.append((kind, tf_type, name, path, body or {}))
            for entry in doc.get("module") or []:
                for name, body in entry.items():
                    lib = module_library(str((body or {}).get("source", "")))
                    blocks.append(("module", lib, name, path, body or {}))

        env_files = {
            key.split("|", 1)[1]: values
            for key, values in tfvars.items()
            if key.split("|", 1)[0] == stack
        }
        envs = [e for e in ENVS if e in env_files] or list(ENVS)
        scopes = {}
        for env in envs:
            variables = {**defaults, **env_files.get(env, {})}
            variables.setdefault("env_short", ENV_SHORT[env])
            variables.setdefault("env", env)
            scopes[env] = Scope(variables, local_values)
        location = {
            env: mask(str(sc.vars["location"]))
            for env, sc in scopes.items()
            if isinstance(sc.vars.get("location"), str)
        }

        resources: list[dict[str, Any]] = []
        data: list[dict[str, Any]] = []
        refs: list[dict[str, str]] = []
        for kind, tf_type, name, path, body in blocks:
            addr = (
                f"module.{name}"
                if kind == "module"
                else (f"data.{tf_type}.{name}" if kind == "data" else f"{tf_type}.{name}")
            )
            raw_name = _name_of(body, tf_type)

            def names_in(scs: dict[str, Scope], raw: Any = raw_name) -> dict[str, str]:
                out = {}
                for env, sc in scs.items():
                    v = sc.value(raw) if raw is not None else None
                    if isinstance(v, str) and v:
                        out[env] = mask(v)[:200]
                return out

            if kind == "data":
                data.append({"addr": addr, "tf_type": tf_type, "names": names_in(scopes)})
                continue
            cls = classify(kind, tf_type)
            if cls is None:
                noise += 1
                continue
            present = [env for env, sc in scopes.items() if sc.present(body.get("count"))]
            if not present:
                continue  # never created in any environment
            ref_scope = scopes.get("prod") or next(iter(scopes.values()))
            instances: list[tuple[str | None, Any]] = list(_instances(body, ref_scope) or [])
            for key, value in instances or [(None, None)]:
                inst = (
                    {env: sc.with_each(key, value) for env, sc in scopes.items()}
                    if key is not None
                    else scopes
                )
                inst_addr = f'{addr}["{key}"]' if key is not None else addr
                res: dict[str, Any] = {
                    "addr": inst_addr,
                    "kind": kind,
                    "tf_type": tf_type,
                    "ltype": cls[0],
                    "vendor": cls[1],
                    "file": path,
                    "envs": present,
                    "names": names_in(inst),
                    "attrs": _attrs(tf_type, body, inst.get("prod") or ref_scope, network_details),
                }
                inner = _inner(tf_type, body, inst)
                if inner:
                    res["inner"] = inner
                if key is not None:
                    res["base"] = addr
                resources.append(res)
                for attr, target in _refs(body):
                    if target != addr:
                        refs.append({"from": inst_addr, "to": target, "attr": attr})
        if resources or data:
            stacks[stack] = {
                "envs": envs,
                "location": location,
                "resources": resources,
                "data": data,
                "refs": refs,
            }
    return {
        "parser": PARSER_VERSION,
        "stacks": stacks,
        "errors": errors,
        "noise": noise,
        "files": len(files),
    }
