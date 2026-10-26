from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from app.agent.citation_annotation_prompt import (
    CITATION_ANNOTATION_PROMPT_VERSION,
    CITATION_ANNOTATION_SYSTEM_PROMPT,
    render_citation_annotation_messages,
)
from app.agent.citations import (
    CITATION_ANNOTATION_BATCH_VERSION,
    CLAIM_SPAN_VERSION,
    LEGACY_CLAIM_SPAN_VERSION,
    CitationAnnotation,
    CitationAnnotationArtifact,
    CitationAnnotationInput,
    CitationAnnotator,
    CitationEvidenceBlock,
    citation_annotation_output_token_cap,
    split_answer_spans,
)
from app.agent.token_counter import ConservativeObservationTokenCounter
from app.evaluation.citation_prompt import CitationEvidenceInput
from app.evaluation.evidence import (
    JudgeInputError,
    _extract_evidence,
    build_generation_judge_input,
    build_judge_input,
)
from app.evaluation.judgment import _build_citation_task
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
    FakeChatProvider,
)
from app.tracing.models import (
    TraceArtifactRecord,
    TraceDetailRecord,
    TraceManifestRecord,
    TraceSpanRecord,
)


class _FailingProvider:
    def __init__(self) -> None:
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-annotation",
            base_url="http://localhost",
            temperature=0.0,
            max_output_tokens=4096,
            thinking_enabled=False,
        )

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        _messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        assert max_output_tokens == citation_annotation_output_token_cap(1)
        raise ChatProviderError(
            code="chat_upstream_unavailable",
            message="annotation upstream unavailable",
            retryable=True,
        )


class _BatchingAnnotationProvider:
    """为每批全部 span 返回一个合法 annotation，并记录批次边界。"""

    def __init__(self) -> None:
        self._profile = FakeChatProvider(["unused"]).profile
        self.span_counts: list[int] = []
        self.evidence_counts: list[int] = []
        self.max_output_tokens: list[int | None] = []

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        payload = json.loads(messages[-1].content)
        spans = payload["spans"]
        self.max_output_tokens.append(max_output_tokens)
        assert max_output_tokens == citation_annotation_output_token_cap(len(spans))
        evidence_blocks = payload["evidence_blocks"]
        self.span_counts.append(len(spans))
        self.evidence_counts.append(len(evidence_blocks))
        block_id = evidence_blocks[0]["block_id"]
        content = json.dumps(
            {
                "annotations": [
                    {"span_id": span["span_id"], "block_ids": [block_id]} for span in spans
                ]
            },
            ensure_ascii=False,
        )
        return ChatCompletionResult(
            content=content,
            request_id=f"batch-{len(self.span_counts)}",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=100,
            output_tokens=50,
        )


class _TruncatedAnnotationProvider:
    """持续返回被输出上限截断的 annotation 响应。"""

    def __init__(self) -> None:
        self._profile = FakeChatProvider(["unused"]).profile
        self.calls = 0

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        _messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        assert max_output_tokens == citation_annotation_output_token_cap(1)
        self.calls += 1
        return ChatCompletionResult(
            content='{"annotations":[{"span_id":"span_0001","block_ids":["bl',
            request_id=f"truncated-{self.calls}",
            actual_model_id=self.profile.model_id,
            finish_reason="length",
            input_tokens=100,
            output_tokens=4096,
        )


class _MemoryTraceCatalog:
    def __init__(self, trace: TraceDetailRecord, files: dict[str, Path]) -> None:
        self._trace = trace
        self._files = files

    def load(self, trace_id: str) -> TraceDetailRecord:
        assert trace_id == self._trace.manifest.trace_id
        return self._trace

    def artifact_path(self, trace_id: str, artifact_id: str) -> Path:
        assert trace_id == self._trace.manifest.trace_id
        return self._files[artifact_id]


def _evidence() -> list[CitationEvidenceBlock]:
    return [
        CitationEvidenceBlock(
            block_id="block_1",
            doc_id="doc_1",
            doc_title="测试文档",
            source_label="local",
            section_path=["边界"],
            text="MCP 沙箱是本文最重要的原始证据。",
        ),
        CitationEvidenceBlock(
            block_id="block_2",
            doc_id="doc_1",
            doc_title="测试文档",
            source_label="local",
            section_path=["边界"],
            text="本地进程只允许访问只读工具。",
        ),
    ]


