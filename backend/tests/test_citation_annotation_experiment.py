from __future__ import annotations

from app.evaluation.citation_annotation_experiment import summarize_records


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
