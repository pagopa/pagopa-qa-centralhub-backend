"""gpd position snapshot tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-11
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gpd_position_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("report_date", sa.Date, nullable=False, unique=True),
        sa.Column("run_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("total", sa.BigInteger, nullable=False),
        sa.Column("gpd", sa.BigInteger, nullable=False),
        sa.Column("gpd_payable", sa.BigInteger, nullable=False),
        sa.Column("gpd4aca", sa.BigInteger, nullable=False),
        sa.Column("gpd4aca_payable", sa.BigInteger, nullable=False),
        sa.Column("wisp", sa.BigInteger, nullable=False),
        sa.Column("pa_create_position", sa.BigInteger, nullable=False),
        sa.Column("pa_create_position_payable", sa.BigInteger, nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_table(
        "gpd_position_sync_status",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("item_count", sa.Integer, nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("gpd_position_sync_status")
    op.drop_table("gpd_position_snapshots")
