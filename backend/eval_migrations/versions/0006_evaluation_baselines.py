"""Add immutable evaluation baselines and current pointers.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create baseline history and one current pointer per suite/run type."""

    op.create_table(
        "eval_baselines",
        sa.Column("baseline_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "suite_id",
            sa.String(length=64),
            sa.ForeignKey("eval_suites.suite_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("run_type", sa.String(length=16), nullable=False),
        sa.Column(
            "suite_revision_id",
            sa.String(length=96),
            sa.ForeignKey("eval_suite_revisions.revision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("collection_snapshot_id", sa.String(length=64), nullable=False),
        sa.Column(
            "plan_id",
            sa.String(length=64),
            sa.ForeignKey("eval_execution_plans.plan_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "execution_run_id",
            sa.String(length=64),
            sa.ForeignKey("eval_execution_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "evaluation_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_evaluation_revisions.revision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("case_revision_ids_json", sa.Text(), nullable=False),
        sa.Column("run_policy_json", sa.Text(), nullable=False),
        sa.Column("aggregate_rule_version", sa.String(length=64), nullable=False),
        sa.Column("result_matrix_json", sa.Text(), nullable=False),
        sa.Column("run_started_at", sa.DateTime(timezone=True)),
        sa.Column("run_sealed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "suite_id",
            "run_type",
            "execution_run_id",
            "evaluation_revision_id",
            name="uq_eval_baseline_run_revision_scope",
        ),
        if_not_exists=True,
    )
    op.create_table(
        "eval_baseline_pointers",
        sa.Column(
            "suite_id",
            sa.String(length=64),
            sa.ForeignKey("eval_suites.suite_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("run_type", sa.String(length=16), primary_key=True),
        sa.Column(
            "baseline_id",
            sa.String(length=64),
            sa.ForeignKey("eval_baselines.baseline_id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_eval_baselines_scope_history
        ON eval_baselines(suite_id, run_type, created_at DESC)
        """
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS eval_baselines_no_{action.lower()}
            BEFORE {action} ON eval_baselines
            BEGIN
                SELECT RAISE(ABORT, 'eval baselines are append-only');
            END
            """
        )


def downgrade() -> None:
    """Drop baseline pointer and history."""

    op.drop_table("eval_baseline_pointers")
    op.drop_table("eval_baselines")
