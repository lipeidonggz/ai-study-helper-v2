"""Add reusable evidence translation cache.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the evidence translation cache table."""

    op.create_table(
        "evidence_translations",
        sa.Column("translation_key", sa.String(length=64), primary_key=True),
        sa.Column("source_text_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_model_id", sa.String(length=200), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_evidence_translations_source_text_hash",
        "evidence_translations",
        ["source_text_hash"],
    )


def downgrade() -> None:
    """Drop the evidence translation cache table."""

    op.drop_table("evidence_translations")
