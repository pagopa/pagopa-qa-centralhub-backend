"""psp fee tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-10
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "psp_fee_services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("psp_id", sa.String(50), nullable=False),
        sa.Column("psp_rag_soc", sa.String(255), nullable=False),
        sa.Column("codice_abi", sa.String(20), nullable=False),
        sa.Column("nome_servizio", sa.String(255), nullable=False),
        sa.Column("descrizione_canale_mod_pag", sa.String(255), nullable=False),
        sa.Column("inf_desc_serv", sa.String(255), nullable=False),
        sa.Column("inf_url_canale", sa.String(2000), nullable=True),
        sa.Column("url_informazioni_psp", sa.String(2000), nullable=True),
        sa.Column("tipo_vers_cod", sa.String(10), nullable=False),
        sa.Column("canale_mod_pag", sa.String(50), nullable=False),
        sa.Column("canale_mod_pag_code", sa.Integer, nullable=False),
        sa.Column("importo_minimo", sa.Numeric(12, 2), nullable=True),
        sa.Column("importo_massimo", sa.Numeric(12, 2), nullable=True),
        sa.Column("costo_fisso", sa.Numeric(12, 2), nullable=True),
        sa.Column("on_us", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("carte", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("conto", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("altri_wisp", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("altri_io", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("conto_app", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("carte_app", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("is_duplicated", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "psp_fee_sync_status",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("last_run", sa.String(20), nullable=False),
        sa.Column("notebook_version", sa.String(20), nullable=False),
        sa.Column("item_count", sa.Integer, nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("psp_fee_sync_status")
    op.drop_table("psp_fee_services")
