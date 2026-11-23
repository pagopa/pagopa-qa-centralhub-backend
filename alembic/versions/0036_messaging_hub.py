"""QA Messaging Hub: events, deliveries, inbox and dedupe tables

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-22
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "msg_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("type", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("product", sa.String(100), nullable=True),
        sa.Column("environment", sa.String(10), nullable=True),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_msg_events_type_received_at", "msg_events", ["type", "received_at"])

    op.create_table(
        "msg_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("msg_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("consumer", sa.String(50), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("params_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("event_id", "consumer", "params_hash", name="uq_msg_deliveries_key"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'DONE', 'DEAD')", name="ck_msg_deliveries_status"
        ),
    )
    op.create_index(
        "ix_msg_deliveries_status_next_attempt", "msg_deliveries", ["status", "next_attempt_at"]
    )

    op.create_table(
        "msg_inbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("msg_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "delivery_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("msg_deliveries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sa.String(500), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("type", sa.String(255), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("event_id", "user_id", name="uq_msg_inbox_event_user"),
    )
    op.create_index("ix_msg_inbox_user_created_at", "msg_inbox", ["user_id", "created_at"])

    op.create_table(
        "msg_dedupe",
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("members", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("last_event_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("msg_dedupe")
    op.drop_index("ix_msg_inbox_user_created_at", table_name="msg_inbox")
    op.drop_table("msg_inbox")
    op.drop_index("ix_msg_deliveries_status_next_attempt", table_name="msg_deliveries")
    op.drop_table("msg_deliveries")
    op.drop_index("ix_msg_events_type_received_at", table_name="msg_events")
    op.drop_table("msg_events")
