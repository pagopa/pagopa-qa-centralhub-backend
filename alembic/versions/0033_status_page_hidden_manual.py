"""status page hidden flags and manual version source

Revision ID: 0033
Revises: 0032
Create Date: 2026-11-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None

_OLD_STATUSES = ("OK", "ERROR", "EMPTY_BODY", "NO_VERSION", "NO_RELEASE")
_NEW_STATUSES = (*_OLD_STATUSES, "MANUAL")


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({quoted})"


def upgrade() -> None:
    op.add_column(
        "sp_products",
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "sp_applications",
        sa.Column("hidden", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "sp_applications",
        sa.Column("version_source", sa.String(10), nullable=False, server_default="AUTO"),
    )
    op.create_check_constraint(
        "ck_sp_applications_version_source",
        "sp_applications",
        _in("version_source", ("AUTO", "MANUAL")),
    )
    op.drop_constraint("ck_sp_version_state_read_status", "sp_version_state", type_="check")
    op.create_check_constraint(
        "ck_sp_version_state_read_status",
        "sp_version_state",
        _in("read_status", _NEW_STATUSES),
    )


def downgrade() -> None:
    op.execute("UPDATE sp_version_state SET read_status='NO_VERSION' WHERE read_status='MANUAL'")
    op.drop_constraint("ck_sp_version_state_read_status", "sp_version_state", type_="check")
    op.create_check_constraint(
        "ck_sp_version_state_read_status",
        "sp_version_state",
        _in("read_status", _OLD_STATUSES),
    )
    op.drop_constraint("ck_sp_applications_version_source", "sp_applications", type_="check")
    op.drop_column("sp_applications", "version_source")
    op.drop_column("sp_applications", "hidden")
    op.drop_column("sp_products", "hidden")
