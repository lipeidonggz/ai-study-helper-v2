"""Add Profile extraction management, preview and comparison tables.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the first-stage Profile extraction persistence model."""

    op.create_table(
        "extraction_profiles",
        sa.Column("profile_id", sa.String(length=40), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("domain_context", sa.Text(), nullable=False),
        sa.Column("primary_uses_json", sa.Text(), nullable=False),
        sa.Column("representative_questions_json", sa.Text(), nullable=False),
        sa.Column("extraction_channels_json", sa.Text(), nullable=False),
        sa.Column("exclude_criteria_json", sa.Text(), nullable=False),
        sa.Column("granularity", sa.String(length=32), nullable=False),
        sa.Column("additional_guidance", sa.Text(), nullable=False),
        sa.Column("created_from_profile_id", sa.String(length=40), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_extraction_profiles_status",
        "extraction_profiles",
        ["status"],
    )
    op.create_index(
        "ix_extraction_profiles_system",
        "extraction_profiles",
        ["is_system"],
    )

    op.create_table(
        "knowledge_base_profile_selection",
        sa.Column("knowledge_base_id", sa.String(length=40), primary_key=True),
        sa.Column(
            "selected_profile_id",
            sa.String(length=40),
            sa.ForeignKey("extraction_profiles.profile_id"),
            nullable=True,
        ),
        sa.Column("selected_profile_hash", sa.String(length=64), nullable=True),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "profile_preview_runs",
        sa.Column("run_id", sa.String(length=40), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.String(length=40), nullable=False),
        sa.Column("profile_hash", sa.String(length=64), nullable=False),
        sa.Column("profile_config_snapshot_json", sa.Text(), nullable=False),
        sa.Column("document_scope_json", sa.Text(), nullable=False),
        sa.Column("document_versions_json", sa.Text(), nullable=False),
        sa.Column("document_hashes_json", sa.Text(), nullable=False),
        sa.Column("candidate_pipeline_version", sa.String(length=64), nullable=False),
        sa.Column("name_normalization_version", sa.String(length=64), nullable=False),
        sa.Column("compiler_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_hash", sa.String(length=64), nullable=False),
        sa.Column("provider_profile_json", sa.Text(), nullable=False),
        sa.Column("window_policy_version", sa.String(length=64), nullable=False),
        sa.Column("window_policy_json", sa.Text(), nullable=False),
        sa.Column("execution_policy_json", sa.Text(), nullable=False),
        sa.Column("preview_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("usage_json", sa.Text(), nullable=False),
        sa.Column("diagnostics_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_profile_preview_runs_snapshot",
        "profile_preview_runs",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_profile_preview_runs_profile",
        "profile_preview_runs",
        ["profile_id"],
    )
    op.create_index(
        "ix_profile_preview_runs_fingerprint",
        "profile_preview_runs",
        ["preview_fingerprint"],
    )
    op.create_index(
        "ix_profile_preview_runs_status",
        "profile_preview_runs",
        ["status"],
    )
    op.create_index(
        "ix_profile_preview_runs_fingerprint_status",
        "profile_preview_runs",
        ["preview_fingerprint", "status"],
    )
    op.create_index(
        "ix_profile_preview_runs_created",
        "profile_preview_runs",
        ["created_at"],
    )

    op.create_table(
        "preview_candidates",
        sa.Column("candidate_id", sa.String(length=40), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=40),
            sa.ForeignKey("profile_preview_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("doc_id", sa.String(length=40), nullable=False),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("normalized_name", sa.Text(), nullable=False),
        sa.Column("aliases_json", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("summary_variants_json", sa.Text(), nullable=False),
        sa.Column("source_block_ids_json", sa.Text(), nullable=False),
        sa.Column("evidence_blocks_json", sa.Text(), nullable=False),
        sa.Column("channel_ids_json", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_preview_candidates_run",
        "preview_candidates",
        ["run_id"],
    )
    op.create_index(
        "ix_preview_candidates_doc",
        "preview_candidates",
        ["doc_id"],
    )
    op.create_index(
        "ix_preview_candidates_normalized_name",
        "preview_candidates",
        ["normalized_name"],
    )
    op.create_index(
        "ix_preview_candidates_run_doc_name",
        "preview_candidates",
        ["run_id", "doc_id", "normalized_name"],
    )

    op.create_table(
        "document_checklists",
        sa.Column("checklist_id", sa.String(length=40), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("doc_id", sa.String(length=40), nullable=False),
        sa.Column("doc_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("evaluation_note", sa.Text(), nullable=False),
        sa.Column("items_json", sa.Text(), nullable=False),
        sa.Column("checklist_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_document_checklists_snapshot",
        "document_checklists",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_document_checklists_doc",
        "document_checklists",
        ["doc_id"],
    )

    op.create_table(
        "profile_comparisons",
        sa.Column("comparison_id", sa.String(length=40), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column(
            "run_a_id",
            sa.String(length=40),
            sa.ForeignKey("profile_preview_runs.run_id"),
            nullable=False,
        ),
        sa.Column(
            "run_b_id",
            sa.String(length=40),
            sa.ForeignKey("profile_preview_runs.run_id"),
            nullable=False,
        ),
        sa.Column("profile_a_id", sa.String(length=40), nullable=False),
        sa.Column("profile_b_id", sa.String(length=40), nullable=False),
        sa.Column("checklist_refs_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("document_results_json", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=False),
        sa.Column("comparison_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_profile_comparisons_snapshot",
        "profile_comparisons",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_profile_comparisons_runs",
        "profile_comparisons",
        ["run_a_id", "run_b_id"],
    )


def downgrade() -> None:
    """Drop only tables introduced by the first-stage Profile extraction flow."""

    op.drop_table("profile_comparisons")
    op.drop_table("document_checklists")
    op.drop_table("preview_candidates")
    op.drop_table("profile_preview_runs")
    op.drop_table("knowledge_base_profile_selection")
    op.drop_table("extraction_profiles")