def _annotate(
    *,
    answer: str,
    responses: Sequence[str],
    evidence_blocks: list[CitationEvidenceBlock] | None = None,
    max_context_tokens: int = 100_000,
    max_request_tokens: int = 400_000,
):
    provider = FakeChatProvider(
        responses,
        auto_empty_citation_annotations=False,
    )
    outcome = asyncio.run(
        CitationAnnotator(
            provider,
            token_counter=ConservativeObservationTokenCounter(),
        ).annotate(
            question="MCP 的边界是什么？",
            answer=answer,
            evidence_blocks=_evidence() if evidence_blocks is None else evidence_blocks,
            spent_input_tokens=10,
            spent_output_tokens=5,
            max_context_tokens=max_context_tokens,
            max_request_tokens=max_request_tokens,
        )
    )
    return outcome, provider


def test_split_answer_spans_covers_claims_without_headings_or_code() -> None:
    answer = (
        "第一句说明事实。第二句补充边界。\n\n"
        "- 列表第一项；列表第二项。\n\n"
        "| 列一 | 列二 |\n"
        "| --- | --- |\n"
        "| 甲 | 乙 |\n\n"
        "# 标题不是 claim\n\n"
        "```text\n代码不是 claim\n```\n"
    )

    spans = split_answer_spans(answer)

    assert [span.text for span in spans] == [
        "第一句说明事实。",
        "第二句补充边界。",
        "- 列表第一项；",
        "列表第二项。",
        "| 甲 | 乙 |",
    ]
    assert [span.kind for span in spans] == [
        "sentence",
        "sentence",
        "list_item",
        "list_item",
        "table_row",
    ]
    assert all(answer[span.start_index : span.end_index] == span.text for span in spans)
    assert [span.span_id for span in spans] == [
        "span_0001",
        "span_0002",
        "span_0003",
        "span_0004",
        "span_0005",
    ]


def test_split_answer_spans_keeps_inline_code_inside_one_sentence() -> None:
    answer = '判断 `find . -name "*.tmp" -exec rm {} \\;` 是否安全。下一句。'

    spans = split_answer_spans(answer)

    assert [span.text for span in spans] == [
        '判断 `find . -name "*.tmp" -exec rm {} \\;` 是否安全。',
        "下一句。",
    ]
    assert all(answer[span.start_index : span.end_index] == span.text for span in spans)


def test_split_answer_spans_does_not_emit_ordered_list_numbers_as_claims() -> None:
    answer = "1. **第一项**：事实一。\n2. **第二项**：事实二。\n"

    spans = split_answer_spans(answer)

    assert [span.text for span in spans] == [
        "1. **第一项**：事实一。",
        "2. **第二项**：事实二。",
    ]
    assert all(span.kind == "list_item" for span in spans)


def test_split_answer_spans_keeps_closing_emphasis_with_its_sentence() -> None:
    answer = (
        "**1. 通过已批准域名的外泄。** 沙箱本身工作正常，但数据仍被外泄。\n\n"
        "**2. EDR 看不见虚拟机内部。**"
    )

    spans = split_answer_spans(answer)

    assert [span.text for span in spans] == [
        "**1. 通过已批准域名的外泄。**",
        "沙箱本身工作正常，但数据仍被外泄。",
        "**2. EDR 看不见虚拟机内部。**",
    ]
    assert all(answer[span.start_index : span.end_index] == span.text for span in spans)


def test_annotator_returns_independent_claim_spans_with_exact_offsets() -> None:
    answer = "第一事实。第二事实。"
    outcome, provider = _annotate(
        answer=answer,
        responses=[
            json.dumps(
                {
                    "annotations": [
                        {"span_id": "span_0001", "block_ids": ["block_1"]},
                        {"span_id": "span_0002", "block_ids": ["block_1", "block_2"]},
                    ]
                }
            )
        ],
    )

    assert outcome.artifact.status == "complete"
    assert outcome.artifact.span_version == CLAIM_SPAN_VERSION
    assert outcome.artifact.batch_count == 1
    assert outcome.artifact.batch_version == CITATION_ANNOTATION_BATCH_VERSION
    assert outcome.artifact.prompt_version == CITATION_ANNOTATION_PROMPT_VERSION
    assert provider.max_output_requests == [citation_annotation_output_token_cap(2)]
    assert outcome.model_calls == 1
    assert [item.text for item in outcome.artifact.annotations] == [
        "第一事实。",
        "第二事实。",
    ]
    assert [item.block_ids for item in outcome.artifact.annotations] == [
        ["block_1"],
        ["block_1", "block_2"],
    ]
    assert all(
        answer[item.start_index : item.end_index] == item.text
        for item in outcome.artifact.annotations
    )
    annotation_request = json.loads(provider.requests[0][1].content)
    assert [item["span_id"] for item in annotation_request["spans"]] == [
        "span_0001",
        "span_0002",
    ]
    assert "answer" in annotation_request


