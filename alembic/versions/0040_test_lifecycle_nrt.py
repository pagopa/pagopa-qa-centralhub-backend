"""test lifecycle nrt: execution tags, scenario aliases, feature running_at

Revision ID: 0040
Revises: 0039
Create Date: 2026-11-21
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None

S = "qachub"
TAGS_EXPR = (
    "coalesce(jsonb_path_query_array(allure_report, "
    "'$.labels[*] ? (@.name == \"tag\").value'), '[]'::jsonb)"
)


def upgrade() -> None:
    # Colonna generata: riscrive la tabella una volta (backfill implicito), poi si mantiene da sola.
    op.add_column(
        "test_executions",
        sa.Column("tags", postgresql.JSONB(), sa.Computed(TAGS_EXPR, persisted=True)),
        schema=S,
    )
    op.create_index(
        "ix_qachub_test_executions_tags", "test_executions", ["tags"],
        schema=S, postgresql_using="gin",
    )
    op.create_table(
        "tl_scenario_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "scenario_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_scenarios.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("alias", sa.String(128), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        schema=S,
    )
    op.create_index(
        "ix_tl_scenario_aliases_scenario", "tl_scenario_aliases", ["scenario_id"], schema=S
    )
    op.add_column("tl_features", sa.Column("running_at", sa.DateTime(timezone=True)), schema=S)


def downgrade() -> None:
    op.drop_column("tl_features", "running_at", schema=S)
    op.drop_index("ix_tl_scenario_aliases_scenario", table_name="tl_scenario_aliases", schema=S)
    op.drop_table("tl_scenario_aliases", schema=S)
    op.drop_index("ix_qachub_test_executions_tags", table_name="test_executions", schema=S)
    op.drop_column("test_executions", "tags", schema=S)
