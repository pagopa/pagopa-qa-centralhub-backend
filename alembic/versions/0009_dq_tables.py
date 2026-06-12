"""dq tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-11
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


DIMENSIONS = [
    {"name": "Validità", "sort_order": 0},
    {"name": "Completezza", "sort_order": 1},
    {"name": "Consistenza", "sort_order": 2},
    {"name": "Accuratezza", "sort_order": 3},
    {"name": "Unicità", "sort_order": 4},
    {"name": "Tempestività", "sort_order": 5},
]

DOMAINS = [
    {"name": "GEC", "sort_order": 0},
    {"name": "GPD", "sort_order": 1},
    {"name": "BIZ", "sort_order": 2},
    {"name": "FDR", "sort_order": 3},
    {"name": "Wallet", "sort_order": 4},
]


def upgrade() -> None:
    bind = op.get_bind()

    dq_category = postgresql.ENUM(
        "puntuale", "intra_entita", "cross_entita", name="dq_category", create_type=False
    )
    dq_risk_level = postgresql.ENUM("ALTO", "MEDIO", "BASSO", name="dq_risk_level", create_type=False)
    dq_control_status = postgresql.ENUM(
        "da_implementare", "in_sviluppo", "attivo", "non_attivo", name="dq_control_status", create_type=False
    )
    dq_category.create(bind, checkfirst=True)
    dq_risk_level.create(bind, checkfirst=True)
    dq_control_status.create(bind, checkfirst=True)

    dimensions_table = op.create_table(
        "dq_dimensions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
    )

    op.bulk_insert(
        dimensions_table,
        [{"id": uuid.uuid4(), **row} for row in DIMENSIONS],
    )

    op.create_table(
        "dq_catalog_controls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("category", dq_category, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column(
            "dimension_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dq_dimensions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    domains_table = op.create_table(
        "dq_domains",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("name", sa.String(50), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.bulk_insert(
        domains_table,
        [{"id": uuid.uuid4(), **row} for row in DOMAINS],
    )

    op.create_table(
        "dq_control_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column(
            "domain_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dq_domains.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "catalog_control_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dq_catalog_controls.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("table_ref", sa.String(255), nullable=False),
        sa.Column("field_ref", sa.String(255), nullable=False),
        sa.Column("owner", sa.String(255), nullable=True),
        sa.Column("risk", dq_risk_level, nullable=False),
        sa.Column("impact", dq_risk_level, nullable=False),
        sa.Column(
            "status",
            dq_control_status,
            nullable=False,
            server_default="da_implementare",
        ),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("dq_control_instances")
    op.drop_table("dq_domains")
    op.drop_table("dq_catalog_controls")
    op.drop_table("dq_dimensions")

    bind = op.get_bind()
    postgresql.ENUM(name="dq_control_status").drop(bind, checkfirst=True)
    postgresql.ENUM(name="dq_risk_level").drop(bind, checkfirst=True)
    postgresql.ENUM(name="dq_category").drop(bind, checkfirst=True)
