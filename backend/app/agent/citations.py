from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.citation_annotation_prompt import (
    CITATION_ANNOTATION_PROMPT_HASH,
    CITATION_ANNOTATION_PROMPT_VERSION,
    render_citation_annotation_messages,
)
from app.agent.token_counter import ObservationTokenCounter
from app.providers.chat import ChatMessage, ChatProvider, ChatProviderError

CITATION_ANNOTATION_SCHEMA_VERSION = "citation-annotation-v1"
CLAIM_SPAN_VERSION = "claim-spans-v2"
LEGACY_CLAIM_SPAN_VERSION = "legacy-unknown"
MAX_CITATION_ANNOTATION_OUTPUT_TOKENS = 4_096
MAX_CITATION_ANNOTATION_ATTEMPTS = 2

CitationSpanKind = Literal["sentence", "list_item", "table_row", "line"]


class CitationSpan(BaseModel):
    """答案中可由引用标注器选择的稳定文本范围。"""

    model_config = ConfigDict(extra="forbid")

    span_id: str
    kind: CitationSpanKind
    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)
    text: str = Field(min_length=1)


class CitationEvidenceBlock(BaseModel):
    """本次 attempt 中真实工具返回、允许被引用的完整证据块。"""

    model_config = ConfigDict(extra="forbid")

    block_id: str
    doc_id: str
    doc_title: str
    source_label: str = ""
    section_path: list[str] = Field(default_factory=list)
    text: str = Field(min_length=1)


class CitationAnnotationInput(BaseModel):
    """后置引用标注器可见的全部输入。"""

    model_config = ConfigDict(extra="forbid")

    question: str
    answer: str
    spans: list[CitationSpan]
    evidence_blocks: list[CitationEvidenceBlock]


class CitationAnnotation(BaseModel):
    """一个答案 claim span 与一组支持证据的结构化绑定。"""

    model_config = ConfigDict(extra="forbid")

    annotation_id: str
    span_id: str
    kind: CitationSpanKind
    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)
    text: str = Field(min_length=1)
    block_ids: list[str] = Field(min_length=1)


