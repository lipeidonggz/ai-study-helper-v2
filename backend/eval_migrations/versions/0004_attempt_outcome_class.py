"""Classify attempts by evaluation sample eligibility.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add and backfill the outcome class without changing execution facts."""

    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("eval_attempts")}
    if "outcome_class" not in columns:
        with op.batch_alter_table("eval_attempts") as batch:
            batch.add_column(
                sa.Column(
                    "outcome_class",
                    sa.String(length=16),
                    nullable=False,
                    server_default="unknown",
                )
            )
    op.execute(
        """
        UPDATE eval_attempts
        SET outcome_class = CASE
            WHEN execution_status IN ('succeeded', 'partial') THEN 'product'
            WHEN execution_status = 'canceled' THEN 'canceled'
            WHEN execution_status = 'outcome_unknown' THEN 'unknown'
            WHEN error_code LIKE 'chat_%'
              OR error_code LIKE 'embedding_%'
              OR error_code IN (
                'evaluation_attempt_exception',
                'evaluation_attempt_timeout',
                'evaluation_process_restarted',
                'evaluation_worker_exception',
                'harness_internal_error'
              )
            THEN 'infrastructure'
            WHEN execution_status = 'failed' THEN 'product'
            ELSE 'unknown'
        END
        """
    )


def downgrade() -> None:
    """Remove sample eligibility classification."""

    with op.batch_alter_table("eval_attempts") as batch:
        batch.drop_column("outcome_class")
