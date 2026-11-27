"""Track the independent candidate name scope audit prompt.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store the prompt version used for knowledge-base name scope checks."""

    op.add_column(
        "profile_preview_runs",
        sa.Column("name_scope_audit_prompt_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Remove the name scope audit prompt version."""

    op.drop_column("profile_preview_runs", "name_scope_audit_prompt_version")
