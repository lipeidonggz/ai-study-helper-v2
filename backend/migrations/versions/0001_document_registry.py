"""Create the initial document registry tables.

Revision ID: 0001
Revises:
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_artifacts",
        sa.Column("artifact_ref", sa.String(length=512), nullable=False),
        sa.Column("source_kind", sa.String(length=8), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("raw_byte_length", sa.Integer(), nullable=False),
        sa.Column("fetched_at", sa.String(length=40), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("acquisition_mode", sa.String(length=16), nullable=False),
        sa.Column("charset", sa.String(length=64), nullable=False),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("artifact_ref"),
    )
    op.create_index(
        "ix_source_artifacts_content_hash",
        "source_artifacts",
        ["content_hash"],
    )
    op.create_index(
        "ix_source_artifacts_source_key",
        "source_artifacts",
        ["source_key"],
    )

    op.create_table(
        "documents",
        sa.Column("doc_id", sa.String(length=40), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(length=8), nullable=False),
        sa.Column("current_doc_version", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("aliases_json", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("doc_id"),
        sa.UniqueConstraint("source_key"),
    )

    op.create_table(
        "document_versions",
        sa.Column("doc_id", sa.String(length=40), nullable=False),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("extraction_hash", sa.String(length=64), nullable=False),
        sa.Column("source_artifact_ref", sa.String(length=512), nullable=False),
        sa.Column("extracted_document_ref", sa.String(length=512), nullable=False),
        sa.Column("section_tree_ref", sa.String(length=512), nullable=True),
        sa.Column("block_set_ref", sa.String(length=512), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_label", sa.Text(), nullable=False),
        sa.Column("aliases_json", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("extractor_name", sa.String(length=128), nullable=False),
        sa.Column("extractor_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["doc_id"],
            ["documents.doc_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_artifact_ref"],
            ["source_artifacts.artifact_ref"],
        ),
        sa.PrimaryKeyConstraint("doc_id", "doc_version"),
    )
    op.create_index(
        "ix_document_versions_doc_hash",
        "document_versions",
        ["doc_id", "extraction_hash"],
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=8), nullable=False),
        sa.Column("source_value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("doc_id", sa.String(length=40), nullable=True),
        sa.Column("doc_version", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["doc_id"], ["documents.doc_id"]),
        sa.PrimaryKeyConstraint("job_id"),
    )


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_index("ix_document_versions_doc_hash", table_name="document_versions")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_index("ix_source_artifacts_source_key", table_name="source_artifacts")
    op.drop_index("ix_source_artifacts_content_hash", table_name="source_artifacts")
    op.drop_table("source_artifacts")