def test_annotation_prompt_rejects_underinformative_claim_fragments() -> None:
    assert CITATION_ANNOTATION_PROMPT_VERSION == "claim-citation-v2"
    assert "完整、独立、可核验主张" in CITATION_ANNOTATION_SYSTEM_PROMPT
    assert "单独的确认词" in CITATION_ANNOTATION_SYSTEM_PROMPT


def test_long_answer_annotations_are_batched_and_merged_in_answer_order() -> None:
    answer = "".join(f"事实{index}成立。" for index in range(224))
    evidence_blocks = [
        CitationEvidenceBlock(
            block_id=f"block_{index:02d}",
            doc_id="doc_1",
            doc_title="测试文档",
            source_label="local",
            section_path=["边界"],
            text=f"{index}:" + "长证据" * 475,
        )
        for index in range(86)
    ]
    provider = _BatchingAnnotationProvider()

    outcome = asyncio.run(
        CitationAnnotator(
            provider,
            token_counter=ConservativeObservationTokenCounter(),
        ).annotate(
            question="请罗列事实",
            answer=answer,
            evidence_blocks=evidence_blocks,
            spent_input_tokens=199_500,
            spent_output_tokens=14_093,
            max_context_tokens=100_000,
            max_request_tokens=400_000,
        )
    )

    assert outcome.artifact.status == "complete"
    assert outcome.artifact.batch_count == 4
    assert provider.span_counts == [224, 224, 224, 224]
    assert provider.evidence_counts == [24, 24, 24, 14]
    assert provider.max_output_tokens == [citation_annotation_output_token_cap(224)] * 4
    assert outcome.model_calls == 4
    assert len(outcome.raw_outputs) == 4
    assert len(outcome.messages) == 8
    assert len(outcome.artifact.annotations) == 224
    assert [item.annotation_id for item in outcome.artifact.annotations] == [
        f"annotation_{index:04d}" for index in range(1, 225)
    ]
    assert [item.span_id for item in outcome.artifact.annotations] == [
        f"span_{index:04d}" for index in range(1, 225)
    ]
    assert outcome.artifact.annotations[0].block_ids == [
        "block_00",
        "block_24",
        "block_48",
        "block_72",
    ]


def test_annotation_output_length_is_reported_as_truncation() -> None:
    provider = _TruncatedAnnotationProvider()

    outcome = asyncio.run(
        CitationAnnotator(
            provider,
            token_counter=ConservativeObservationTokenCounter(),
        ).annotate(
            question="问题",
            answer="唯一事实。",
            evidence_blocks=_evidence(),
            spent_input_tokens=10,
            spent_output_tokens=5,
            max_context_tokens=100_000,
            max_request_tokens=100_000,
        )
    )

    assert outcome.artifact.status == "pending"
    assert outcome.artifact.error_code == "citation_annotation_output_truncated"
    assert outcome.model_calls == 2
    assert provider.calls == 2


@pytest.mark.parametrize(
    ("selection", "expected_code"),
    [
        (
            {"span_id": "span_0001", "block_ids": ["block_outside_attempt"]},
            "citation_annotation_source_invalid",
        ),
        (
            {"span_id": "span_missing", "block_ids": ["block_1"]},
            "citation_annotation_span_invalid",
        ),
        (
            {"span_id": "span_0001", "block_ids": ["block_1", "block_1"]},
            "citation_annotation_source_duplicate",
        ),
    ],
)
def test_invalid_annotation_becomes_pending_without_changing_answer(
    selection: dict[str, object],
    expected_code: str,
) -> None:
    response = json.dumps({"annotations": [selection]})
    outcome, provider = _annotate(
        answer="唯一事实。",
        responses=[response, response],
    )

    assert outcome.artifact.status == "pending"
    assert outcome.artifact.error_code == expected_code
    assert outcome.artifact.annotations == []
    assert outcome.model_calls == 2
    assert len(provider.requests) == 2


