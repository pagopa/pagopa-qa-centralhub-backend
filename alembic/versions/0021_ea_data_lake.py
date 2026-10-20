"""EA Map: Data Lake datasets from the data contracts

Adds the ``dataset`` node kind, the ``produces``/``derives``/``feeds`` edge types and the
``contract``/``contract_rule`` edge origins (docs/ea-map/DESIGN-data-lake.md §3).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS_OLD = (
    "area",
    "product",
    "component",
    "external",
    "datastore",
    "segment",
    "service",
    "function",
    "process",
    "infra",
)
_TYPES_OLD = (
    "api",
    "soap",
    "event",
    "data",
    "ui",
    "serves",
    "realizes",
    "groups",
    "uses",
    "runs_on",
)
_ORIGINS_OLD = ("helm", "infra_rule", "curated", "manual")

_KINDS_NEW = (*_KINDS_OLD, "dataset")
_TYPES_NEW = (*_TYPES_OLD, "produces", "derives", "feeds")
_ORIGINS_NEW = (*_ORIGINS_OLD, "contract", "contract_rule")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _replace(table: str, name: str, condition: str) -> None:
    op.drop_constraint(name, table, type_="check")
    op.create_check_constraint(name, table, condition)


def upgrade() -> None:
    _replace("ea_nodes", "ck_ea_nodes_kind", _in("kind", _KINDS_NEW))
    _replace("ea_edges", "ck_ea_edges_type", _in("type", _TYPES_NEW))
    _replace("ea_edges", "ck_ea_edges_origin", _in("origin", _ORIGINS_NEW))


def downgrade() -> None:
    op.execute(
        "DELETE FROM ea_edges WHERE type IN ('produces', 'derives', 'feeds') "
        "OR origin IN ('contract', 'contract_rule')"
    )
    op.execute("DELETE FROM ea_nodes WHERE kind = 'dataset'")
    _replace("ea_nodes", "ck_ea_nodes_kind", _in("kind", _KINDS_OLD))
    _replace("ea_edges", "ck_ea_edges_type", _in("type", _TYPES_OLD))
    _replace("ea_edges", "ck_ea_edges_origin", _in("origin", _ORIGINS_OLD))
