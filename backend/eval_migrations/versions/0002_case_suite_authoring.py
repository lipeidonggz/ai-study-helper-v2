"""Add mutable drafts, atomic publication fields and exploratory plans.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the 022B authoring workflow without rewriting published facts."""

    op.execute("DROP TRIGGER IF EXISTS eval_plan_case_content_immutable")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_eval_execution_plans")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_eval_plan_cases")
    op.create_table(
        "eval_case_drafts",
        sa.Column(
            "case_id",
            sa.String(length=32),
            sa.ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "base_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("target_bindings_json", sa.Text(), nullable=False),
        sa.Column("distractor_metadata_json", sa.Text(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("execution_policy_json", sa.Text(), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "eval_case_draft_criteria",
        sa.Column("criterion_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "case_id",
            sa.String(length=32),
            sa.ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "base_revision_id",
            sa.String(length=96),
            sa.ForeignKey(
                "eval_criterion_revisions.revision_id",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column("rule_type", sa.String(length=1), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("rule_text", sa.Text(), nullable=False),
        sa.Column("evaluator", sa.String(length=16), nullable=False),
        sa.Column("evaluator_ref", sa.Text(), nullable=True),
        if_not_exists=True,
    )
    op.create_table(
        "eval_suite_drafts",
        sa.Column(
            "suite_id",
            sa.String(length=64),
            sa.ForeignKey("eval_suites.suite_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "base_revision_id",
            sa.String(length=96),
            sa.ForeignKey("eval_suite_revisions.revision_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("change_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        if_not_exists=True,
    )
    op.create_table(
        "eval_suite_draft_cases",
        sa.Column(
            "suite_id",
            sa.String(length=64),
            sa.ForeignKey("eval_suite_drafts.suite_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "case_revision_id",
            sa.String(length=64),
            sa.ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        if_not_exists=True,
    )

    inspector = sa.inspect(op.get_bind())

    def columns(table_name: str) -> set[str]:
        return {column["name"] for column in inspector.get_columns(table_name)}

    if "change_note" not in columns("eval_case_revisions"):
        with op.batch_alter_table("eval_case_revisions") as batch:
            batch.add_column(sa.Column("change_note", sa.Text(), nullable=False, server_default=""))
    if "change_note" not in columns("eval_criterion_revisions"):
        with op.batch_alter_table("eval_criterion_revisions") as batch:
            batch.add_column(sa.Column("change_note", sa.Text(), nullable=False, server_default=""))
    with op.batch_alter_table("eval_suite_revisions") as batch:
        suite_columns = columns("eval_suite_revisions")
        if "name" not in suite_columns:
            batch.add_column(sa.Column("name", sa.String(length=128), nullable=True))
        if "description" not in suite_columns:
            batch.add_column(sa.Column("description", sa.Text(), nullable=True))
        if "change_note" not in suite_columns:
            batch.add_column(sa.Column("change_note", sa.Text(), nullable=False, server_default=""))
    with op.batch_alter_table("eval_execution_plans") as batch:
        if "mode" not in columns("eval_execution_plans"):
            batch.add_column(
                sa.Column("mode", sa.String(length=24), nullable=False, server_default="formal")
            )
    with op.batch_alter_table("eval_plan_cases") as batch:
        plan_case_columns = columns("eval_plan_cases")
        if "source_n" not in plan_case_columns:
            batch.add_column(
                sa.Column("source_n", sa.Integer(), nullable=False, server_default="0")
            )
        if "source_k" not in plan_case_columns:
            batch.add_column(
                sa.Column("source_k", sa.Integer(), nullable=False, server_default="0")
            )

    with op.batch_alter_table("eval_execution_plans") as batch:
        batch.alter_column(
            "suite_revision_id",
            existing_type=sa.String(length=96),
            nullable=True,
        )

    op.execute(
        """
        UPDATE eval_suite_revisions
        SET name = (
                SELECT suite_id
                FROM eval_suites
                WHERE eval_suites.suite_id = eval_suite_revisions.suite_id
            ),
            description = ''
        WHERE name IS NULL
        """
    )
    op.execute("UPDATE eval_plan_cases SET source_n = n, source_k = k")
    op.execute(
        """
        UPDATE eval_case_revisions AS current
        SET review_status = 'retired'
        WHERE current.review_status = 'active'
          AND EXISTS (
              SELECT 1
              FROM eval_case_revisions AS newer
              WHERE newer.case_id = current.case_id
                AND newer.review_status = 'active'
                AND newer.revision_number > current.revision_number
          )
        """
    )
    op.execute(
        """
        UPDATE eval_suite_revisions AS current
        SET review_status = 'retired'
        WHERE current.review_status = 'active'
          AND EXISTS (
              SELECT 1
              FROM eval_suite_revisions AS newer
              WHERE newer.suite_id = current.suite_id
                AND newer.review_status = 'active'
                AND newer.revision_number > current.revision_number
          )
        """
    )

    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_case_revision_number
        ON eval_case_revisions(case_id, revision_number)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_suite_revision_number
        ON eval_suite_revisions(suite_id, revision_number)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_case_revision_one_active
        ON eval_case_revisions(case_id)
        WHERE review_status = 'active'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_suite_revision_one_active
        ON eval_suite_revisions(suite_id)
        WHERE review_status = 'active'
        """
    )

    op.execute("DROP TRIGGER IF EXISTS eval_case_revision_content_immutable")
    op.execute(
        """
        CREATE TRIGGER eval_case_revision_content_immutable
        BEFORE UPDATE OF
            case_id, revision_number, category, question, target_bindings_json,
            distractor_metadata_json, n, k, execution_policy_json, source_ref,
            content_hash, change_note
        ON eval_case_revisions
        BEGIN
            SELECT RAISE(ABORT, 'case revision content is immutable');
        END
        """
    )
    op.execute("DROP TRIGGER IF EXISTS eval_criterion_revision_content_immutable")
    op.execute(
        """
        CREATE TRIGGER eval_criterion_revision_content_immutable
        BEFORE UPDATE OF
            criterion_id, revision_number, rule_type, ordinal, title,
            rule_text, evaluator, evaluator_ref, source_ref, content_hash,
            change_note
        ON eval_criterion_revisions
        BEGIN
            SELECT RAISE(ABORT, 'criterion revision content is immutable');
        END
        """
    )
    op.execute("DROP TRIGGER IF EXISTS eval_suite_revision_content_immutable")
    op.execute(
        """
        CREATE TRIGGER eval_suite_revision_content_immutable
        BEFORE UPDATE OF
            suite_id, revision_number, name, description, scope_id,
            content_hash, change_note
        ON eval_suite_revisions
        BEGIN
            SELECT RAISE(ABORT, 'suite revision content is immutable');
        END
        """
    )
    op.execute("DROP TRIGGER IF EXISTS eval_execution_plan_content_immutable")
    op.execute(
        """
        CREATE TRIGGER eval_execution_plan_content_immutable
        BEFORE UPDATE OF
            suite_revision_id, collection_snapshot_id, run_type,
            agent_version_set_json, model_config_json, run_policy_json,
            experiment_variable_json, preflight_result_json, mode
        ON eval_execution_plans
        BEGIN
            SELECT RAISE(ABORT, 'execution plan content is immutable');
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER eval_plan_case_content_immutable
        BEFORE UPDATE ON eval_plan_cases
        BEGIN
            SELECT RAISE(ABORT, 'execution plan cases are immutable');
        END
        """
    )


def downgrade() -> None:
    """Remove authoring workflow objects while retaining core execution facts."""

    op.execute("DROP TRIGGER IF EXISTS eval_execution_plan_content_immutable")
    op.execute("DROP TRIGGER IF EXISTS eval_suite_revision_content_immutable")
    op.execute("DROP TRIGGER IF EXISTS eval_criterion_revision_content_immutable")
    op.execute("DROP TRIGGER IF EXISTS eval_case_revision_content_immutable")
    op.drop_index("uq_eval_suite_revision_one_active", table_name="eval_suite_revisions")
    op.drop_index("uq_eval_case_revision_one_active", table_name="eval_case_revisions")
    op.drop_index("uq_eval_suite_revision_number", table_name="eval_suite_revisions")
    op.drop_index("uq_eval_case_revision_number", table_name="eval_case_revisions")
    with op.batch_alter_table("eval_plan_cases") as batch:
        batch.drop_column("source_k")
        batch.drop_column("source_n")
    with op.batch_alter_table("eval_execution_plans") as batch:
        batch.drop_column("mode")
        batch.alter_column(
            "suite_revision_id",
            existing_type=sa.String(length=96),
            nullable=False,
        )
    with op.batch_alter_table("eval_suite_revisions") as batch:
        batch.drop_column("change_note")
        batch.drop_column("description")
        batch.drop_column("name")
    with op.batch_alter_table("eval_criterion_revisions") as batch:
        batch.drop_column("change_note")
    with op.batch_alter_table("eval_case_revisions") as batch:
        batch.drop_column("change_note")
    op.drop_table("eval_suite_draft_cases")
    op.drop_table("eval_suite_drafts")
    op.drop_table("eval_case_draft_criteria")
    op.drop_table("eval_case_drafts")
