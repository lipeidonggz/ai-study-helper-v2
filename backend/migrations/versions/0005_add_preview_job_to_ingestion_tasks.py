"""Attach a preprocessed preview job to a background ingestion task.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ingestion_tasks",
        sa.Column("preview_job_id", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ingestion_tasks", "preview_job_id")
