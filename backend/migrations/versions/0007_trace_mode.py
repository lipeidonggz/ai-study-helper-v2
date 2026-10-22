"""Add trace mode to runtime settings.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a nullable mode so existing rows inherit the environment default."""

    op.add_column(
        "runtime_settings",
        sa.Column("trace_mode", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    """Remove trace mode from runtime settings."""

    op.drop_column("runtime_settings", "trace_mode")