class CitationAnnotationArtifact(BaseModel):
    """可随 trace 持久化的引用标注事实与调用审计摘要。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = CITATION_ANNOTATION_SCHEMA_VERSION
    span_version: str = LEGACY_CLAIM_SPAN_VERSION
    status: Literal["complete", "pending", "not_applicable"]
    prompt_version: str = CITATION_ANNOTATION_PROMPT_VERSION
    prompt_hash: str = CITATION_ANNOTATION_PROMPT_HASH
    model_id: str
    span_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    annotations: list[CitationAnnotation] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CitationAnnotationOutcome:
    """一次后置引用标注的 artifact 与实际模型消耗。"""

    artifact: CitationAnnotationArtifact
    model_calls: int
    input_tokens: int
    output_tokens: int
    messages: tuple[ChatMessage, ...] = ()
    raw_outputs: tuple[str, ...] = ()


class _AnnotationSelection(BaseModel):
    """模型可返回的单个 span 选择。"""

    model_config = ConfigDict(extra="forbid")

    span_id: str
    block_ids: list[str] = Field(min_length=1)


class _AnnotationSelectionOutput(BaseModel):
    """后置引用标注器的严格输出根对象。"""

    model_config = ConfigDict(extra="forbid")

    annotations: list[_AnnotationSelection]


class CitationAnnotationError(ValueError):
    """引用标注无法安全完成时的稳定协议错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CitationAnnotator:
    """用独立模型调用为不可变答案选择稳定 span 和 evidence block。"""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        token_counter: ObservationTokenCounter,
    ) -> None:
        self._provider = provider
        self._token_counter = token_counter

    async def annotate(
        self,
        *,
        question: str,
        answer: str,
        evidence_blocks: list[CitationEvidenceBlock],
        spent_input_tokens: int,
        spent_output_tokens: int,
        max_context_tokens: int,
        max_request_tokens: int,
        max_output_tokens: int,
    ) -> CitationAnnotationOutcome:
        """生成后置 citation annotations；正文只读，失败时返回 pending artifact。"""

        spans = split_answer_spans(answer)
        model_id = self._provider.profile.model_id
        if "\u3010\u6765\u6e90\uff1a" in answer:
            return self._pending_outcome(
                model_id=model_id,
                spans=spans,
                evidence_blocks=evidence_blocks,
                code="citation_annotation_inline_marker_forbidden",
                message="answer contains a legacy inline citation marker",
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
            )
        if not spans or not evidence_blocks:
            return self._outcome(
                status="not_applicable",
                model_id=model_id,
                spans=spans,
                evidence_blocks=evidence_blocks,
            )

        packet = CitationAnnotationInput(
            question=question,
            answer=answer,
            spans=spans,
            evidence_blocks=evidence_blocks,
        )
        messages = tuple(render_citation_annotation_messages(packet))
        estimated_input_tokens = sum(
            self._token_counter.count(message.content) for message in messages
        )
        output_token_cap = min(max_output_tokens, MAX_CITATION_ANNOTATION_OUTPUT_TOKENS)
        if estimated_input_tokens > max_context_tokens:
            return self._pending_outcome(
                model_id=model_id,
                spans=spans,
                evidence_blocks=evidence_blocks,
                code="citation_annotation_context_exceeded",
                message=(
                    "citation annotation input exceeds max_context_tokens: "
                    f"{estimated_input_tokens}>{max_context_tokens}"
                ),
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
                messages=messages,
            )
        model_calls = 0
        input_tokens = 0
        output_tokens = 0
        last_error: str | None = None
        raw_outputs: list[str] = []
        for attempt in range(1, MAX_CITATION_ANNOTATION_ATTEMPTS + 1):
            if (
                spent_input_tokens
                + spent_output_tokens
                + input_tokens
                + output_tokens
                + estimated_input_tokens
                + output_token_cap
                > max_request_tokens
            ):
                return self._pending_outcome(
                    model_id=model_id,
                    spans=spans,
                    evidence_blocks=evidence_blocks,
                    code="citation_annotation_request_budget_exceeded",
                    message="citation annotation would exceed the attempt request budget",
                    model_calls=model_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    messages=messages,
                    raw_outputs=tuple(raw_outputs),
                )
            try:
                result = await self._provider.complete(
                    messages,
                    max_output_tokens=output_token_cap,
                )
            except ChatProviderError as error:
                model_calls += 1
                input_tokens += estimated_input_tokens
                last_error = error.code
                if not error.retryable or attempt == MAX_CITATION_ANNOTATION_ATTEMPTS:
                    return self._pending_outcome(
                        model_id=model_id,
                        spans=spans,
                        evidence_blocks=evidence_blocks,
                        code="citation_annotation_model_failed",
                        message=error.message,
                        model_calls=model_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        messages=messages,
                        raw_outputs=tuple(raw_outputs),
                    )
                continue

            model_calls += 1
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            raw_outputs.append(result.content)
            try:
                annotations = _parse_annotations(
                    result.content,
                    spans=spans,
                    evidence_blocks=evidence_blocks,
                    answer=answer,
                )
            except CitationAnnotationError as error:
                last_error = error.code
                if attempt == MAX_CITATION_ANNOTATION_ATTEMPTS:
                    return self._pending_outcome(
                        model_id=model_id,
                        spans=spans,
                        evidence_blocks=evidence_blocks,
                        code=error.code,
                        message=str(error),
                        model_calls=model_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        messages=messages,
                        raw_outputs=tuple(raw_outputs),
                    )
                continue

            return self._outcome(
                status="complete",
                model_id=model_id,
                spans=spans,
                evidence_blocks=evidence_blocks,
                annotations=annotations,
                model_calls=model_calls,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                messages=messages,
                raw_outputs=tuple(raw_outputs),
            )

        return self._pending_outcome(
            model_id=model_id,
            spans=spans,
            evidence_blocks=evidence_blocks,
            code=last_error or "citation_annotation_failed",
            message="citation annotation did not produce a valid result",
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            messages=messages,
            raw_outputs=tuple(raw_outputs),
        )

    @staticmethod
    def _outcome(
        *,
        status: Literal["complete", "not_applicable"],
        model_id: str,
        spans: list[CitationSpan],
        evidence_blocks: list[CitationEvidenceBlock],
        annotations: list[CitationAnnotation] | None = None,
        model_calls: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        messages: tuple[ChatMessage, ...] = (),
        raw_outputs: tuple[str, ...] = (),
    ) -> CitationAnnotationOutcome:
        """构造成功或无需标注的 outcome。"""

        return CitationAnnotationOutcome(
            artifact=CitationAnnotationArtifact(
                status=status,
                span_version=CLAIM_SPAN_VERSION,
                model_id=model_id,
                span_count=len(spans),
                evidence_count=len(evidence_blocks),
                annotations=annotations or [],
            ),
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            messages=messages,
            raw_outputs=raw_outputs,
        )

    @staticmethod
    def _pending_outcome(
        *,
        model_id: str,
        spans: list[CitationSpan],
        evidence_blocks: list[CitationEvidenceBlock],
        code: str,
        message: str,
        model_calls: int,
        input_tokens: int,
        output_tokens: int,
        messages: tuple[ChatMessage, ...] = (),
        raw_outputs: tuple[str, ...] = (),
    ) -> CitationAnnotationOutcome:
        """构造保留正文事实的 pending annotation outcome。"""

        return CitationAnnotationOutcome(
            artifact=CitationAnnotationArtifact(
                status="pending",
                span_version=CLAIM_SPAN_VERSION,
                model_id=model_id,
                span_count=len(spans),
                evidence_count=len(evidence_blocks),
                error_code=code,
                error_message=message,
            ),
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            messages=messages,
            raw_outputs=raw_outputs,
        )


