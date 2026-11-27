"""Add document-level candidate name anchoring state.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store anchor versions, per-document statuses, and candidate provenance."""

    op.add_column(
        "profile_preview_runs",
        sa.Column("document_anchor_prompt_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "profile_preview_runs",
        sa.Column("document_anchor_policy_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "profile_preview_runs",
        sa.Column(
            "document_anchor_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "preview_candidates",
        sa.Column(
            "name_anchor_source_block_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "preview_candidates",
        sa.Column("name_anchor_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Remove document-level candidate name anchoring state."""

    op.drop_column("preview_candidates", "name_anchor_reason")
    op.drop_column("preview_candidates", "name_anchor_source_block_ids_json")
    op.drop_column("profile_preview_runs", "document_anchor_json")
    op.drop_column("profile_preview_runs", "document_anchor_policy_version")
    op.drop_column("profile_preview_runs", "document_anchor_prompt_version")
