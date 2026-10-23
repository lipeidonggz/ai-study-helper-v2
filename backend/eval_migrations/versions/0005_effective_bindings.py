"""Add effective judgment bindings and human LLM check reviews.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create append-only binding/review facts and backfill current judgments."""

    op.execute("DROP TRIGGER IF EXISTS eval_judgments_no_update")
    op.execute("DROP TRIGGER IF EXISTS eval_judgments_no_delete")
    with op.batch_alter_table("eval_judgments", recreate="always") as batch:
        batch.drop_constraint(
            "uq_eval_judgment_revision_attempt_criterion",
            type_="unique",
        )
    op.create_table(
        "eval_judgment_bindings",
        sa.Column("binding_order", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("binding_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "evaluation_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_evaluation_revisions.revision_id", ondelete="RESTRICT"),
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
            sa.ForeignKey("eval_criterion_revisions.revision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "judgment_id",
            sa.String(length=64),
            sa.ForeignKey("eval_judgments.judgment_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_eval_judgment_bindings_current
        ON eval_judgment_bindings(
            evaluation_revision_id, attempt_id, criterion_revision_id, binding_order
        )
        """
    )
    op.create_table(
        "eval_check_reviews",
        sa.Column("review_order", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("review_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "evaluation_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_evaluation_revisions.revision_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(length=64),
            sa.ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("checker_id", sa.String(length=96), nullable=False),
        sa.Column(
            "source_check_result_id",
            sa.String(length=64),
            sa.ForeignKey("eval_check_results.check_result_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(length=96), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("reviewer", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_eval_check_reviews_current
        ON eval_check_reviews(
            evaluation_revision_id, attempt_id, checker_id, review_order
        )
        """
    )
    op.execute(
        """
        INSERT INTO eval_judgment_bindings (
            binding_id, evaluation_revision_id, attempt_id,
            criterion_revision_id, judgment_id, bound_at
        )
        SELECT
            'binding_' || lower(hex(randomblob(16))),
            judgment.evaluation_revision_id,
            judgment.attempt_id,
            judgment.criterion_revision_id,
            judgment.judgment_id,
            judgment.created_at
        FROM eval_judgments AS judgment
        WHERE NOT EXISTS (
            SELECT 1
            FROM eval_judgment_bindings AS binding
            WHERE binding.evaluation_revision_id = judgment.evaluation_revision_id
              AND binding.attempt_id = judgment.attempt_id
              AND binding.criterion_revision_id = judgment.criterion_revision_id
              AND binding.judgment_id = judgment.judgment_id
        )
        """
    )
    for table_name in (
        "eval_judgments",
        "eval_judgment_bindings",
        "eval_check_reviews",
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
    """Drop effective binding and check review facts."""

    op.drop_table("eval_check_reviews")
    op.drop_table("eval_judgment_bindings")
    with op.batch_alter_table("eval_judgments", recreate="always") as batch:
        batch.create_unique_constraint(
            "uq_eval_judgment_revision_attempt_criterion",
            ["evaluation_revision_id", "attempt_id", "criterion_revision_id"],
        )
