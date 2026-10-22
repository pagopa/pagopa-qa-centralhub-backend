"""EA Map: infrastructure view from the Terraform of the payments platform

Adds ``ea_infra_resources`` and ``ea_infra_links`` and the ``infra`` entity of the review
queue (docs/ea-map/DESIGN-infra.md §2).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENTITIES_OLD = ("node", "edge", "qa", "doc")
_ENTITIES_NEW = (*_ENTITIES_OLD, "infra")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _entities(values: tuple[str, ...]) -> None:
    op.drop_constraint("ck_ea_changes_entity", "ea_changes", type_="check")
    op.create_check_constraint("ck_ea_changes_entity", "ea_changes", _in("entity", values))


def upgrade() -> None:
    op.create_table(
        "ea_infra_resources",
        sa.Column("id", sa.String(400), primary_key=True),
        sa.Column("repo", sa.String(200), nullable=False),
        sa.Column("stack", sa.String(300), nullable=False),
        sa.Column("address", sa.String(300), nullable=False),
        sa.Column("tf_kind", sa.String(10), nullable=False),
        sa.Column("tf_type", sa.String(120), nullable=False),
        sa.Column("ltype", sa.String(40), nullable=False),
        sa.Column("vendor", sa.String(20), nullable=False),
        sa.Column("label", sa.String(300), nullable=False),
        sa.Column("file", sa.String(500), nullable=True),
        sa.Column("envs", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("names", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("attrs", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("parent_id", sa.String(400), nullable=True),
        sa.Column("blueprint_id", sa.String(200), nullable=True),
        sa.Column(
            "ea_node_id",
            sa.String(200),
            sa.ForeignKey("ea_nodes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("external", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(10), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('active', 'removed')", name="ck_ea_infra_resources_status"),
    )
    op.create_index("ix_ea_infra_resources_parent_id", "ea_infra_resources", ["parent_id"])
    op.create_index("ix_ea_infra_resources_ea_node_id", "ea_infra_resources", ["ea_node_id"])
    op.create_index("ix_ea_infra_resources_blueprint_id", "ea_infra_resources", ["blueprint_id"])

    op.create_table(
        "ea_infra_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "from_id",
            sa.String(400),
            sa.ForeignKey("ea_infra_resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_id",
            sa.String(400),
            sa.ForeignKey("ea_infra_resources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("origin", sa.String(20), nullable=False),
        sa.Column("envs", postgresql.ARRAY(sa.String()), nullable=False, server_default="{}"),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("from_id", "to_id", "kind", name="uq_ea_infra_links_from_to_kind"),
        sa.CheckConstraint(
            "kind IN ('ref', 'monitors', 'notifies')", name="ck_ea_infra_links_kind"
        ),
        sa.CheckConstraint("origin IN ('explicit', 'inferred')", name="ck_ea_infra_links_origin"),
    )
    op.create_index("ix_ea_infra_links_to_id", "ea_infra_links", ["to_id"])
    _entities(_ENTITIES_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM ea_changes WHERE entity = 'infra'")
    _entities(_ENTITIES_OLD)
    op.drop_table("ea_infra_links")
    op.drop_table("ea_infra_resources")
