"""Store candidate summaries as one sibling list.

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-10
"""

from __future__ import annotations

import json
import re
import unicodedata

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def _normalize_summary(value: str) -> str:
    """Match the candidate summary normalization used by the application."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def _unique_summaries(primary: str, variants_json: str) -> list[str]:
    """Flatten legacy primary/variant fields into sibling summaries."""

    variants = json.loads(variants_json)
    if not isinstance(variants, list) or not all(isinstance(item, str) for item in variants):
        raise ValueError("summary_variants_json must contain only strings")
    result: list[str] = []
    seen: set[str] = set()
    for value in [primary, *variants]:
        text = " ".join(value.split())
        key = _normalize_summary(text)
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    if not result:
        raise ValueError("candidate must contain at least one summary")
    return result


def upgrade() -> None:
    """Replace summary variants with one normalized sibling list."""

    op.add_column(
        "preview_candidates",
        sa.Column("summaries_json", sa.Text(), nullable=True),
    )
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.text("SELECT candidate_id, summary, summary_variants_json FROM preview_candidates")
        )
        .mappings()
        .all()
    )
    for row in rows:
        summaries = _unique_summaries(
            str(row["summary"]),
            str(row["summary_variants_json"]),
        )
        connection.execute(
            sa.text(
                "UPDATE preview_candidates "
                "SET summaries_json = :summaries_json "
                "WHERE candidate_id = :candidate_id"
            ),
            {
                "candidate_id": row["candidate_id"],
                "summaries_json": json.dumps(
                    summaries,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
    with op.batch_alter_table("preview_candidates") as batch_op:
        batch_op.alter_column("summaries_json", nullable=False)
        batch_op.drop_column("summary_variants_json")
        batch_op.drop_column("summary")


def downgrade() -> None:
    """Restore legacy primary and variant fields for local development."""

    op.add_column(
        "preview_candidates",
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "preview_candidates",
        sa.Column("summary_variants_json", sa.Text(), nullable=True),
    )
    connection = op.get_bind()
    rows = (
        connection.execute(sa.text("SELECT candidate_id, summaries_json FROM preview_candidates"))
        .mappings()
        .all()
    )
    for row in rows:
        summaries = json.loads(str(row["summaries_json"]))
        if not isinstance(summaries, list) or not summaries:
            raise ValueError("summaries_json must contain at least one string")
        connection.execute(
            sa.text(
                "UPDATE preview_candidates "
                "SET summary = :summary, summary_variants_json = :variants "
                "WHERE candidate_id = :candidate_id"
            ),
            {
                "candidate_id": row["candidate_id"],
                "summary": str(summaries[0]),
                "variants": json.dumps(
                    summaries[1:],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        )
    with op.batch_alter_table("preview_candidates") as batch_op:
        batch_op.alter_column("summary", nullable=False)
        batch_op.alter_column("summary_variants_json", nullable=False)
        batch_op.drop_column("summaries_json")
