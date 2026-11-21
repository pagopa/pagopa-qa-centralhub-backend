"""test lifecycle migration: items, scopes, tags, category tags, feature migration fields

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


def _now() -> sa.Column:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _upd() -> sa.Column:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def upgrade() -> None:
    status = postgresql.ENUM(name="tl_feature_status", schema=S, create_type=False)
    op.create_table(
        "tl_migration_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("page_id", sa.String(32)),
        sa.Column("page_version", sa.Integer()),
        sa.Column("page_title", sa.String(500)),
        sa.Column("file_path", sa.String(1024)),
        sa.Column("file_blob_sha", sa.String(64)),
        sa.Column("scope_code", sa.String(20), nullable=False),
        sa.Column("number", sa.Integer()),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("status", status, nullable=False),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "problems", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "proposal", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "overrides", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "feature_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey(f"{S}.tl_features.id", ondelete="SET NULL"),
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        _now(),
        _upd(),
        schema=S,
    )
    op.create_index(
        "uq_tl_migration_items_page", "tl_migration_items", ["page_id"], unique=True, schema=S,
        postgresql_where=sa.text("page_id IS NOT NULL"),
    )
    op.create_index(
        "uq_tl_migration_items_file", "tl_migration_items", ["file_path"], unique=True, schema=S,
        postgresql_where=sa.text("file_path IS NOT NULL"),
    )
    op.create_table(
        "tl_migration_scopes",
        sa.Column("scope_code", sa.String(20), primary_key=True),
        sa.Column("executed_at", sa.DateTime(timezone=True)),
        sa.Column("pr_number", sa.Integer()),
        sa.Column("pr_url", sa.String(1024)),
        sa.Column("pr_state", sa.String(16)),
        sa.Column("pr_error", sa.Text()),
        sa.Column(
            "confluence_errors", postgresql.JSONB(), nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        _upd(),
        schema=S,
    )
    op.create_table(
        "tl_migration_tags",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("decision", sa.String(8), nullable=False, server_default="keep"),
        schema=S,
    )
    op.create_table(
        "tl_category_tags",
        sa.Column("name", sa.String(64), primary_key=True),
        _now(),
        schema=S,
    )
    seed = "('runnable'), ('e2e'), ('ui'), ('skip')"
    op.execute(f"INSERT INTO {S}.tl_category_tags (name) VALUES {seed} ON CONFLICT DO NOTHING")  # noqa: S608
    op.create_table(
        "tl_migration_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("analyzed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("error", sa.Text()),
        schema=S,
    )
    op.execute(f"INSERT INTO {S}.tl_migration_state (id) VALUES (1) ON CONFLICT DO NOTHING")  # noqa: S608
    op.add_column("tl_features", sa.Column("migration_note", sa.Text()), schema=S)
    op.add_column("tl_features", sa.Column("migrated_from", postgresql.JSONB()), schema=S)
    op.add_column(
        "tl_features",
        sa.Column("alignment_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        schema=S,
    )


def downgrade() -> None:
    op.drop_column("tl_features", "alignment_pending", schema=S)
    op.drop_column("tl_features", "migrated_from", schema=S)
    op.drop_column("tl_features", "migration_note", schema=S)
    op.drop_table("tl_migration_state", schema=S)
    op.drop_table("tl_category_tags", schema=S)
    op.drop_table("tl_migration_tags", schema=S)
    op.drop_table("tl_migration_scopes", schema=S)
    op.drop_index("uq_tl_migration_items_file", table_name="tl_migration_items", schema=S)
    op.drop_index("uq_tl_migration_items_page", table_name="tl_migration_items", schema=S)
    op.drop_table("tl_migration_items", schema=S)
