"""Data Quality overlay on the Data Lake datasets (docs/ea-map/DESIGN-data-lake.md §6).

Two sources of checks for a dataset:

* the SodaCL checks declared in its data contract (``attrs.check_names``);
* the controls censused in the Hub's DQ Framework (``dq_control_instances``), whose
  ``table_ref`` names Data Lake tables.

The DQF controls of an ingestion run on its **bronze** landing table
(``pagopa.bronze_gpd_payment_position``) while the ingestion contract names the silver
table it feeds (``quality.dataset: pagopa.silver_gpd_payment_position``): the match goes
through the table base name without the layer prefix.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.dq import DqCatalogControl, DqControlInstance
from app.schemas.dq import DIMENSION_CODES

logger = structlog.get_logger(__name__)

DIMENSION_BY_CODE = {code: name for name, code in DIMENSION_CODES.items()}
OTHER_DIMENSION = "Altro"
_LAYER_PREFIX = re.compile(r"^(bronze|silver|gold)_")


def _short(table: str) -> str:
    try:
        return table.strip().lower().split(".")[-1]
    except Exception:
        logger.exception("_short failed")
        raise


def base_name(table: str) -> str:
    """``pagopa.bronze_gpd_transfer`` → ``gpd_transfer``."""
    try:
        return _LAYER_PREFIX.sub("", _short(table))
    except Exception:
        logger.exception("base_name failed")
        raise


def table_refs(value: str) -> list[str]:
    """A DQ ``table_ref`` can list several tables (cross-entity controls)."""
    try:
        return [t for t in re.split(r"[\s,;]+", value or "") if t]
    except Exception:
        logger.exception("table_refs failed")
        raise


def check_dimension(name: str) -> str:
    try:
        parts = name.split("__")
        if len(parts) >= 2 and parts[1] in DIMENSION_BY_CODE:
            return DIMENSION_BY_CODE[parts[1]]
        return OTHER_DIMENSION
    except Exception:
        logger.exception("check_dimension failed")
        raise


@dataclass
class DatasetKeys:
    """How a dataset can be named by a DQ control."""

    exact: set[str]  # full table names (silver / gold)
    bases: set[str]  # base names, for ingestions (bronze landing ↔ silver target)


def dataset_keys(attrs: dict[str, Any]) -> DatasetKeys:
    try:
        exact: set[str] = set()
        bases: set[str] = set()
        table = attrs.get("table")
        if table:
            exact.add(_short(table))
        if attrs.get("layer") == "ingestion":
            quality = attrs.get("quality") or {}
            if quality.get("dataset"):
                bases.add(base_name(quality["dataset"]))
            for model in attrs.get("models") or []:
                bases.add(base_name(str(model).replace("-", "_")))
        return DatasetKeys(exact, bases)
    except Exception:
        logger.exception("dataset_keys failed")
        raise


def match_controls(
    datasets: dict[str, dict[str, Any]], controls: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Dataset id → the DQ controls whose ``table_ref`` names that dataset.

    A bronze table goes to the ingestion feeding it; silver and gold tables go to the
    dataset with that exact table, else to the ingestion with the same base name.
    """
    try:
        exact: dict[str, str] = {}
        by_base: dict[str, str] = {}
        for dataset_id, attrs in datasets.items():
            keys = dataset_keys(attrs)
            for t in keys.exact:
                exact.setdefault(t, dataset_id)
            for b in keys.bases:
                by_base.setdefault(b, dataset_id)
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for control in controls:
            seen: set[str] = set()
            for ref in table_refs(control.get("table_ref") or ""):
                short = _short(ref)
                target = None if short.startswith("bronze_") else exact.get(short)
                target = target or by_base.get(base_name(ref))
                if target and target not in seen:
                    seen.add(target)
                    out[target].append(control)
        return dict(out)
    except Exception:
        logger.exception("match_controls failed")
        raise


def coverage(attrs: dict[str, Any], controls: list[dict[str, Any]]) -> dict[str, Any]:
    """Summary for one dataset: contract checks, DQF controls and their alignment."""
    try:
        contract_names = [str(n) for n in attrs.get("check_names") or []]
        hub_names = {str(c["check_name"]) for c in controls if c.get("check_name")}
        contract_set = set(contract_names)
        return {
            "contract_checks": len(contract_names),
            "contract_dimensions": dict(Counter(check_dimension(n) for n in contract_names)),
            "controls": len(controls),
            "active": sum(1 for c in controls if c.get("status") == "attivo"),
            "statuses": dict(Counter(str(c.get("status")) for c in controls)),
            "dimensions": dict(Counter(str(c.get("dimension") or OTHER_DIMENSION) for c in controls)),
            "domains": sorted({str(c["domain"]) for c in controls if c.get("domain")}),
            "aligned": len(contract_set & hub_names),
            "only_contract": sorted(contract_set - hub_names),
            "only_hub": sorted(hub_names - contract_set),
        }
    except Exception:
        logger.exception("coverage failed")
        raise


async def load_controls(db: AsyncSession) -> list[dict[str, Any]]:
    try:
        rows = await db.scalars(
            select(DqControlInstance).options(
                selectinload(DqControlInstance.domain),
                selectinload(DqControlInstance.catalog_control).selectinload(
                    DqCatalogControl.dimension
                ),
            )
        )
        rows = list(rows)
        if not rows:
            logger.info("load_controls query succeeded with 0 results")
        out: list[dict[str, Any]] = []
        for i in rows:
            status = getattr(i.status, "value", i.status)
            if status == "eliminato":
                continue
            out.append(
                {
                    "id": str(i.id),
                    "table_ref": i.table_ref,
                    "field_ref": i.field_ref,
                    "check_name": i.check_name,
                    "status": status,
                    "dimension": i.catalog_control.dimension.name if i.catalog_control else None,
                    "control": i.catalog_control.name if i.catalog_control else None,
                    "domain": i.domain.name if i.domain else None,
                    "soda_url": i.soda_url,
                }
            )
        return out
    except Exception:
        logger.exception("load_controls failed")
        raise


async def dataset_coverage(
    db: AsyncSession, datasets: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Coverage summary and matched controls for every dataset."""
    try:
        matched = match_controls(datasets, await load_controls(db))
        summary = {d: coverage(attrs, matched.get(d, [])) for d, attrs in datasets.items()}
        return summary, matched
    except Exception:
        logger.exception("dataset_coverage failed")
        raise
