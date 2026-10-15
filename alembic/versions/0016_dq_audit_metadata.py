"""add creator and updater metadata to DQ controls

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    for table_name in ("dq_catalog_controls", "dq_control_instances"):
        op.add_column(table_name, sa.Column("created_by", sa.String(255), nullable=True))
        op.add_column(table_name, sa.Column("updated_by", sa.String(255), nullable=True))


def downgrade() -> None:
    for table_name in ("dq_control_instances", "dq_catalog_controls"):
        op.drop_column(table_name, "updated_by")
        op.drop_column(table_name, "created_by")