def test_duplicate_annotation_span_is_rejected() -> None:
    response = json.dumps(
        {
            "annotations": [
                {"span_id": "span_0001", "block_ids": ["block_1"]},
                {"span_id": "span_0001", "block_ids": ["block_2"]},
            ]
        }
    )
    outcome, _provider = _annotate(
        answer="唯一事实。",
        responses=[response, response],
    )

    assert outcome.artifact.status == "pending"
    assert outcome.artifact.error_code == "citation_annotation_duplicate_span"
    assert outcome.artifact.annotations == []


def test_annotation_without_evidence_is_not_applicable_and_skips_model() -> None:
    outcome, provider = _annotate(
        answer="模型补充内容。",
        responses=["unused"],
        evidence_blocks=[],
    )

    assert outcome.artifact.status == "not_applicable"
    assert outcome.model_calls == 0
    assert provider.requests == []


def test_legacy_inline_marker_in_new_answer_becomes_pending() -> None:
    outcome, provider = _annotate(
        answer="答案。【来源：block_1】",
        responses=["unused"],
    )

    assert outcome.artifact.status == "pending"
    assert outcome.artifact.error_code == "citation_annotation_inline_marker_forbidden"
    assert outcome.model_calls == 0
    assert provider.requests == []


def test_annotation_context_budget_exceeded_skips_model_and_keeps_pending() -> None:
    outcome, provider = _annotate(
        answer="唯一事实。",
        responses=["unused"],
        max_context_tokens=1,
        max_request_tokens=1,
    )

    assert outcome.artifact.status == "pending"
    assert outcome.artifact.error_code == "citation_annotation_context_exceeded"
    assert outcome.model_calls == 0
    assert provider.requests == []


def test_annotation_provider_failure_is_retried_once_then_pending() -> None:
    outcome = asyncio.run(
        CitationAnnotator(
            _FailingProvider(),
            token_counter=ConservativeObservationTokenCounter(),
        ).annotate(
            question="问题",
            answer="唯一事实。",
            evidence_blocks=_evidence(),
            spent_input_tokens=10,
            spent_output_tokens=5,
            max_context_tokens=100_000,
            max_request_tokens=400_000,
        )
    )

    assert outcome.artifact.status == "pending"
    assert outcome.artifact.error_code == "citation_annotation_model_failed"
    assert outcome.model_calls == 2
    assert outcome.input_tokens > 0
    assert outcome.artifact.annotations == []


def test_retry_that_would_exceed_request_budget_stops_before_second_call() -> None:
    answer = "唯一事实。"
    packet = CitationAnnotationInput(
        question="MCP 的边界是什么？",
        answer=answer,
        spans=split_answer_spans(answer),
        evidence_blocks=_evidence(),
    )
    counter = ConservativeObservationTokenCounter()
    estimated_input_tokens = sum(
        counter.count(message.content) for message in render_citation_annotation_messages(packet)
    )
    outcome, provider = _annotate(
        answer=answer,
        responses=["not-json", "unused"],
        max_request_tokens=(15 + estimated_input_tokens + citation_annotation_output_token_cap(1)),
    )

    assert outcome.artifact.status == "pending"
    assert outcome.artifact.error_code == "citation_annotation_request_budget_exceeded"
    assert outcome.model_calls == 1
    assert len(provider.requests) == 1


