from __future__ import annotations

import asyncio
import json

import pytest

from app.agent.citation_source_meta import (
    CITATION_SOURCE_META_PROMPT_HASH,
    CITATION_SOURCE_META_PROMPT_VERSION,
    CITATION_SOURCE_META_RULE_VERSION,
    CitationSourceMetaClassifier,
    CitationSourceMetaError,
    parse_citation_source_meta_output,
    render_citation_source_meta_messages,
)
from app.agent.citations import split_answer_spans
from app.agent.token_counter import ConservativeObservationTokenCounter
from app.providers.chat import FakeChatProvider


def _assessments(
    spans,
    *,
    source_meta: bool = False,
) -> str:
    return json.dumps(
        {
            "assessments": [
                {
                    "span_id": span.span_id,
                    "has_content_claim": not source_meta,
                    "source_relation": "location" if source_meta else "none",
                    "answer_relation": "none",
                    "reason": "来源结构说明" if source_meta else "正文知识命题",
                }
                for span in spans
            ]
        },
        ensure_ascii=False,
    )


def test_source_meta_prompt_exposes_structure_role_and_fixed_schema() -> None:
    spans = split_answer_spans("第一事实。第二事实。")
    messages = render_citation_source_meta_messages(
        question="问题",
        answer="第一事实。第二事实。",
        spans=spans,
    )

    payload = json.loads(messages[-1].content)
    assert CITATION_SOURCE_META_PROMPT_VERSION == "citation-source-meta-v8"
    assert CITATION_SOURCE_META_RULE_VERSION == "citation-source-meta-rules-v4"
    assert len(CITATION_SOURCE_META_PROMPT_HASH) == 64
    assert [item["structure_role"] for item in payload["spans"]] == [
        "body_sentence",
        "body_sentence",
    ]
    assert "has_content_claim" in messages[0].content
    assert "source_relation" in messages[0].content
    assert "answer_relation" in messages[0].content
    assert "无法确定时必须放行到正文引用标注" in messages[0].content


def test_parser_accepts_omitted_empty_category_for_body_span() -> None:
    spans = split_answer_spans("正文事实。")
    raw_output = json.dumps(
        {
            "assessments": [
                {
                    "span_id": "span_0001",
                    "has_content_claim": True,
                    "source_relation": "none",
                    "answer_relation": "none",
                    "reason": "正文知识命题",
                }
            ]
        },
        ensure_ascii=False,
    )

    decisions = parse_citation_source_meta_output(raw_output, spans=spans)

    assert len(decisions) == 1
    assert decisions[0].source_meta is False
    assert decisions[0].category == ""


@pytest.mark.parametrize(
    ("assessment", "source_meta", "category"),
    [
        (
            {
                "span_id": "span_0001",
                "has_content_claim": False,
                "source_relation": "coverage_not_appear",
                "answer_relation": "none",
                "reason": "来源没有该内容",
            },
            True,
            "source_coverage_not_appear",
        ),
        (
            {
                "span_id": "span_0001",
                "has_content_claim": False,
                "source_relation": "none",
                "answer_relation": "answer_process",
                "reason": "说明本次回答证据不足",
            },
            False,
            "",
        ),
        (
            {
                "span_id": "span_0001",
                "has_content_claim": False,
                "source_relation": "location",
                "answer_relation": "none",
                "reason": "只说明所在位置",
            },
            False,
            "",
        ),
        (
            {
                "span_id": "span_0001",
                "has_content_claim": True,
                "source_relation": "location",
                "answer_relation": "none",
                "reason": "正在解释该处内容",
            },
            False,
            "",
        ),
        (
            {
                "span_id": "span_0001",
                "has_content_claim": True,
                "source_relation": "location",
                "answer_relation": "answer_process",
                "reason": "同时包含实质内容与回答组织说明",
            },
            False,
            "",
        ),
        (
            {
                "span_id": "span_0001",
                "has_content_claim": False,
                "source_relation": "none",
                "answer_relation": "none",
                "reason": "纯提纲标签默认进入正文",
            },
            False,
            "",
        ),
        (
            {
                "span_id": "span_0001",
                "has_content_claim": True,
                "source_relation": "coverage_stat",
                "answer_relation": "none",
                "reason": "核心命题是来源中的出现次数",
            },
            True,
            "source_coverage_stat",
        ),
    ],
)
def test_parser_derives_gate_decision_from_semantic_signals(
    assessment: dict[str, object],
    source_meta: bool,
    category: str,
) -> None:
    spans = split_answer_spans("唯一事实。")
    raw_output = json.dumps({"assessments": [assessment]}, ensure_ascii=False)

    decisions = parse_citation_source_meta_output(raw_output, spans=spans)

    assert decisions[0].source_meta is source_meta
    assert decisions[0].category == category


