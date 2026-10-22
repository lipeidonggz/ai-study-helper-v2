"""Add user-adjustable runtime settings.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the single-row runtime settings table."""

    op.create_table(
        "runtime_settings",
        sa.Column("settings_id", sa.Integer(), primary_key=True),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("max_context_tokens", sa.Integer(), nullable=False),
        sa.Column("max_request_tokens", sa.Integer(), nullable=False),
        sa.Column("max_tool_calls", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Drop runtime settings."""

    op.drop_table("runtime_settings")
