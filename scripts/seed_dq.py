from __future__ import annotations

import asyncio
import csv
import logging
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from app.core.db import async_session
from app.models.dq import (
    DqCatalogControl,
    DqCategory,
    DqControlInstance,
    DqControlStatus,
    DqDimension,
    DqDomain,
    DqRiskLevel,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO_ROOT / "csv-dq-catalog"
INSTANCES_DIR = REPO_ROOT / "csv-dq"

CATALOG_FILES: dict[str, DqCategory] = {
    "Controlli Puntuali.csv": DqCategory.PUNTUALE,
    "Controlli Intra-entità.csv": DqCategory.INTRA_ENTITA,
    "Controlli Cross-entità.csv": DqCategory.CROSS_ENTITA,
}

INSTANCE_CATEGORY_SUFFIXES: dict[str, DqCategory] = {
    "Puntuali": DqCategory.PUNTUALE,
    "Intra-entità": DqCategory.INTRA_ENTITA,
    "Cross-entità": DqCategory.CROSS_ENTITA,
}

DOMAINS = ["GEC", "GPD", "BIZ", "FDR", "Wallet"]

RISK_MAP: dict[str, DqRiskLevel] = {
    "ALTO": DqRiskLevel.ALTO,
    "MEDIO": DqRiskLevel.MEDIO,
    "BASSO": DqRiskLevel.BASSO,
}

HEADER_ALIASES = {
    "Tipo Controllo": "Tipo controllo",
    "Tabelle": "Tabella",
    "Campi": "Campo",
}


def normalize_header(header: str) -> str:
    """Strip a UTF-8 BOM and map known header spelling variants to their canonical name."""
    cleaned = header.strip().lstrip("﻿")
    return HEADER_ALIASES.get(cleaned, cleaned)


def read_catalog_csv(path: Path, category: DqCategory) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            name = (raw.get("Nome Controllo") or "").strip()
            description = (raw.get("Descrizione") or "").strip()
            dimension_name = (raw.get("Dimensione DQ") or "").strip()
            if not name or not dimension_name:
                continue
            rows.append(
                {
                    "category": category,
                    "name": name,
                    "description": description,
                    "dimension_name": dimension_name,
                }
            )
    return rows


def read_instance_csv(path: Path, domain_name: str, category: DqCategory) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [normalize_header(h) for h in (reader.fieldnames or [])]
        for line_no, raw in enumerate(reader, start=2):
            control_type = (raw.get("Tipo controllo") or "").strip()
            table_ref = (raw.get("Tabella") or "").strip()
            field_ref = (raw.get("Campo") or "").strip()
            risk = (raw.get("Rischio") or "").strip().upper()
            impact = (raw.get("Impatto") or "").strip().upper()

            if not control_type or not table_ref:
                logger.warning(
                    "Skipping %s line %d: missing 'Tipo controllo' or 'Tabella' (domain=%s, category=%s)",
                    path.name,
                    line_no,
                    domain_name,
                    category.value,
                )
                continue

            rows.append(
                {
                    "domain_name": domain_name,
                    "category": category,
                    "control_type": control_type,
                    "table_ref": table_ref,
                    "field_ref": field_ref,
                    "owner": (raw.get("Owner") or "").strip() or None,
                    "risk": RISK_MAP.get(risk, DqRiskLevel.BASSO),
                    "impact": RISK_MAP.get(impact, DqRiskLevel.BASSO),
                    "notes": (raw.get("Note") or "").strip() or None,
                }
            )
    return rows


async def seed() -> None:
    async with async_session() as session:
        dim_by_name = {d.name: d for d in (await session.execute(select(DqDimension))).scalars()}
        domain_by_name = {d.name: d for d in (await session.execute(select(DqDomain))).scalars()}

        existing_controls = (await session.execute(select(DqCatalogControl))).scalars().all()
        control_by_key = {(c.category, c.name): c for c in existing_controls}

        for filename, category in CATALOG_FILES.items():
            csv_path = CATALOG_DIR / filename
            if not csv_path.exists():
                logger.warning("Missing catalog CSV %s, skipping", csv_path)
                continue
            for row in read_catalog_csv(csv_path, category):
                key = (category, row["name"])
                if key in control_by_key:
                    continue
                dimension = dim_by_name.get(row["dimension_name"])
                if dimension is None:
                    logger.warning(
                        "Unknown dimension %r for control %r, skipping", row["dimension_name"], row["name"]
                    )
                    continue
                control = DqCatalogControl(
                    id=uuid4(),
                    category=category,
                    name=row["name"],
                    description=row["description"],
                    dimension_id=dimension.id,
                )
                session.add(control)
                control_by_key[key] = control

        await session.flush()

        existing_instances = (await session.execute(select(DqControlInstance))).scalars().all()
        instance_keys = {
            (i.domain_id, i.catalog_control_id, i.table_ref, i.field_ref) for i in existing_instances
        }

        for domain_name in DOMAINS:
            domain = domain_by_name.get(domain_name)
            if domain is None:
                logger.warning("Unknown domain %r, skipping its instance CSVs", domain_name)
                continue

            for suffix, category in INSTANCE_CATEGORY_SUFFIXES.items():
                csv_path = INSTANCES_DIR / f"{domain_name} - Controlli {suffix}.csv"
                if not csv_path.exists():
                    logger.warning("Missing instance CSV %s, skipping", csv_path)
                    continue

                for row in read_instance_csv(csv_path, domain_name, category):
                    control = control_by_key.get((category, row["control_type"]))
                    if control is None:
                        logger.warning(
                            "Unknown catalog control %r (category=%s) for domain %s, skipping",
                            row["control_type"],
                            category.value,
                            domain_name,
                        )
                        continue

                    inst_key = (domain.id, control.id, row["table_ref"], row["field_ref"])
                    if inst_key in instance_keys:
                        continue

                    instance = DqControlInstance(
                        id=uuid4(),
                        domain_id=domain.id,
                        catalog_control_id=control.id,
                        table_ref=row["table_ref"],
                        field_ref=row["field_ref"],
                        owner=row["owner"],
                        risk=row["risk"],
                        impact=row["impact"],
                        status=DqControlStatus.DA_IMPLEMENTARE,
                        notes=row["notes"],
                    )
                    session.add(instance)
                    instance_keys.add(inst_key)

        await session.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(seed())
