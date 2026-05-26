"""bdd tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-26
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bdd_projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.create_table(
        "bdd_scenarios",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bdd_projects.id"), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("requirement", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("source_ref", sa.String(1000), nullable=True),
        sa.Column("gherkin", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("ai_provider", sa.String(20), nullable=False),
        sa.Column("ai_model", sa.String(100), nullable=False),
        sa.Column("generation_time_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_bdd_scenarios_project_id", "bdd_scenarios", ["project_id"])
    op.create_index("ix_bdd_scenarios_status", "bdd_scenarios", ["status"])
    op.create_index("ix_bdd_scenarios_created_at", "bdd_scenarios", ["created_at"])

    op.create_table(
        "bdd_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ai_provider", sa.String(20), nullable=False, server_default="ollama"),
        sa.Column("claude_api_key", sa.Text(), nullable=True),
        sa.Column("claude_model", sa.String(100), nullable=False, server_default="claude-sonnet-4-6"),
        sa.Column("ollama_base_url", sa.String(500), nullable=False, server_default="http://localhost:11434"),
        sa.Column("ollama_model", sa.String(100), nullable=False, server_default="llama3.2"),
        sa.Column("confluence_email", sa.String(200), nullable=True),
        sa.Column("confluence_api_token", sa.Text(), nullable=True),
        sa.Column("gherkin_language", sa.String(5), nullable=False, server_default="it"),
        sa.Column("max_scenarios", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    # Insert default settings row
    op.execute("INSERT INTO bdd_settings (id) VALUES (1) ON CONFLICT DO NOTHING")


def downgrade() -> None:
    op.drop_table("bdd_settings")
    op.drop_table("bdd_scenarios")
    op.drop_table("bdd_projects")
