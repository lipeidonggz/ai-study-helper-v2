"""Create persistent background ingestion task table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingestion_tasks",
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("sources_json", sa.Text(), nullable=False),
        sa.Column("total_sources", sa.Integer(), nullable=False),
        sa.Column("next_source_index", sa.Integer(), nullable=False),
        sa.Column("completed_sources", sa.Integer(), nullable=False),
        sa.Column("failed_sources", sa.Integer(), nullable=False),
        sa.Column("successful_job_ids_json", sa.Text(), nullable=False),
        sa.Column("failures_json", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("task_id"),
    )


def downgrade() -> None:
    op.drop_table("ingestion_tasks")
