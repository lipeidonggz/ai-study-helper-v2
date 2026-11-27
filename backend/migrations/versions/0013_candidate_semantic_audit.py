"""Add optional semantic candidate audit state to Preview Runs.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store audit opt-in, lifecycle state, errors, and immutable decisions."""

    op.add_column(
        "profile_preview_runs",
        sa.Column("semantic_audit_enabled", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column(
        "profile_preview_runs",
        sa.Column(
            "semantic_audit_prompt_version",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "profile_preview_runs",
        sa.Column(
            "semantic_audit_status",
            sa.String(length=16),
            nullable=False,
            server_default="disabled",
        ),
    )
    op.add_column(
        "profile_preview_runs",
        sa.Column("semantic_audit_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "profile_preview_runs",
        sa.Column(
            "semantic_audit_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    """Remove optional semantic audit state."""

    op.drop_column("profile_preview_runs", "semantic_audit_json")
    op.drop_column("profile_preview_runs", "semantic_audit_error")
    op.drop_column("profile_preview_runs", "semantic_audit_status")
    op.drop_column("profile_preview_runs", "semantic_audit_prompt_version")
    op.drop_column("profile_preview_runs", "semantic_audit_enabled")
