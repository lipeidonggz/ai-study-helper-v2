"""Create non-secret provider settings state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_settings",
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("last_test_status", sa.String(length=16), nullable=True),
        sa.Column("last_test_at", sa.String(length=40), nullable=True),
        sa.Column("last_test_message", sa.Text(), nullable=True),
        sa.Column("last_test_request_id", sa.String(length=128), nullable=True),
        sa.Column("last_test_input_tokens", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("provider"),
    )


def downgrade() -> None:
    op.drop_table("provider_settings")
