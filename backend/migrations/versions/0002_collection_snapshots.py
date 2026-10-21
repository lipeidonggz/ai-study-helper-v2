"""Create collection pointer and publish audit tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "active_collection_pointer",
        sa.Column("pointer_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("previous_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("pointer_id"),
    )
    op.create_table(
        "collection_publish_records",
        sa.Column("publish_id", sa.String(length=32), nullable=False),
        sa.Column("from_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("to_snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("expected_generation", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("publish_id"),
    )


def downgrade() -> None:
    op.drop_table("collection_publish_records")
    op.drop_table("active_collection_pointer")
