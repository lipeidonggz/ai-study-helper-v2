"""Allow a corrected evaluation state to become a new immutable baseline.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-24
"""

from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Key baseline idempotency by the frozen result state, not revision alone."""

    _drop_baseline_immutability_triggers()
    op.add_column(
        "eval_baselines",
        sa.Column("result_matrix_hash", sa.String(length=64), nullable=True),
    )
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT baseline_id, result_matrix_json FROM eval_baselines")
    ).mappings()
    for row in rows:
        result_matrix_hash = hashlib.sha256(row["result_matrix_json"].encode("utf-8")).hexdigest()
        connection.execute(
            sa.text(
                "UPDATE eval_baselines "
                "SET result_matrix_hash = :result_matrix_hash "
                "WHERE baseline_id = :baseline_id"
            ),
            {
                "result_matrix_hash": result_matrix_hash,
                "baseline_id": row["baseline_id"],
            },
        )
    with op.batch_alter_table("eval_baselines", recreate="always") as batch:
        batch.alter_column(
            "result_matrix_hash",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch.drop_constraint(
            "uq_eval_baseline_run_revision_scope",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_eval_baseline_result_state",
            [
                "suite_id",
                "run_type",
                "execution_run_id",
                "evaluation_revision_id",
                "result_matrix_hash",
            ],
        )
    _create_baseline_immutability_triggers()


def downgrade() -> None:
    """Restore revision-only uniqueness; fail if multiple result states now exist."""

    _drop_baseline_immutability_triggers()
    with op.batch_alter_table("eval_baselines", recreate="always") as batch:
        batch.drop_constraint(
            "uq_eval_baseline_result_state",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_eval_baseline_run_revision_scope",
            [
                "suite_id",
                "run_type",
                "execution_run_id",
                "evaluation_revision_id",
            ],
        )
        batch.drop_column("result_matrix_hash")
    _create_baseline_immutability_triggers()


def _drop_baseline_immutability_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS eval_baselines_no_update")
    op.execute("DROP TRIGGER IF EXISTS eval_baselines_no_delete")


def _create_baseline_immutability_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS eval_baselines_no_update
        BEFORE UPDATE ON eval_baselines
        BEGIN
            SELECT RAISE(ABORT, 'eval baselines are append-only');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER IF NOT EXISTS eval_baselines_no_delete
        BEFORE DELETE ON eval_baselines
        BEGIN
            SELECT RAISE(ABORT, 'eval baselines are append-only');
        END
        """
    )
