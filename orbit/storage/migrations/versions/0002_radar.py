"""Internship Radar tables: companies, postings, notifications, profile, runs.

Revision ID: 0002_radar
Revises: 0001_core
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0002_radar"
down_revision: str | Sequence[str] | None = "0001_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Matches orbit.storage.models.EMBEDDING_DIM at the time of this migration (text-embedding-3-small).
EMBEDDING_DIM = 1536


def upgrade() -> None:
    # 0001_core already ran CREATE EXTENSION vector.
    op.create_table(
        "radar_companies",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("board_token", sa.String(length=200), nullable=False),
        sa.Column("hq_city", sa.String(length=100), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_verify_status", sa.String(length=50), nullable=True),
        sa.Column("last_verify_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('greenhouse', 'lever', 'ashby')", name=op.f("ck_radar_companies_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radar_companies")),
        sa.UniqueConstraint("slug", name=op.f("uq_radar_companies_slug")),
    )

    op.create_table(
        "radar_postings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("location_raw", sa.Text(), nullable=False),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=False),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'open'"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("extraction_status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("extraction_error", sa.Text(), nullable=True),
        sa.Column("extraction_model", sa.String(length=100), nullable=True),
        sa.Column("extracted_at", sa.DateTime(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("embedded_hash", sa.String(length=64), nullable=True),
        sa.Column("score", sa.Double(), nullable=True),
        sa.Column("scored_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "source IN ('greenhouse', 'lever', 'ashby')", name=op.f("ck_radar_postings_source")
        ),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_radar_postings_status")),
        sa.CheckConstraint(
            "extraction_status IN ('pending', 'ok', 'failed', 'skipped')",
            name=op.f("ck_radar_postings_extraction_status"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["radar_companies.id"],
            name=op.f("fk_radar_postings_company_id_radar_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radar_postings")),
        sa.UniqueConstraint("source", "external_id", name="uq_radar_postings_source_external_id"),
    )
    # No ANN (HNSW/IVFFlat) index on embedding: a few thousand rows, exact search is fast enough.
    op.create_index("ix_radar_postings_company_id_status", "radar_postings", ["company_id", "status"])
    op.create_index(
        "ix_radar_postings_status_extraction_status", "radar_postings", ["status", "extraction_status"]
    )
    op.create_index("ix_radar_postings_status_score", "radar_postings", ["status", "score"])

    op.create_table(
        "radar_notifications",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("posting_id", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Double(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')", name=op.f("ck_radar_notifications_status")
        ),
        sa.ForeignKeyConstraint(
            ["posting_id"],
            ["radar_postings.id"],
            name=op.f("fk_radar_notifications_posting_id_radar_postings"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radar_notifications")),
        sa.UniqueConstraint("posting_id", name=op.f("uq_radar_notifications_posting_id")),
    )

    op.create_table(
        "radar_profile",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('resume', 'goal')", name=op.f("ck_radar_profile_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radar_profile")),
        sa.UniqueConstraint("kind", name=op.f("uq_radar_profile_kind")),
    )

    op.create_table(
        "radar_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("fetched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("new", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("closed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["radar_companies.id"],
            name=op.f("fk_radar_runs_company_id_radar_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_radar_runs")),
    )
    op.create_index("ix_radar_runs_company_id_started_at", "radar_runs", ["company_id", "started_at"])


def downgrade() -> None:
    # Children first; dropping a table drops its constraints and indexes with it.
    op.drop_index("ix_radar_runs_company_id_started_at", table_name="radar_runs")
    op.drop_table("radar_runs")
    op.drop_table("radar_profile")
    op.drop_table("radar_notifications")
    op.drop_index("ix_radar_postings_status_score", table_name="radar_postings")
    op.drop_index("ix_radar_postings_status_extraction_status", table_name="radar_postings")
    op.drop_index("ix_radar_postings_company_id_status", table_name="radar_postings")
    op.drop_table("radar_postings")
    op.drop_table("radar_companies")
