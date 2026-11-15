"""flaky registry, scores and recompute status

Revision ID: 0031
Revises: 0030
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

# One registry row per scenario (test_object + test_type + scenario_name): the legacy
# per-version rows keep the flag of their most recent insertion. The legacy table is left as is.
COPY_LEGACY_FLAGS_SQL = """
INSERT INTO qachub.flaky_registry
    (id, test_object, test_type, scenario_name, is_flaky, flagged_at, updated_at, inserted_at)
SELECT gen_random_uuid(), latest.test_object, latest.test_type, latest.scenario_name,
       latest.is_flaky,
       CASE WHEN latest.is_flaky THEN latest.inserted_at END,
       latest.inserted_at, latest.inserted_at
FROM (
    SELECT DISTINCT ON (ts.test_object, ts.test_type, fs.scenario_name)
           ts.test_object, ts.test_type, fs.scenario_name, fs.is_flaky, fs.inserted_at
    FROM qachub.flaky_scenarios fs
    JOIN qachub.test_suites ts ON ts.id = fs.suite_id
    ORDER BY ts.test_object, ts.test_type, fs.scenario_name, fs.inserted_at DESC
) AS latest
ON CONFLICT (test_object, test_type, scenario_name) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "flaky_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("test_object", sa.String(), nullable=False),
        sa.Column("test_type", sa.String(), nullable=False),
        sa.Column("scenario_name", sa.String(), nullable=False),
        sa.Column("is_flaky", sa.Boolean(), nullable=False),
        sa.Column("flagged_by", sa.String(), nullable=True),
        sa.Column("flagged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "inserted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "test_object", "test_type", "scenario_name", name="uq_qachub_flaky_registry_key"
        ),
        schema="qachub",
    )
    op.create_table(
        "flaky_scores",
        sa.Column("test_object", sa.String(), nullable=False),
        sa.Column("test_type", sa.String(), nullable=False),
        sa.Column("scenario_name", sa.String(), nullable=False),
        sa.Column(
            "env",
            postgresql.ENUM(
                "DEV", "UAT", "PROD", name="env_enum", schema="qachub", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("level", sa.SmallInteger(), nullable=True),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("flip_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("retry_pass_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("runs_analyzed", sa.Integer(), nullable=False),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recent_outcomes", postgresql.JSONB(), nullable=False),
        sa.Column(
            "last_suite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("qachub.test_suites.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("test_object", "test_type", "scenario_name", "env"),
        schema="qachub",
    )
    op.create_index(
        "ix_qachub_flaky_scores_env_level", "flaky_scores", ["env", "level"], schema="qachub"
    )
    op.create_table(
        "flaky_recompute_status",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_trigger", sa.String(20), nullable=True),
        sa.Column("last_requested_by", sa.String(), nullable=True),
        sa.Column("scored_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("id = 1", name="ck_flaky_recompute_status_singleton"),
        schema="qachub",
    )
    op.execute(COPY_LEGACY_FLAGS_SQL)


def downgrade() -> None:
    op.drop_table("flaky_recompute_status", schema="qachub")
    op.drop_index("ix_qachub_flaky_scores_env_level", table_name="flaky_scores", schema="qachub")
    op.drop_table("flaky_scores", schema="qachub")
    op.drop_table("flaky_registry", schema="qachub")
