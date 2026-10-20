"""remove functional identifier from DQ catalog controls

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_dq_catalog_controls_catalog_code", "dq_catalog_controls", type_="unique"
    )
    op.drop_column("dq_catalog_controls", "catalog_code")


def downgrade() -> None:
    op.add_column("dq_catalog_controls", sa.Column("catalog_code", sa.String(255), nullable=True))
    op.create_unique_constraint(
        "uq_dq_catalog_controls_catalog_code", "dq_catalog_controls", ["catalog_code"]
    )