def split_answer_spans(answer: str) -> list[CitationSpan]:
    """按 Markdown 结构和句末标点切出稳定、可引用的答案 span。"""

    spans: list[CitationSpan] = []
    paragraph: list[tuple[str, int]] = []
    paragraph_start = 0
    offset = 0
    in_code_fence = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if not paragraph:
            return
        text = "".join(value for value, _start in paragraph)
        _append_text_spans(spans, text, paragraph_start, kind="sentence")
        paragraph = []

    lines = answer.splitlines(keepends=True)
    for line_index, raw_line in enumerate(lines):
        line_start = offset
        offset += len(raw_line)
        stripped = raw_line.strip()
        if stripped.startswith(("```", "~~~")):
            flush_paragraph()
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not stripped:
            flush_paragraph()
            continue
        if _HEADING_PATTERN.match(raw_line) or _TABLE_SEPARATOR_PATTERN.match(raw_line):
            flush_paragraph()
            continue
        if _TABLE_ROW_PATTERN.match(raw_line):
            flush_paragraph()
            if line_index + 1 < len(lines) and _TABLE_SEPARATOR_PATTERN.match(
                lines[line_index + 1]
            ):
                continue
            _append_text_spans(
                spans,
                raw_line,
                line_start,
                kind="table_row",
                split_semicolons=False,
            )
            continue
        if _LIST_ITEM_PATTERN.match(raw_line):
            flush_paragraph()
            _append_text_spans(
                spans,
                raw_line,
                line_start,
                kind="list_item",
                split_semicolons=True,
            )
            continue
        if not paragraph:
            paragraph_start = line_start
        paragraph.append((raw_line, line_start))
    flush_paragraph()
    return [
        span.model_copy(update={"span_id": f"span_{index:04d}"})
        for index, span in enumerate(spans, start=1)
    ]


def _append_text_spans(
    spans: list[CitationSpan],
    text: str,
    base_offset: int,
    *,
    kind: CitationSpanKind,
    split_semicolons: bool = False,
) -> None:
    """在一个 Markdown 语义单元内按句末标点追加有界 span。"""

    for start, end in _claim_ranges(text, split_semicolons=split_semicolons):
        value = text[start:end]
        if not any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in value):
            continue
        spans.append(
            CitationSpan(
                span_id="pending",
                kind=kind,
                start_index=base_offset + start,
                end_index=base_offset + end,
                text=value,
            )
        )


