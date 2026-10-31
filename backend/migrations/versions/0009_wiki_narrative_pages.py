"""Add wiki narrative pages and paragraph evidence.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create current narrative page, paragraph and link projections."""

    op.create_table(
        "wiki_pages",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("page_id", sa.String(length=40), primary_key=True),
        sa.Column("topic_id", sa.String(length=40), nullable=False),
        sa.Column("content_level", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("artifact_ref", sa.String(length=512), nullable=False),
        sa.Column("artifact_hash", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "build_id",
            "topic_id",
            name="uq_wiki_pages_build_topic",
        ),
    )
    op.create_table(
        "wiki_page_paragraphs",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("page_id", sa.String(length=40), primary_key=True),
        sa.Column("ordinal", sa.Integer(), primary_key=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_block_ids_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "wiki_page_links",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("from_topic_id", sa.String(length=40), primary_key=True),
        sa.Column("to_topic_id", sa.String(length=40), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source_block_ids_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    """Drop narrative page projections."""

    op.drop_table("wiki_page_links")
    op.drop_table("wiki_page_paragraphs")
    op.drop_table("wiki_pages")
