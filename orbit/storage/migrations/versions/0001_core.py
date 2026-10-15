"""Core Orbit tables (messages, reminders, usage, kv) and the pgvector extension.

Revision ID: 0001_core
Revises:
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_core"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("session", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index("ix_messages_session_id", "messages", ["session", "id"])

    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.String(length=50), nullable=False),
        sa.Column("chat_id", sa.String(length=100), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
    )
    op.create_index("ix_reminders_status_due_at", "reminders", ["status", "due_at"])

    op.create_table(
        "usage",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("task", sa.String(length=50), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_usage")),
    )
    op.create_index(op.f("ix_usage_created_at"), "usage", ["created_at"])

    op.create_table(
        "kv",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_kv")),
    )


def downgrade() -> None:
    op.drop_table("kv")
    op.drop_index(op.f("ix_usage_created_at"), table_name="usage")
    op.drop_table("usage")
    op.drop_index("ix_reminders_status_due_at", table_name="reminders")
    op.drop_table("reminders")
    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_table("messages")
    # The vector extension stays: dropping it would break any other objects that use it.
