"""Create the independent evaluation database.

Revision ID: 0001
Revises:
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create revision, plan, run and attempt facts in dependency order."""

    op.create_table(
        "eval_schema_versions",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_cases",
        sa.Column("case_id", sa.String(length=32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_case_revisions",
        sa.Column("revision_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "case_id",
            sa.String(length=32),
            sa.ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("target_bindings_json", sa.Text(), nullable=False),
        sa.Column("distractor_metadata_json", sa.Text(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("execution_policy_json", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_criteria",
        sa.Column("criterion_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "case_id",
            sa.String(length=32),
            sa.ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_criterion_revisions",
        sa.Column("revision_id", sa.String(length=96), primary_key=True),
        sa.Column(
            "criterion_id",
            sa.String(length=64),
            sa.ForeignKey("eval_criteria.criterion_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("rule_type", sa.String(length=1), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column("evaluator", sa.String(length=16), nullable=False),
        sa.Column("evaluator_ref", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_case_revision_criteria",
        sa.Column(
            "case_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "criterion_revision_id",
            sa.String(length=96),
            sa.ForeignKey(
                "eval_criterion_revisions.revision_id",
                ondelete="RESTRICT",
            ),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
    )
    op.create_table(
        "eval_suites",
        sa.Column("suite_id", sa.String(length=64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_suite_revisions",
        sa.Column("revision_id", sa.String(length=96), primary_key=True),
        sa.Column(
            "suite_id",
            sa.String(length=64),
            sa.ForeignKey("eval_suites.suite_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_suite_revision_cases",
        sa.Column(
            "suite_revision_id",
            sa.String(length=96),
            sa.ForeignKey("eval_suite_revisions.revision_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "case_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
    )
    op.create_table(
        "eval_execution_plans",
        sa.Column("plan_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "suite_revision_id",
            sa.String(length=96),
            sa.ForeignKey("eval_suite_revisions.revision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("collection_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("run_type", sa.String(length=16), nullable=False),
        sa.Column("agent_version_set_json", sa.Text(), nullable=False),
        sa.Column("model_config_json", sa.Text(), nullable=False),
        sa.Column("run_policy_json", sa.Text(), nullable=False),
        sa.Column("experiment_variable_json", sa.Text(), nullable=False),
        sa.Column("preflight_result_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_plan_cases",
        sa.Column(
            "plan_id",
            sa.String(length=64),
            sa.ForeignKey("eval_execution_plans.plan_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "case_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("resolved_targets_json", sa.Text(), nullable=False),
        sa.Column("execution_policy_json", sa.Text(), nullable=False),
    )
    op.create_table(
        "eval_execution_runs",
        sa.Column("run_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(length=64),
            sa.ForeignKey("eval_execution_plans.plan_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column(
            "resume_of_run_id",
            sa.String(length=64),
            sa.ForeignKey("eval_execution_runs.run_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "eval_attempts",
        sa.Column("attempt_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("eval_execution_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "case_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "replacement_for_attempt_id",
            sa.String(length=64),
            sa.ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("trace_status", sa.String(length=16), nullable=False),
        sa.Column("execution_status", sa.String(length=32), nullable=False),
        sa.Column("termination_reason", sa.String(length=96), nullable=True),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("model_calls", sa.Integer(), nullable=False),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_token_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    """Drop evaluation facts in reverse dependency order."""

    op.drop_table("eval_attempts")
    op.drop_table("eval_execution_runs")
    op.drop_table("eval_plan_cases")
    op.drop_table("eval_execution_plans")
    op.drop_table("eval_suite_revision_cases")
    op.drop_table("eval_suite_revisions")
    op.drop_table("eval_suites")
    op.drop_table("eval_case_revision_criteria")
    op.drop_table("eval_criterion_revisions")
    op.drop_table("eval_criteria")
    op.drop_table("eval_case_revisions")
    op.drop_table("eval_cases")
    op.drop_table("eval_schema_versions")
