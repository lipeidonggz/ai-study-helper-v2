from __future__ import annotations

from pathlib import Path

import pytest

from app.agent.citations import CitationSpan
from app.evaluation.source_meta_experiment import (
    GoldCase,
    GoldOverride,
    SourceMetaGold,
    load_source_meta_gold,
    resolve_expected_labels,
    summarize_records,
)


def _span(span_id: str, text: str) -> CitationSpan:
    return CitationSpan(
        span_id=span_id,
        kind="sentence",
        start_index=0,
        end_index=len(text),
        text=text,
    )


def test_source_meta_gold_resolves_default_and_text_override() -> None:
    gold = SourceMetaGold(
        version="test",
        review_status="provisional",
        cases={
            "c14-001": GoldCase(
                default_source_meta=True,
                overrides=[
                    GoldOverride(
                        text_contains="字面核对结果",
                        source_meta=False,
                    )
                ],
            )
        },
    )
    spans = [
        _span("span_0001", "没有原文依据。"),
        _span("span_0002", "字面核对结果："),
    ]

    expected = resolve_expected_labels(case_id="c14-001", spans=spans, gold=gold)

    assert expected == {"span_0001": True, "span_0002": False}


def test_source_meta_gold_rejects_ambiguous_override() -> None:
    gold = SourceMetaGold(
        version="test",
        review_status="provisional",
        cases={
            "c1-001": GoldCase(
                default_source_meta=False,
                overrides=[GoldOverride(text_contains="共同文本", source_meta=True)],
            )
        },
    )
    spans = [
        _span("span_0001", "共同文本甲。"),
        _span("span_0002", "共同文本乙。"),
    ]

    with pytest.raises(ValueError, match="ambiguous"):
        resolve_expected_labels(case_id="c1-001", spans=spans, gold=gold)


def test_source_meta_summary_counts_false_exclusion_by_decision_and_run() -> None:
    attempt_id = "attempt_1"
    spans = [
        _span("span_0001", "正文事实。"),
        _span("span_0002", "来源中没有该说法。"),
    ]
    records = [
        {
            "attempt_id": attempt_id,
            "repeat": 1,
            "status": "complete",
            "decisions": {
                "span_0001": {
                    "source_meta": True,
                    "category": "source_structure",
                    "reason": "错误排除",
                },
                "span_0002": {
                    "source_meta": False,
                    "category": "",
                    "reason": "错误放行",
                },
            },
        },
        {
            "attempt_id": attempt_id,
            "repeat": 2,
            "status": "complete",
            "decisions": {
                "span_0001": {
                    "source_meta": False,
                    "category": "",
                    "reason": "正文",
                },
                "span_0002": {
                    "source_meta": True,
                    "category": "source_coverage_not_appear",
                    "reason": "来源缺失",
                },
            },
        },
    ]

    summary = summarize_records(
        records=records,
        spans_by_attempt={attempt_id: spans},
        expected_by_attempt={attempt_id: {"span_0001": False, "span_0002": True}},
        repeats=2,
    )

    assert summary["false_exclusion_decisions"] == 1
    assert summary["false_pass_decisions"] == 1
    assert summary["false_exclusion_runs"] == 1
    assert summary["false_pass_runs"] == 1
    assert summary["correct_all"] == 0
    assert summary["protocol_errors"] == 0


def test_repository_gold_fixture_loads() -> None:
    path = Path(__file__).parent / "fixtures" / "citation_source_meta_gold_v6.json"

    gold = load_source_meta_gold(path)

    assert gold.version == "citation-source-meta-calibration-v6"
    assert gold.review_status == "provisional"
    assert "c14-001" in gold.cases
