"""Data contracts → Data Lake datasets for the EA Map (docs/ea-map/DESIGN-data-lake.md).

Pure functions: they turn the YAML data contracts of ``pagopa/pdnd-data-contracts``
(datacontract.com spec 0.9.3 + pagoPA extension) into dataset nodes and the edges among
them (``derives``) and towards their consumers (``feeds``). Producer → dataset edges
(``produces``) need the EA components and are proposed by the sync (PR2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog
import yaml

logger = structlog.get_logger(__name__)

CONTRACTS_REPO = "pdnd-data-contracts"
CONTRACTS_PATH = "src/data/pagopa"
CONTRACTS_URL = "https://github.com/pagopa/pdnd-data-contracts/blob/main"

QUALITY_META_DIR = "qa"  # src/data/pagopa/qa/**: DQF results, not business datasets
DEFAULT_DATABASE = "pagopa"

LAYERS = ("ingestion", "silver", "gold", "export")

# consumers known from the contracts; BI reads every gold table (curated rule)
EXT_BI = "ext-bi"
EXT_MEF = "ext-mef"
EXT_BDI = "ext-bdi"
CONSUMER_NODES: dict[str, dict[str, str]] = {
    EXT_BI: {
        "name": "Business Intelligence",
        "description": "Report e dashboard aziendali che leggono le tabelle gold del Data Lake",
    },
    EXT_MEF: {
        "name": "MEF",
        "description": "Ministero dell'Economia e delle Finanze: riceve report dal Data Lake",
    },
}

_TABLE_TOKEN = re.compile(r"\b(?:(pagopa(?:_\w+)?)\.)?((?:bronze|silver|gold)_[a-z0-9_]+)\b")
# phrases that state where a table comes from…
_LINEAGE_HINT = re.compile(
    r"deriv|provengon|con join su|calcolat|aggregat|denormalizzazion|elaborat|processat",
    re.IGNORECASE,
)
# …and phrases that only advise how to use other tables
_ADVICE = re.compile(
    r"consulta|utilizza(?:re)? la tabella|effettuare una join|relazion|si collega|foreign key"
    r"|gestit[ie] nella tabella|per analisi",
    re.IGNORECASE,
)
_CHECK_NAME = re.compile(r"^\s*name:\s*(.+?)\s*$", re.MULTILINE)


class ContractError(ValueError):
    pass


@dataclass
class Contract:
    """The parts of a data contract the EA Map uses."""

    path: str
    urn: str
    title: str
    description: str
    version: str | None
    spec: str | None
    owner: str | None
    contact: dict[str, str]
    domain: str
    servers: dict[str, dict[str, Any]]
    models: list[str]
    fields: int
    pii_fields: int
    sla: dict[str, str]
    quality: dict[str, Any]
    links: dict[str, str]
    text: str  # description + terms, for the lineage
    quality_meta: bool = False

    @property
    def node_id(self) -> str:
        try:
            return "dc:" + self.urn.removeprefix("urn:")
        except Exception:
            logger.exception("Contract.node_id failed")
            raise

    @property
    def production(self) -> dict[str, Any]:
        try:
            return self.servers.get("production") or next(iter(self.servers.values()), {})
        except Exception:
            logger.exception("Contract.production failed")
            raise

    @property
    def table(self) -> str | None:
        try:
            srv = self.production
            if srv.get("type") not in ("impala", "hive", "databricks", "postgres"):
                return None
            return normalize_table(str(srv.get("table") or ""), srv.get("database"))
        except Exception:
            logger.exception("Contract.table failed")
            raise

    @property
    def topic(self) -> str | None:
        try:
            srv = self.production
            return str(srv["topic"]) if srv.get("type") == "kafka" and srv.get("topic") else None
        except Exception:
            logger.exception("Contract.topic failed")
            raise

    @property
    def layer(self) -> str:
        try:
            return classify_layer(self)
        except Exception:
            logger.exception("Contract.layer failed")
            raise


def normalize_table(name: str, database: str | None = None) -> str | None:
    try:
        name = name.strip().lower()
        if not name:
            return None
        if "." in name:
            return name
        return f"{(database or DEFAULT_DATABASE).strip().lower()}.{name}"
    except Exception:
        logger.exception("normalize_table failed")
        raise


def _text(value: Any) -> str:
    try:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            return "\n".join(_text(v) for v in value.values())
        if isinstance(value, list):
            return "\n".join(_text(v) for v in value)
        return str(value)
    except Exception:
        logger.exception("_text failed")
        raise


def _count_fields(fields: dict[str, Any] | None) -> tuple[int, int]:
    """Leaf fields and PII leaf fields, descending into records (CDC before/after…)."""
    try:
        total = pii = 0
        for spec in (fields or {}).values():
            if not isinstance(spec, dict):
                continue
            nested = spec.get("fields")
            if isinstance(nested, dict) and nested:
                t, p = _count_fields(nested)
                total, pii = total + t, pii + p
                continue
            total += 1
            if spec.get("pii") is True:
                pii += 1
        return total, pii
    except Exception:
        logger.exception("_count_fields failed")
        raise


def _sla(levels: Any) -> dict[str, str]:
    try:
        out: dict[str, str] = {}
        if not isinstance(levels, dict):
            return out
        for key, value in levels.items():
            if not isinstance(value, dict):
                continue
            summary = value.get("percentage") or value.get("period") or value.get("threshold")
            if value.get("unlimited") is True:
                summary = "illimitata"
            summary = summary or value.get("description")
            if summary:
                out[str(key)] = str(summary).strip()[:200]
        return out
    except Exception:
        logger.exception("_sla failed")
        raise


def _quality(q: Any) -> dict[str, Any]:
    try:
        if not isinstance(q, dict):
            return {}
        spec = q.get("specification")
        names = _CHECK_NAME.findall(spec) if isinstance(spec, str) else []
        xref = q.get("xref-dataset") or []
        return {
            "type": q.get("type"),
            "dataset": normalize_table(str(q["dataset"])) if q.get("dataset") else None,
            "xref": [normalize_table(str(x)) for x in (xref if isinstance(xref, list) else [xref])],
            "checks": len(names),
            "names": [n.strip("'\"") for n in names],
        }
    except Exception:
        logger.exception("_quality failed")
        raise


def parse_contract(path: str, text: str) -> Contract:
    try:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            logger.exception("parse_contract failed")
            raise ContractError(f"{path}: YAML non valido ({exc})") from exc
        if not isinstance(doc, dict) or not doc.get("id"):
            raise ContractError(f"{path}: contratto senza id")
        info = doc.get("info") or {}
        contact = info.get("contact") or {}
        rel = path.removeprefix(CONTRACTS_PATH + "/")
        parts = rel.split("/")
        domain = parts[0] if len(parts) > 1 else "pagopa"
        quality_meta = domain == QUALITY_META_DIR
        if quality_meta and len(parts) > 2:
            domain = parts[1]

        servers: dict[str, dict[str, Any]] = {}
        for env, srv in (doc.get("servers") or {}).items():
            if isinstance(srv, dict):
                servers[str(env)] = {
                    k: srv[k]
                    for k in (
                        "type",
                        "host",
                        "port",
                        "topic",
                        "database",
                        "schema",
                        "table",
                        "location",
                        "format",
                    )
                    if srv.get(k) not in (None, "")
                }

        models = doc.get("models") or {}
        if not isinstance(models, dict):  # malformed: keep the contract, without the schema
            models = {}
        total = pii = 0
        for model in models.values():
            if isinstance(model, dict):
                t, p = _count_fields(model.get("fields"))
                total, pii = total + t, pii + p

        links = {str(k): str(v) for k, v in (doc.get("links") or {}).items() if v}
        return Contract(
            path=path,
            urn=str(doc["id"]).strip(),
            title=_text(info.get("title")) or str(doc["id"]),
            description=_text(info.get("description")),
            version=str(info["version"]) if info.get("version") is not None else None,
            spec=str(doc.get("dataContractSpecification") or "") or None,
            owner=_text(info.get("owner")) or None,
            contact={
                k: str(contact[src]).strip()
                for k, src in (
                    ("name", "name"),
                    ("email", "email"),
                    ("url", "url"),
                    ("slack", "slack-channel"),
                )
                if contact.get(src)
            },
            domain=domain,
            servers=servers,
            models=[str(m) for m in models],
            fields=total,
            pii_fields=pii,
            sla=_sla(doc.get("servicelevels") or doc.get("serviceLevels")),
            quality=_quality(doc.get("quality")),
            links=links,
            text="\n".join((_text(info.get("description")), _text(doc.get("terms")))),
            quality_meta=quality_meta,
        )
    except Exception:
        logger.exception("parse_contract failed")
        raise


def classify_layer(c: Contract) -> str:
    try:
        srv = c.production
        kind = str(srv.get("type") or "").lower()
        if kind == "kafka":
            return "ingestion"
        if kind in ("s3", "azure", "sftp", "local") and str(srv.get("format") or "").lower() in (
            "csv",
            "parquet",
            "json",
        ):
            return "export"
        table = (c.table or "").split(".")[-1]
        for layer in ("gold", "silver"):
            if table.startswith(f"{layer}_"):
                return layer
        if table.startswith("bronze_"):
            return "ingestion"
        title = c.title.lower()
        for word, layer in (("gold", "gold"), ("silver", "silver"), ("bronze", "ingestion")):
            if word in title:
                return layer
        return "silver"
    except Exception:
        logger.exception("classify_layer failed")
        raise


def table_layer(table: str) -> str:
    try:
        short = table.split(".")[-1]
        if short.startswith("gold_"):
            return "gold"
        if short.startswith("bronze_"):
            return "ingestion"
        return "silver"
    except Exception:
        logger.exception("table_layer failed")
        raise


def lineage_tables(c: Contract, known_models: dict[str, str]) -> list[str]:
    """Tables a contract says it is derived from (description and terms)."""
    try:
        found: list[str] = []
        own = c.table
        ingestion = c.layer == "ingestion"
        # terms are folded YAML lists: "- a - b" ends up on one line, so " - " splits too
        for sentence in re.split(r"(?<=[.;\n])\s+|\n-\s*|\s-\s", c.text):
            if not _LINEAGE_HINT.search(sentence) or _ADVICE.search(sentence):
                continue
            for db, name in _TABLE_TOKEN.findall(sentence):
                if ingestion and name.startswith("bronze_"):
                    continue  # the landing table of the ingestion itself, not a source
                table = normalize_table(name, db or None)
                if table and table != own and table not in found:
                    found.append(table)
            for model, table in known_models.items():
                if "_" not in model:
                    continue  # "transfer" or "wallet" are ordinary words, not table names
                if re.search(rf"\b{re.escape(model)}\b", sentence) and table not in (*found, own):
                    found.append(table)
        return found
    except Exception:
        logger.exception("lineage_tables failed")
        raise


def _consumers(c: Contract) -> list[str]:
    try:
        out: list[str] = []
        if c.layer == "ingestion":
            return out  # data entering the Data Lake is not handed to consumers as is
        hay = f"{c.urn} {c.title} {c.description}".lower()
        if ":mef:" in c.urn or "ministero dell'economia" in hay or re.search(r"\bmef\b", hay):
            out.append(EXT_MEF)
        if ":bdi" in c.urn or "banca d'italia" in hay:
            out.append(EXT_BDI)
        if c.layer == "gold":
            out.append(EXT_BI)
        return out
    except Exception:
        logger.exception("_consumers failed")
        raise


@dataclass
class DataLakeGraph:
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: list[dict[str, Any]] = field(default_factory=list)
    quality_tables: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def _node_payload(c: Contract) -> dict[str, Any]:
    try:
        return {
            "id": c.node_id,
            "name": c.title[:300],
            "description": c.description,
            "attrs": {
                "layer": c.layer,
                "domain": c.domain,
                "contracted": True,
                "urn": c.urn,
                "contract": {
                    "path": c.path,
                    "url": f"{CONTRACTS_URL}/{c.path}",
                    "version": c.version,
                    "spec": c.spec,
                    "owner": c.owner,
                    "contact": c.contact,
                    "links": c.links,
                },
                "servers": c.servers,
                "table": c.table,
                "topic": c.topic,
                "models": c.models,
                "fields": c.fields,
                "pii_fields": c.pii_fields,
                "sla": c.sla,
                "quality": {k: v for k, v in c.quality.items() if k != "names"},
                "check_names": c.quality.get("names") or [],
            },
        }
    except Exception:
        logger.exception("_node_payload failed")
        raise


def _table_node(table: str, domain: str, cited_by: str) -> dict[str, Any]:
    try:
        return {
            "id": f"dl:{table}",
            "name": table.split(".")[-1],
            "description": "Tabella del Data Lake citata nei contratti ma senza un contratto proprio",
            "attrs": {
                "layer": table_layer(table),
                "domain": domain,
                "contracted": False,
                "table": table,
                "cited_by": [cited_by],
            },
        }
    except Exception:
        logger.exception("_table_node failed")
        raise


def build_graph(files: dict[str, str]) -> DataLakeGraph:
    """Dataset nodes and ``derives``/``feeds`` edges from the contract files (path → YAML)."""
    try:
        g = DataLakeGraph()
        contracts: list[Contract] = []
        for path in sorted(files):
            if not path.endswith((".yaml", ".yml")) or not path.startswith(CONTRACTS_PATH + "/"):
                continue
            try:
                c = parse_contract(path, files[path])
            except ContractError as exc:
                logger.exception("build_graph failed")
                g.errors[path] = str(exc)
                continue
            if c.quality_meta:
                g.quality_tables.append(
                    {
                        "urn": c.urn,
                        "domain": c.domain,
                        "table": c.table,
                        "title": c.title,
                        "path": c.path,
                    }
                )
                continue
            contracts.append(c)

        seen: dict[str, Contract] = {}
        for c in contracts:
            if c.node_id in seen:
                g.errors[c.path] = f"{c.path}: id {c.urn} già usato da {seen[c.node_id].path}"
                continue
            seen[c.node_id] = c
            g.nodes[c.node_id] = _node_payload(c)
        contracts = list(seen.values())

        by_table: dict[str, str] = {}
        known_models: dict[str, str] = {}  # model name of an ingestion contract → its table name
        for c in contracts:
            if c.table:
                by_table[c.table] = c.node_id
            if c.layer == "ingestion":
                for m in c.models:
                    key = normalize_table(m.replace("-", "_"))
                    if key:
                        known_models[m] = key
                        by_table.setdefault(key, c.node_id)

        def target(table: str, c: Contract) -> str:
            try:
                node_id = by_table.get(table)
                if node_id is None:
                    node_id = f"dl:{table}"
                    if node_id not in g.nodes:
                        g.nodes[node_id] = _table_node(table, c.domain, c.node_id)
                    else:
                        cited = g.nodes[node_id]["attrs"]["cited_by"]
                        if c.node_id not in cited:
                            cited.append(c.node_id)
                    by_table[table] = node_id
                return node_id
            except Exception:
                logger.exception("build_graph.target failed")
                raise

        edges: dict[tuple[str, str, str], dict[str, Any]] = {}

        def add(src: str, dst: str, edge_type: str, origin: str, evidence: dict[str, Any]) -> None:
            try:
                if src != dst and (src, dst, edge_type) not in edges:
                    edges[(src, dst, edge_type)] = {
                        "from": src,
                        "to": dst,
                        "type": edge_type,
                        "origin": origin,
                        "evidence": evidence,
                    }
            except Exception:
                logger.exception("build_graph.add failed")
                raise

        for c in contracts:
            # the contract says where it comes from
            for table in lineage_tables(c, known_models):
                add(
                    target(table, c),
                    c.node_id,
                    "derives",
                    "contract",
                    {"contract": c.path, "rule": "testo"},
                )
            # an ingestion contract checked on a silver table feeds that table
            q_table = c.quality.get("dataset")
            if c.layer == "ingestion" and q_table and q_table != c.table:
                add(
                    c.node_id,
                    target(q_table, c),
                    "derives",
                    "contract",
                    {"contract": c.path, "rule": "quality.dataset"},
                )
            for consumer in _consumers(c):
                add(
                    c.node_id,
                    consumer,
                    "feeds",
                    "curated" if consumer == EXT_BI else "contract",
                    {"contract": c.path, "rule": "gold → BI" if consumer == EXT_BI else "contratto"},
                )

        g.edges = list(edges.values())
        return g
    except Exception:
        logger.exception("build_graph failed")
        raise


# ---- producers (PR2): who writes the topic of an ingestion contract ------------------

# Known topics → EA node, with the confidence shown in the review queue. Order matters:
# the first match wins (negative biz events before the generic biz events topic).
PRODUCER_RULES: tuple[tuple[str, str, int], ...] = (
    (r"^gpd-ingestion\.", "pagopa-gpd-ingestion-manager", 90),
    (r"^nodo-dei-pagamenti-fdr$", "pagopa-fdr-2-event-hub", 85),
    (r"^nodo-dei-pagamenti-negative-.*biz-evt", "pagopa-negative-biz-events-datastore", 60),
    (r"^nodo-dei-pagamenti-biz-evt", "pagopa-biz-events-service", 70),
    (r"^gec-ingestion-", "pagopa-afm-marketplace-be", 60),
    (r"^payment-wallet-ingestion", "pagopa-payment-wallet-cdc-service", 60),
    (r"^bdi-kpi-ingestion", "pagopa-observability-bdi", 60),
    (r"^quality-improvement-psp-kpi", "prod:qi", 50),
)
_GENERIC_TOKENS = {
    "pagopa",
    "ingestion",
    "dl",
    "evt",
    "evh",
    "event",
    "events",
    "service",
    "nodo",
    "dei",
    "pagamenti",
    "p",
    "u",
    "d",
    "itn",
    "weu",
    "core",
    "observ",
    "ns01",
    "ns03",
    "ns04",
    "sb",
    "servicebus",
    "windows",
    "net",
    "be",
    "fe",
    "function",
    "manager",
}
SIMILARITY_CONFIDENCE = 40


def _tokens(value: str) -> set[str]:
    try:
        return {t for t in re.split(r"[^a-z0-9]+", value.lower()) if t and t not in _GENERIC_TOKENS}
    except Exception:
        logger.exception("_tokens failed")
        raise


def propose_producer(topic: str | None, candidates: dict[str, str]) -> tuple[str, int, str] | None:
    """Best EA node for a topic: (node id, confidence, rule).

    ``candidates`` maps active component/product ids to their names. Explicit rules first;
    otherwise the component sharing at least two distinctive tokens with the topic.
    """
    try:
        if not topic:
            return None
        for pattern, node_id, confidence in PRODUCER_RULES:
            if re.search(pattern, topic) and node_id in candidates:
                return node_id, confidence, f"regola {pattern}"
        wanted = _tokens(topic)
        best: tuple[int, str] | None = None
        for node_id, name in candidates.items():
            shared = len(wanted & (_tokens(node_id) | _tokens(name)))
            if shared >= 2 and (best is None or shared > best[0]):
                best = (shared, node_id)
        if best is None:
            return None
        return best[1], SIMILARITY_CONFIDENCE, "somiglianza dei nomi"
    except Exception:
        logger.exception("propose_producer failed")
        raise