def test_parser_rejects_incomplete_span_set() -> None:
    spans = split_answer_spans("第一事实。第二事实。")
    raw_output = json.dumps(
        {
            "assessments": [
                {
                    "span_id": "span_0001",
                    "has_content_claim": True,
                    "source_relation": "none",
                    "answer_relation": "none",
                    "reason": "正文",
                }
            ]
        },
        ensure_ascii=False,
    )

    with pytest.raises(CitationSourceMetaError) as error:
        parse_citation_source_meta_output(raw_output, spans=spans)

    assert error.value.code == "citation_source_meta_span_set_invalid"


def test_source_structure_requires_a_pure_location_pointer() -> None:
    spans = split_answer_spans("相关内容位于第二章。文档在结尾对比表中给出三类方案的代价与边界。")
    raw_output = json.dumps(
        {
            "assessments": [
                {
                    "span_id": span.span_id,
                    "has_content_claim": False,
                    "source_relation": "location",
                    "answer_relation": "none",
                    "reason": "说明来源位置",
                }
                for span in spans
            ]
        },
        ensure_ascii=False,
    )

    decisions = parse_citation_source_meta_output(raw_output, spans=spans)

    assert decisions[0].source_meta is True
    assert decisions[0].category == "source_structure"
    assert decisions[1].source_meta is False
    assert decisions[1].category == ""


def test_answer_provenance_requires_an_explicit_answer_process_signal() -> None:
    spans = split_answer_spans(
        "下面先分别说明两篇文档。本次回答未完整完成。建议标注为基于文档的推论。"
    )
    raw_output = json.dumps(
        {
            "assessments": [
                {
                    "span_id": span.span_id,
                    "has_content_claim": False,
                    "source_relation": "none",
                    "answer_relation": "answer_process",
                    "reason": "说明回答组织或过程",
                }
                for span in spans
            ]
        },
        ensure_ascii=False,
    )

    decisions = parse_citation_source_meta_output(raw_output, spans=spans)

    assert decisions[0].source_meta is False
    assert decisions[1].source_meta is True
    assert decisions[1].category == "answer_provenance"
    assert decisions[2].source_meta is True
    assert decisions[2].category == "answer_provenance"


def test_classifier_retries_invalid_output_once() -> None:
    spans = split_answer_spans("第一事实。第二事实。")
    provider = FakeChatProvider(
        ["not-json", _assessments(spans)],
        auto_source_meta_assertions=False,
    )

    outcome = asyncio.run(
        CitationSourceMetaClassifier(
            provider,
            token_counter=ConservativeObservationTokenCounter(),
        ).classify(
            question="问题",
            answer="第一事实。第二事实。",
            spans=spans,
            spent_input_tokens=0,
            spent_output_tokens=0,
            max_context_tokens=100_000,
            max_request_tokens=400_000,
        )
    )

    assert outcome.status == "complete"
    assert outcome.model_calls == 2
    assert len(outcome.raw_outputs) == 2
    assert [decision.source_meta for decision in outcome.decisions] == [False, False]


def test_classifier_becomes_pending_after_second_invalid_output() -> None:
    spans = split_answer_spans("唯一事实。")
    provider = FakeChatProvider(
        ["not-json", "not-json"],
        auto_source_meta_assertions=False,
    )

    outcome = asyncio.run(
        CitationSourceMetaClassifier(
            provider,
            token_counter=ConservativeObservationTokenCounter(),
        ).classify(
            question="问题",
            answer="唯一事实。",
            spans=spans,
            spent_input_tokens=0,
            spent_output_tokens=0,
            max_context_tokens=100_000,
            max_request_tokens=400_000,
        )
    )

    assert outcome.status == "pending"
    assert outcome.error_code == "citation_source_meta_output_invalid"
    assert outcome.model_calls == 2
    assert len(outcome.raw_outputs) == 2


def test_classifier_context_budget_failure_skips_model() -> None:
    spans = split_answer_spans("唯一事实。")
    provider = FakeChatProvider(["unused"])

    outcome = asyncio.run(
        CitationSourceMetaClassifier(
            provider,
            token_counter=ConservativeObservationTokenCounter(),
        ).classify(
            question="问题",
            answer="唯一事实。",
            spans=spans,
            spent_input_tokens=0,
            spent_output_tokens=0,
            max_context_tokens=1,
            max_request_tokens=100_000,
        )
    )

    assert outcome.status == "pending"
    assert outcome.error_code == "citation_source_meta_context_exceeded"
    assert outcome.model_calls == 0
    assert provider.requests == []
