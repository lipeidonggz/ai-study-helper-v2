from __future__ import annotations

from app.agent.citations import CitationAnnotationInput, CitationEvidenceBlock, CitationSpan
from app.evaluation.citation_annotation_experiment import (
    restore_frozen_evidence_ids,
    summarize_records,
)
from app.evaluation.evidence import EvidenceBlock


def test_citation_annotation_summary_keeps_attempt_and_span_identity() -> None:
    records = [
        {
            "attempt_id": "attempt_1",
            "status": "complete",
            "annotations": [
                {
                    "span_id": "span_0001",
                    "reason": "证据一直接支持。",
                    "block_ids": ["block_1"],
                }
            ],
        },
        {
            "attempt_id": "attempt_2",
            "status": "complete",
            "annotations": [
                {
                    "span_id": "span_0001",
                    "reason": "证据二直接支持。",
                    "block_ids": ["block_2"],
                }
            ],
        },
    ]

    summary = summarize_records(records=records, repeats=1)

    assert summary["attempt_count"] == 2
    assert summary["reason_coverage"] == 1.0
    assert set(summary["reasons_by_span"]) == {
        "attempt_1:span_0001",
        "attempt_2:span_0001",
    }
    assert summary["blocks_by_span"]["attempt_1:span_0001"] == [
        {"block_ids": ["block_1"], "count": 1}
    ]


def test_frozen_evidence_short_references_restore_real_block_ids() -> None:
    packet = CitationAnnotationInput(
        question="question",
        answer="claim",
        spans=[
            CitationSpan(
                span_id="span_0001",
                kind="sentence",
                start_index=0,
                end_index=5,
                text="claim",
            )
        ],
        evidence_blocks=[
            CitationEvidenceBlock(
                block_id="evidence_001",
                doc_id="doc_1",
                doc_title="Document",
                source_label="local",
                section_path=["Section"],
                text="source text",
            )
        ],
    )

    restored = restore_frozen_evidence_ids(
        packet,
        [
            EvidenceBlock(
                block_id="doc_1#v0001#c1#b00001",
                doc_id="doc_1",
                doc_title="Document",
                source_label="local",
                section_path=["Section"],
                text="source text",
                cited_in_answer=False,
            )
        ],
    )

    assert restored.evidence_blocks[0].block_id == "doc_1#v0001#c1#b00001"
