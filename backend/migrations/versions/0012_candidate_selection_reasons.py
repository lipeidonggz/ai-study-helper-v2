"""Add candidate selection reasons to Preview candidates.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store per-channel and per-window selection reasons for new candidates."""

    op.add_column(
        "preview_candidates",
        sa.Column(
            "selection_reasons_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    """Remove candidate selection reasons."""

    op.drop_column("preview_candidates", "selection_reasons_json")
