"""Add evaluation judgment and invocation facts.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create append-only judgment, checker and invocation tables."""

    op.create_table(
        "eval_evaluation_revisions",
        sa.Column("revision_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "execution_run_id",
            sa.String(length=64),
            sa.ForeignKey("eval_execution_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("checker_version_set_json", sa.Text(), nullable=False),
        sa.Column("judge_version_set_json", sa.Text(), nullable=False),
        sa.Column("config_json", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_eval_evaluation_revisions_run
        ON eval_evaluation_revisions(execution_run_id, created_at)
        """
    )
    op.create_table(
        "eval_check_results",
        sa.Column("check_result_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "evaluation_revision_id",
            sa.String(length=64),
            sa.ForeignKey(
                "eval_evaluation_revisions.revision_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(length=64),
            sa.ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("checker_id", sa.String(length=96), nullable=False),
        sa.Column("checker_version", sa.String(length=32), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "evaluation_revision_id",
            "attempt_id",
            "checker_id",
            name="uq_eval_check_revision_attempt_checker",
        ),
        if_not_exists=True,
    )
    op.create_table(
        "eval_judgments",
        sa.Column("judgment_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "evaluation_revision_id",
            sa.String(length=64),
            sa.ForeignKey(
                "eval_evaluation_revisions.revision_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(length=64),
            sa.ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "criterion_revision_id",
            sa.String(length=96),
            sa.ForeignKey(
                "eval_criterion_revisions.revision_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("executor", sa.String(length=16), nullable=False),
        sa.Column("executor_ref", sa.Text(), nullable=True),
        sa.Column("verdict", sa.String(length=24), nullable=False),
        sa.Column("normalized_result", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "evaluation_revision_id",
            "attempt_id",
            "criterion_revision_id",
            name="uq_eval_judgment_revision_attempt_criterion",
        ),
        if_not_exists=True,
    )
    op.create_table(
        "eval_judge_invocations",
        sa.Column("invocation_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "judgment_id",
            sa.String(length=64),
            sa.ForeignKey("eval_judgments.judgment_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("prompt_version", sa.String(length=96), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output_hash", sa.String(length=64), nullable=True),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, default=0),
        sa.Column("output_tokens", sa.Integer(), nullable=False, default=0),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        if_not_exists=True,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_eval_judge_invocations_judgment
        ON eval_judge_invocations(judgment_id, attempt_number)
        """
    )
    for table_name in (
        "eval_check_results",
        "eval_judgments",
        "eval_judge_invocations",
    ):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table_name}_no_update
            BEFORE UPDATE ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, '{table_name} rows are append-only');
            END
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table_name}_no_delete
            BEFORE DELETE ON {table_name}
            BEGIN
                SELECT RAISE(ABORT, '{table_name} rows are append-only');
            END
            """
        )


def downgrade() -> None:
    """Drop append-only judgment facts."""

    op.drop_table("eval_judge_invocations")
    op.drop_table("eval_judgments")
    op.drop_table("eval_check_results")
    op.drop_table("eval_evaluation_revisions")
