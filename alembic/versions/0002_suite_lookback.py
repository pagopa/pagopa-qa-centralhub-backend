"""add sync_lookback_days to e2e_suites

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-26
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "e2e_suites",
        sa.Column("sync_lookback_days", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("e2e_suites", "sync_lookback_days")
