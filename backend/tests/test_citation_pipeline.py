from __future__ import annotations

import asyncio
import json

from app.agent.citation_pipeline import CitationPipeline
from app.agent.citations import CitationEvidenceBlock
from app.agent.token_counter import ConservativeObservationTokenCounter
from app.providers.chat import FakeChatProvider


def _evidence() -> list[CitationEvidenceBlock]:
    return [
        CitationEvidenceBlock(
            block_id="block_1",
            doc_id="doc_1",
            doc_title="测试文档",
            source_label="local",
            section_path=["测试"],
            text="测试证据。",
        )
    ]


def _pipeline(provider: FakeChatProvider) -> CitationPipeline:
    return CitationPipeline(
        provider,
        token_counter=ConservativeObservationTokenCounter(),
    )


def test_pipeline_excludes_source_meta_before_annotation_and_audits_reason() -> None:
    answer = "MCP 共出现 2 次。MCP 是隔离边界。"
    provider = FakeChatProvider(
        [
            json.dumps(
                {
                    "assessments": [
                        {
                            "span_id": "span_0001",
                            "has_content_claim": False,
                            "source_relation": "coverage_stat",
                            "answer_relation": "none",
                            "reason": "统计来源中的出现次数",
                        },
                        {
                            "span_id": "span_0002",
                            "has_content_claim": True,
                            "source_relation": "none",
                            "answer_relation": "none",
                            "reason": "可核验正文命题",
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "annotations": [
                        {
                            "span_id": "span_0002",
                            "block_ids": ["block_1"],
                        }
                    ]
                }
            ),
        ],
        auto_empty_citation_annotations=False,
        auto_source_meta_assertions=False,
    )

    outcome = asyncio.run(
        _pipeline(provider).annotate(
            question="问题",
            answer=answer,
            evidence_blocks=_evidence(),
            spent_input_tokens=0,
            spent_output_tokens=0,
            max_context_tokens=100_000,
            max_request_tokens=400_000,
        )
    )

    assert outcome.artifact.status == "complete"
    assert outcome.artifact.schema_version == "citation-annotation-v4"
    assert outcome.artifact.span_count == 2
    assert outcome.artifact.source_meta.status == "complete"
    assert outcome.artifact.source_meta.rule_version == "citation-source-meta-rules-v4"
    assert outcome.artifact.source_meta.excluded_span_count == 1
    assert outcome.artifact.source_meta.decisions[0].reason == "统计来源中的出现次数"
    assert [annotation.span_id for annotation in outcome.artifact.annotations] == ["span_0002"]
    assert outcome.model_calls == 2
    annotation_packet = json.loads(provider.requests[1][-1].content)
    assert [span["span_id"] for span in annotation_packet["spans"]] == ["span_0002"]


def test_pipeline_source_meta_failure_falls_back_to_all_spans() -> None:
    provider = FakeChatProvider(
        [
            "not-json",
            "not-json",
            json.dumps(
                {
                    "annotations": [
                        {
                            "span_id": "span_0001",
                            "block_ids": ["block_1"],
                        }
                    ]
                }
            ),
        ],
        auto_empty_citation_annotations=False,
        auto_source_meta_assertions=False,
    )

    outcome = asyncio.run(
        _pipeline(provider).annotate(
            question="问题",
            answer="正文事实。",
            evidence_blocks=_evidence(),
            spent_input_tokens=0,
            spent_output_tokens=0,
            max_context_tokens=100_000,
            max_request_tokens=400_000,
        )
    )

    assert outcome.artifact.status == "complete"
    assert outcome.artifact.source_meta.status == "pending"
    assert outcome.artifact.source_meta.fallback_to_all_spans is True
    assert outcome.artifact.error_code is None
    assert [annotation.span_id for annotation in outcome.artifact.annotations] == ["span_0001"]
    assert outcome.model_calls == 3
    assert len(provider.requests) == 3
    annotation_packet = json.loads(provider.requests[2][-1].content)
    assert [span["span_id"] for span in annotation_packet["spans"]] == ["span_0001"]


def test_pipeline_skips_annotator_when_gate_excludes_every_span() -> None:
    provider = FakeChatProvider(
        [
            json.dumps(
                {
                    "assessments": [
                        {
                            "span_id": "span_0001",
                            "has_content_claim": False,
                            "source_relation": "coverage_not_appear",
                            "answer_relation": "none",
                            "reason": "来源中没有该说法",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ],
        auto_empty_citation_annotations=False,
        auto_source_meta_assertions=False,
    )

    outcome = asyncio.run(
        _pipeline(provider).annotate(
            question="问题",
            answer="文档没有提到该说法。",
            evidence_blocks=_evidence(),
            spent_input_tokens=0,
            spent_output_tokens=0,
            max_context_tokens=100_000,
            max_request_tokens=400_000,
        )
    )

    assert outcome.artifact.status == "complete"
    assert outcome.artifact.source_meta.excluded_span_count == 1
    assert outcome.artifact.annotations == []
    assert outcome.model_calls == 1
    assert len(provider.requests) == 1
