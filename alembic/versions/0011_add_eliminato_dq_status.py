"""add eliminato data quality status

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15
"""
from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE dq_control_status ADD VALUE IF NOT EXISTS 'eliminato'")


def downgrade() -> None:
    op.execute("ALTER TABLE dq_control_instances ALTER COLUMN status DROP DEFAULT")
    op.execute(
        "ALTER TABLE dq_control_instances ALTER COLUMN status TYPE text "
        "USING status::text"
    )
    op.execute("UPDATE dq_control_instances SET status = 'non_attivo' WHERE status = 'eliminato'")
    op.execute("DROP TYPE dq_control_status")
    op.execute(
        "CREATE TYPE dq_control_status AS ENUM "
        "('da_implementare', 'in_sviluppo', 'attivo', 'non_attivo')"
    )
    op.execute(
        "ALTER TABLE dq_control_instances ALTER COLUMN status "
        "TYPE dq_control_status USING status::dq_control_status"
    )
    op.execute(
        "ALTER TABLE dq_control_instances ALTER COLUMN status "
        "SET DEFAULT 'da_implementare'::dq_control_status"
    )