"""add standard identifiers and SODA links to DQ controls

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dq_catalog_controls", sa.Column("catalog_code", sa.String(255), nullable=True))
    op.create_unique_constraint(
        "uq_dq_catalog_controls_catalog_code", "dq_catalog_controls", ["catalog_code"]
    )
    op.add_column("dq_control_instances", sa.Column("check_name", sa.String(255), nullable=True))
    op.add_column("dq_control_instances", sa.Column("soda_url", sa.Text(), nullable=True))
    op.create_unique_constraint(
        "uq_dq_control_instance_check_name",
        "dq_control_instances",
        ["domain_id", "table_ref", "check_name"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_dq_control_instance_check_name", "dq_control_instances", type_="unique"
    )
    op.drop_column("dq_control_instances", "soda_url")
    op.drop_column("dq_control_instances", "check_name")
    op.drop_constraint(
        "uq_dq_catalog_controls_catalog_code", "dq_catalog_controls", type_="unique"
    )
    op.drop_column("dq_catalog_controls", "catalog_code")