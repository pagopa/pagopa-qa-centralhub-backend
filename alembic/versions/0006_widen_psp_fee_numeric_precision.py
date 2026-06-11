"""widen psp fee numeric precision

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "psp_fee_services",
        "importo_minimo",
        type_=sa.Numeric(16, 2),
        existing_type=sa.Numeric(12, 2),
    )
    op.alter_column(
        "psp_fee_services",
        "importo_massimo",
        type_=sa.Numeric(16, 2),
        existing_type=sa.Numeric(12, 2),
    )
    op.alter_column(
        "psp_fee_services",
        "costo_fisso",
        type_=sa.Numeric(16, 2),
        existing_type=sa.Numeric(12, 2),
    )


def downgrade() -> None:
    op.alter_column(
        "psp_fee_services",
        "importo_minimo",
        type_=sa.Numeric(12, 2),
        existing_type=sa.Numeric(16, 2),
    )
    op.alter_column(
        "psp_fee_services",
        "importo_massimo",
        type_=sa.Numeric(12, 2),
        existing_type=sa.Numeric(16, 2),
    )
    op.alter_column(
        "psp_fee_services",
        "costo_fisso",
        type_=sa.Numeric(12, 2),
        existing_type=sa.Numeric(16, 2),
    )
