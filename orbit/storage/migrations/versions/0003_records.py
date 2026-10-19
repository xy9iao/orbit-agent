"""Domain pack records: one table for every pack collection (data is JSONB, validated in the app).

Revision ID: 0003_records
Revises: 0002_radar
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_records"
down_revision: str | Sequence[str] | None = "0002_radar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "records",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("pack", sa.String(length=50), nullable=False),
        sa.Column("collection", sa.String(length=50), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_records")),
    )
    op.create_index(
        "ix_records_pack_collection_occurred_at", "records", ["pack", "collection", "occurred_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_records_pack_collection_occurred_at", table_name="records")
    op.drop_table("records")