def _trace_with_annotation(
    root: Path,
    *,
    answer: str,
    annotation_artifact: CitationAnnotationArtifact | None,
) -> _MemoryTraceCatalog:
    payload_path = root / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "blocks": [
                    {
                        "block_id": "block_1",
                        "doc_id": "doc_1",
                        "doc_title": "测试文档",
                        "source_label": "local",
                        "section_path": ["边界"],
                        "text": "第二事实来自原始证据。",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    payload_artifact = _artifact_record("a", "tool.payload", payload_path)
    artifacts = [payload_artifact]
    files = {payload_artifact.artifact_id: payload_path}
    if annotation_artifact is not None:
        annotation_path = root / "annotations.json"
        annotation_path.write_text(
            annotation_artifact.model_dump_json(),
            encoding="utf-8",
        )
        annotation_record = _artifact_record("b", "citations", annotation_path)
        artifacts.append(annotation_record)
        files[annotation_record.artifact_id] = annotation_path

    started_at = "2026-10-25T00:00:00+00:00"
    trace = TraceDetailRecord(
        manifest=TraceManifestRecord(
            trace_id="c" * 32,
            mode="full",
            app_version="0.1.0",
            started_at=started_at,
            ended_at="2026-10-25T00:00:02+00:00",
            duration_ms=2000,
            status="succeeded",
            termination_reason="finish",
            answer_completeness="complete",
            question_preview="问题",
            snapshot_id="s_0123456789abcdef",
            snapshot_status="ready",
            completeness="complete",
            agent_turns=2,
            tool_calls=1,
            model_calls=3,
            retry_count=0,
            input_tokens=10,
            output_tokens=5,
            artifacts=artifacts,
            replay_scope="retrieval",
        ),
        spans=[
            TraceSpanRecord(
                trace_id="c" * 32,
                span_id="d" * 16,
                parent_span_id=None,
                name="tool",
                kind="TOOL",
                start_time_unix_nano=1,
                end_time_unix_nano=2,
                started_at=started_at,
                ended_at="2026-10-25T00:00:01+00:00",
                status="ok",
                attributes={"app.payload_artifact": payload_artifact.model_dump(mode="json")},
            )
        ],
    )
    return _MemoryTraceCatalog(trace, files)


def _artifact_record(
    prefix: str,
    kind: str,
    path: Path,
) -> TraceArtifactRecord:
    return TraceArtifactRecord(
        artifact_id=prefix * 64,
        kind=kind,
        mime_type="application/json",
        size_bytes=path.stat().st_size,
        content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
        state="stored",
        relative_path=f"artifacts/{path.name}",
    )


def test_duplicate_block_prefers_full_text_over_search_snippet(tmp_path: Path) -> None:
    snippet_path = tmp_path / "snippet.json"
    full_path = tmp_path / "full.json"
    snippet_path.write_text(
        json.dumps(
            {
                "matches": [
                    {
                        "block_id": "block_1",
                        "doc_id": "doc_1",
                        "doc_title": "测试文档",
                        "source_label": "local",
                        "section_path": ["边界"],
                        "snippet": "… 在容器与宿主内核之间增加隔离层 …",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    full_path.write_text(
        json.dumps(
            {
                "blocks": [
                    {
                        "block_id": "block_1",
                        "doc_id": "doc_1",
                        "doc_title": "测试文档",
                        "source_label": "local",
                        "section_path": ["边界"],
                        "text": (
                            "gVisor 是一种容器沙箱运行时，在容器与宿主内核之间增加隔离层。"
                            "后续完整上下文仍在同一个 block 中。"
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    snippet_artifact = _artifact_record("a", "tool.payload", snippet_path)
    full_artifact = _artifact_record("f", "tool.payload", full_path)
    started_at = "2026-10-25T00:00:00+00:00"
    trace = TraceDetailRecord(
        manifest=TraceManifestRecord(
            trace_id="e" * 32,
            mode="full",
            app_version="0.1.0",
            started_at=started_at,
            ended_at="2026-10-25T00:00:02+00:00",
            duration_ms=2000,
            status="succeeded",
            termination_reason="finish",
            answer_completeness="complete",
            question_preview="问题",
            snapshot_id="s_0123456789abcdef",
            snapshot_status="ready",
            completeness="complete",
            agent_turns=2,
            tool_calls=2,
            model_calls=3,
            retry_count=0,
            input_tokens=10,
            output_tokens=5,
            artifacts=[snippet_artifact, full_artifact],
            replay_scope="retrieval",
        ),
        spans=[
            TraceSpanRecord(
                trace_id="e" * 32,
                span_id="1" * 16,
                parent_span_id=None,
                name="tool",
                kind="TOOL",
                start_time_unix_nano=1,
                end_time_unix_nano=2,
                started_at=started_at,
                ended_at="2026-10-25T00:00:01+00:00",
                status="ok",
                attributes={"app.payload_artifact": snippet_artifact.model_dump(mode="json")},
            ),
            TraceSpanRecord(
                trace_id="e" * 32,
                span_id="2" * 16,
                parent_span_id=None,
                name="tool",
                kind="TOOL",
                start_time_unix_nano=3,
                end_time_unix_nano=4,
                started_at=started_at,
                ended_at="2026-10-25T00:00:02+00:00",
                status="ok",
                attributes={"app.payload_artifact": full_artifact.model_dump(mode="json")},
            ),
        ],
    )
    catalog = _MemoryTraceCatalog(
        trace,
        {
            snippet_artifact.artifact_id: snippet_path,
            full_artifact.artifact_id: full_path,
        },
    )

    evidence = _extract_evidence(trace, catalog, ["block_1"])  # type: ignore[arg-type]

    assert len(evidence) == 1
    assert evidence[0].text.startswith("gVisor 是一种容器沙箱运行时")
    assert "后续完整上下文仍在同一个 block 中" in evidence[0].text


def test_structured_annotation_is_the_exact_citation_judge_claim_context(
    tmp_path: Path,
) -> None:
    answer = "第一事实。第二事实。"
    annotation = CitationAnnotation(
        annotation_id="annotation_0001",
        span_id="span_0002",
        kind="sentence",
        start_index=5,
        end_index=10,
        text="第二事实。",
        block_ids=["block_1"],
    )
    catalog = _trace_with_annotation(
        tmp_path,
        answer=answer,
        annotation_artifact=CitationAnnotationArtifact(
            status="complete",
            model_id="fake-annotation",
            span_count=2,
            evidence_count=1,
            annotations=[annotation],
        ),
    )

    built = build_judge_input(
        catalog=catalog,  # type: ignore[arg-type]
        trace_id="c" * 32,
        trace_status="complete",
        answer=answer,
    )
    task = _build_citation_task(
        attempt_id="attempt_1",
        answer=answer,
        citations=[
            CitationEvidenceInput(
                block_id="block_1",
                doc_title="测试文档",
                section_path=["边界"],
                text="第二事实来自原始证据。",
            )
        ],
        annotations=built.citation_annotations,
    )

    assert built.checker_payload.cited_block_ids == ["block_1"]
    assert task is not None
    assert task.occurrences[0].block_ids == ["block_1"]
    assert task.batches[0].contexts[0].context_id == "annotation_0001"
    assert task.batches[0].contexts[0].kind == "claim"
    assert task.batches[0].contexts[0].text == "第二事实。"


def test_complete_empty_annotation_does_not_fall_back_to_inline_marker(
    tmp_path: Path,
) -> None:
    answer = "答案。【来源：block_1】"
    catalog = _trace_with_annotation(
        tmp_path,
        answer=answer,
        annotation_artifact=CitationAnnotationArtifact(
            status="complete",
            model_id="fake-annotation",
            span_count=1,
            evidence_count=1,
        ),
    )

    built = build_judge_input(
        catalog=catalog,  # type: ignore[arg-type]
        trace_id="c" * 32,
        trace_status="complete",
        answer=answer,
    )

    assert built.checker_payload.cited_block_ids == []
    assert built.citation_annotations == []


def test_pending_annotation_keeps_citation_check_pending(tmp_path: Path) -> None:
    catalog = _trace_with_annotation(
        tmp_path,
        answer="答案。",
        annotation_artifact=CitationAnnotationArtifact(
            status="pending",
            model_id="fake-annotation",
            span_count=1,
            evidence_count=1,
            error_code="citation_annotation_model_failed",
            error_message="upstream unavailable",
        ),
    )

    generation_input = build_generation_judge_input(
        catalog=catalog,  # type: ignore[arg-type]
        trace_id="c" * 32,
        trace_status="complete",
        answer="答案。",
    )
    with pytest.raises(JudgeInputError) as error:
        build_judge_input(
            catalog=catalog,  # type: ignore[arg-type]
            trace_id="c" * 32,
            trace_status="complete",
            answer="答案。",
        )

    assert generation_input.checker_payload.generation_state.answer_completeness == "complete"
    assert error.value.code == "citation_annotation_model_failed"


def test_legacy_artifact_without_span_version_is_marked_unknown(tmp_path: Path) -> None:
    payload = {
        "schema_version": "citation-annotation-v1",
        "status": "complete",
        "prompt_version": "claim-citation-v1",
        "prompt_hash": "a" * 64,
        "model_id": "fake-annotation",
        "span_count": 0,
        "evidence_count": 0,
        "annotations": [],
    }

    artifact = CitationAnnotationArtifact.model_validate(payload)

    assert artifact.span_version == LEGACY_CLAIM_SPAN_VERSION
    assert artifact.batch_version == "legacy-unknown"


def test_historical_trace_without_annotation_artifact_uses_inline_fallback(
    tmp_path: Path,
) -> None:
    answer = "答案。【来源：block_1】"
    catalog = _trace_with_annotation(
        tmp_path,
        answer=answer,
        annotation_artifact=None,
    )

    built = build_judge_input(
        catalog=catalog,  # type: ignore[arg-type]
        trace_id="c" * 32,
        trace_status="complete",
        answer=answer,
    )

    assert built.checker_payload.cited_block_ids == ["block_1"]
    assert built.citation_annotations == []
