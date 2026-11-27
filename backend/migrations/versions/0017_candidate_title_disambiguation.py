"""Persist candidate title disambiguation decisions.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store provisional titles and auditable disambiguation outcomes."""

    op.add_column(
        "preview_candidates",
        sa.Column("provisional_name", sa.Text(), nullable=True),
    )
    op.add_column(
        "preview_candidates",
        sa.Column("title_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "preview_candidates",
        sa.Column("title_standalone_clear", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "preview_candidates",
        sa.Column(
            "missing_anchor_types_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "preview_candidates",
        sa.Column(
            "name_changed",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Remove candidate title disambiguation decisions."""

    op.drop_column("preview_candidates", "name_changed")
    op.drop_column("preview_candidates", "missing_anchor_types_json")
    op.drop_column("preview_candidates", "title_standalone_clear")
    op.drop_column("preview_candidates", "title_kind")
    op.drop_column("preview_candidates", "provisional_name")