def _claim_ranges(text: str, *, split_semicolons: bool) -> list[tuple[int, int]]:
    """返回去空白后的句子或分号子句范围。"""

    protected_ranges = _inline_code_ranges(text)
    boundaries: list[int] = []
    terminators = "。！？!?"
    for index, char in enumerate(text):
        if _inside_ranges(index, protected_ranges):
            continue
        if char in terminators or split_semicolons and char in ";；":
            boundaries.append(_extend_markdown_boundary(text, index + 1))
        elif char == ".":
            if _ORDERED_LIST_MARKER_PATTERN.match(text[: index + 1]):
                continue
            next_char = text[index + 1] if index + 1 < len(text) else ""
            previous_char = text[index - 1] if index else ""
            if (
                previous_char.isdigit()
                and next_char.isdigit()
                or next_char
                and not next_char.isspace()
                and next_char not in "\"')]}”’"
            ):
                continue
            boundaries.append(_extend_markdown_boundary(text, index + 1))

    if not boundaries or boundaries[-1] < len(text):
        boundaries.append(len(text))

    ranges: list[tuple[int, int]] = []
    cursor = 0
    for boundary in boundaries:
        raw_start, raw_end = cursor, boundary
        while raw_start < raw_end and text[raw_start].isspace():
            raw_start += 1
        while raw_end > raw_start and text[raw_end - 1].isspace():
            raw_end -= 1
        if raw_start < raw_end:
            ranges.append((raw_start, raw_end))
        cursor = boundary
    return ranges


def _inline_code_ranges(text: str) -> list[tuple[int, int]]:
    """返回 Markdown inline code 的字符范围，避免在代码内容内部切 claim。"""

    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        run_start = index
        while index < len(text) and text[index] == "`":
            index += 1
        run_length = index - run_start
        closing = text.find("`" * run_length, index)
        if closing < 0:
            ranges.append((run_start, len(text)))
            break
        close_end = closing + run_length
        ranges.append((run_start, close_end))
        index = close_end
    return ranges


def _inside_ranges(index: int, ranges: list[tuple[int, int]]) -> bool:
    """判断字符是否位于任一左闭右开保护范围内。"""

    return any(start <= index < end for start, end in ranges)


def _extend_markdown_boundary(text: str, boundary: int) -> int:
    """把紧邻句末标点的强调与引号闭合符并入同一个 claim。"""

    while boundary < len(text):
        if text.startswith(("**", "__"), boundary):
            boundary += 2
            continue
        if text[boundary] in "*_\"'”’」』）)]}":
            boundary += 1
            continue
        break
    return boundary


def _parse_annotations(
    raw_output: str,
    *,
    spans: list[CitationSpan],
    evidence_blocks: list[CitationEvidenceBlock],
    answer: str,
) -> list[CitationAnnotation]:
    """严格校验模型 span 选择并派生只读 annotation 对象。"""

    try:
        parsed = _AnnotationSelectionOutput.model_validate_json(raw_output)
    except ValidationError as error:
        raise CitationAnnotationError(
            "citation_annotation_output_invalid",
            "citation annotation output does not match its schema",
        ) from error

    span_by_id = {span.span_id: span for span in spans}
    evidence_ids = {block.block_id for block in evidence_blocks}
    seen_span_ids: set[str] = set()
    annotations: list[CitationAnnotation] = []
    for selection in parsed.annotations:
        if selection.span_id in seen_span_ids:
            raise CitationAnnotationError(
                "citation_annotation_duplicate_span",
                f"duplicate annotation for span: {selection.span_id}",
            )
        seen_span_ids.add(selection.span_id)
        span = span_by_id.get(selection.span_id)
        if span is None:
            raise CitationAnnotationError(
                "citation_annotation_span_invalid",
                f"unknown answer span: {selection.span_id}",
            )
        if len(selection.block_ids) != len(set(selection.block_ids)):
            raise CitationAnnotationError(
                "citation_annotation_source_duplicate",
                f"duplicate evidence block in annotation: {selection.span_id}",
            )
        block_ids = selection.block_ids
        unknown_ids = [block_id for block_id in block_ids if block_id not in evidence_ids]
        if unknown_ids:
            raise CitationAnnotationError(
                "citation_annotation_source_invalid",
                f"annotation references evidence outside this attempt: {unknown_ids}",
            )
        if answer[span.start_index : span.end_index] != span.text:
            raise CitationAnnotationError(
                "citation_annotation_span_invalid",
                f"answer span text diverged: {span.span_id}",
            )
        annotations.append(
            CitationAnnotation(
                annotation_id=f"annotation_{len(annotations) + 1:04d}",
                span_id=span.span_id,
                kind=span.kind,
                start_index=span.start_index,
                end_index=span.end_index,
                text=span.text,
                block_ids=block_ids,
            )
        )
    return sorted(annotations, key=lambda item: (item.start_index, item.end_index))


_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_ROW_PATTERN = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
_ORDERED_LIST_MARKER_PATTERN = re.compile(r"^\s*(?:\*\*|__)?\d+\.$")
