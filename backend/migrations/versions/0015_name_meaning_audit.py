"""Track the independent candidate name meaning audit prompt.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Store the prompt version used for candidate name meaning checks."""

    op.add_column(
        "profile_preview_runs",
        sa.Column("name_meaning_audit_prompt_version", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    """Remove the name meaning audit prompt version."""

    op.drop_column("profile_preview_runs", "name_meaning_audit_prompt_version")
