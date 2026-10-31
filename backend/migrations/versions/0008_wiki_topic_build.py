"""Add snapshot-bound Wiki topic builds and search projections.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create Wiki build metadata, evidence, vectors and active pointer tables."""

    op.create_table(
        "wiki_builds",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("base_build_id", sa.String(length=40)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("pipeline_versions_json", sa.Text(), nullable=False),
        sa.Column("counts_json", sa.Text(), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_wiki_builds_snapshot_id", "wiki_builds", ["snapshot_id"])
    op.create_table(
        "wiki_build_document_maps",
        sa.Column(
            "build_id",
            sa.String(length=40),
            sa.ForeignKey("wiki_builds.build_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("doc_id", sa.String(length=40), primary_key=True),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("topic_map_id", sa.String(length=40), nullable=False),
    )
    op.create_table(
        "wiki_topics",
        sa.Column(
            "build_id",
            sa.String(length=40),
            sa.ForeignKey("wiki_builds.build_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("topic_id", sa.String(length=40), primary_key=True),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("semantic_tags_json", sa.Text(), nullable=False),
        sa.Column("content_level", sa.String(length=16), nullable=False),
    )
    op.create_table(
        "wiki_topic_aliases",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("normalized_alias", sa.Text(), primary_key=True),
        sa.Column("topic_id", sa.String(length=40), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
    )
    op.create_table(
        "wiki_topic_evidence",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("topic_id", sa.String(length=40), primary_key=True),
        sa.Column("block_id", sa.String(length=255), primary_key=True),
        sa.Column("doc_id", sa.String(length=40), nullable=False),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("relation", sa.String(length=16), nullable=False),
    )
    op.create_table(
        "wiki_coverage_slots",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("slot_id", sa.String(length=40), primary_key=True),
        sa.Column("topic_id", sa.String(length=40), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
    )
    op.create_table(
        "wiki_slot_evidence",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("slot_id", sa.String(length=40), primary_key=True),
        sa.Column("block_id", sa.String(length=255), primary_key=True),
    )
    op.create_table(
        "wiki_topic_vectors",
        sa.Column("build_id", sa.String(length=40), primary_key=True),
        sa.Column("topic_id", sa.String(length=40), primary_key=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("dimension", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
        sa.Column("norm", sa.Float(), nullable=False),
    )
    op.create_table(
        "active_wiki_pointer",
        sa.Column("pointer_id", sa.Integer(), primary_key=True),
        sa.Column("build_id", sa.String(length=40), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("previous_build_id", sa.String(length=40)),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        """
        CREATE VIRTUAL TABLE wiki_topic_fts USING fts5(
          build_id UNINDEXED,
          topic_id UNINDEXED,
          title,
          aliases,
          summary,
          body,
          tokenize='unicode61'
        )
        """
    )


def downgrade() -> None:
    """Drop Wiki topic projections and build metadata."""

    op.execute("DROP TABLE IF EXISTS wiki_topic_fts")
    op.drop_table("active_wiki_pointer")
    op.drop_table("wiki_topic_vectors")
    op.drop_table("wiki_slot_evidence")
    op.drop_table("wiki_coverage_slots")
    op.drop_table("wiki_topic_evidence")
    op.drop_table("wiki_topic_aliases")
    op.drop_table("wiki_topics")
    op.drop_table("wiki_build_document_maps")
    op.drop_index("ix_wiki_builds_snapshot_id", table_name="wiki_builds")
    op.drop_table("wiki_builds")
