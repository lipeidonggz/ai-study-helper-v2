from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.citation_annotation_prompt import (
    CITATION_ANNOTATION_PROMPT_HASH,
    CITATION_ANNOTATION_PROMPT_VERSION,
    DEFAULT_CITATION_ANNOTATION_PROMPT,
    CitationAnnotationPrompt,
    citation_evidence_reference,
    render_citation_annotation_messages,
)
from app.agent.citation_source_meta import CitationSourceMetaArtifact
from app.agent.token_counter import ObservationTokenCounter
from app.providers.chat import ChatMessage, ChatProvider, ChatProviderError

CITATION_ANNOTATION_SCHEMA_VERSION = "citation-annotation-v4"
CLAIM_SPAN_VERSION = "claim-spans-v4"
LEGACY_CLAIM_SPAN_VERSION = "legacy-unknown"
CITATION_ANNOTATION_BATCH_VERSION = "evidence-blocks-v2-span-split"
LEGACY_CITATION_ANNOTATION_BATCH_VERSION = "legacy-unknown"
MAX_CITATION_ANNOTATION_OUTPUT_TOKENS = 4_096
# 固定 schema 开销与单条 span→block_ids 关系的保守 token 估计；不读取用户回答预算。
MIN_CITATION_ANNOTATION_OUTPUT_TOKENS = 512
CITATION_ANNOTATION_OUTPUT_TOKENS_PER_SPAN = 128
MAX_CITATION_ANNOTATION_ATTEMPTS = 2
# 24 个 long evidence blocks 可让 cf-003 的 86 块在 4 批内完成，并保留请求预算余量。
MAX_CITATION_ANNOTATION_EVIDENCE_BLOCKS_PER_BATCH = 24

CitationSpanKind = Literal["sentence", "list_item", "table_row", "line"]
CitationSpanRole = Literal[
    "body_sentence",
    "body_list_item",
    "body_table_row",
    "body_line",
]
CitationContextKind = Literal["paragraph", "list", "table"]


class CitationSpan(BaseModel):
    """答案中可由引用标注器选择的稳定文本范围。"""

    model_config = ConfigDict(extra="forbid")

    span_id: str
    kind: CitationSpanKind
    structure_role: CitationSpanRole = "body_sentence"
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


class CitationContextFragment(BaseModel):
    """Harness 从答案原文中确定性截取、仅供判官解释 focus claim 的上下文。"""

    model_config = ConfigDict(extra="forbid")

    fragment_id: str
    kind: CitationContextKind
    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)
    text: str = Field(min_length=1)


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
    reason: str = ""
    context_fragments: list[CitationContextFragment] = Field(default_factory=list)


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
    batch_count: int = Field(default=0, ge=0)
    batch_version: str = LEGACY_CITATION_ANNOTATION_BATCH_VERSION
    annotations: list[CitationAnnotation] = Field(default_factory=list)
    source_meta: CitationSourceMetaArtifact = Field(
        default_factory=CitationSourceMetaArtifact,
    )
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
    reason: str = ""


class _AnnotationSelectionOutput(BaseModel):
    """后置引用标注器的严格输出根对象。"""

    model_config = ConfigDict(extra="forbid")

    annotations: list[_AnnotationSelection]


@dataclass(slots=True)
class _AnnotationCallState:
    """一次 annotation 调用树共享的审计计数。"""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    messages: list[ChatMessage] = field(default_factory=list)
    raw_outputs: list[str] = field(default_factory=list)


class CitationAnnotationError(ValueError):
    """引用标注无法安全完成时的稳定协议错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def citation_annotation_output_token_cap(span_count: int) -> int:
    """按输出 schema 的最大 annotation 数推导独立输出预算。"""

    estimated = MIN_CITATION_ANNOTATION_OUTPUT_TOKENS + (
        span_count * CITATION_ANNOTATION_OUTPUT_TOKENS_PER_SPAN
    )
    return min(MAX_CITATION_ANNOTATION_OUTPUT_TOKENS, estimated)


class CitationAnnotator:
    """用独立模型调用为不可变答案选择稳定 span 和 evidence block。"""

    def __init__(
        self,
        provider: ChatProvider,
        *,
        token_counter: ObservationTokenCounter,
        prompt: CitationAnnotationPrompt = DEFAULT_CITATION_ANNOTATION_PROMPT,
    ) -> None:
        self._provider = provider
        self._token_counter = token_counter
        self._prompt = prompt

    async def annotate(
        self,
        *,
        question: str,
        answer: str,
        evidence_blocks: list[CitationEvidenceBlock],
        spans: list[CitationSpan] | None = None,
        spent_input_tokens: int,
        spent_output_tokens: int,
        max_context_tokens: int,
        max_request_tokens: int,
    ) -> CitationAnnotationOutcome:
        """生成后置 citation annotations；正文只读，失败时返回 pending artifact。"""

        resolved_spans = split_answer_spans(answer) if spans is None else spans
        model_id = self._provider.profile.model_id
        if "\u3010\u6765\u6e90\uff1a" in answer:
            return self._pending_outcome(
                model_id=model_id,
                spans=resolved_spans,
                evidence_blocks=evidence_blocks,
                code="citation_annotation_inline_marker_forbidden",
                message="answer contains a legacy inline citation marker",
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
            )
        if not resolved_spans or not evidence_blocks:
            return self._outcome(
                status="not_applicable",
                model_id=model_id,
                spans=resolved_spans,
                evidence_blocks=evidence_blocks,
            )

        evidence_batches = [
            evidence_blocks[index : index + MAX_CITATION_ANNOTATION_EVIDENCE_BLOCKS_PER_BATCH]
            for index in range(
                0,
                len(evidence_blocks),
                MAX_CITATION_ANNOTATION_EVIDENCE_BLOCKS_PER_BATCH,
            )
        ]
        call_state = _AnnotationCallState()
        annotations: list[CitationAnnotation] = []
        for batch_index, evidence_batch in enumerate(evidence_batches, start=1):
            try:
                batch_annotations = await self._annotate_span_group(
                    question=question,
                    answer=answer,
                    spans=resolved_spans,
                    evidence_blocks=evidence_batch,
                    batch_index=batch_index,
                    batch_count=len(evidence_batches),
                    spent_input_tokens=spent_input_tokens,
                    spent_output_tokens=spent_output_tokens,
                    max_context_tokens=max_context_tokens,
                    max_request_tokens=max_request_tokens,
                    state=call_state,
                )
            except CitationAnnotationError as error:
                return self._pending_outcome(
                    model_id=model_id,
                    spans=resolved_spans,
                    evidence_blocks=evidence_blocks,
                    code=error.code,
                    message=str(error),
                    model_calls=call_state.model_calls,
                    input_tokens=call_state.input_tokens,
                    output_tokens=call_state.output_tokens,
                    messages=tuple(call_state.messages),
                    raw_outputs=tuple(call_state.raw_outputs),
                    batch_count=len(evidence_batches),
                )
            annotations.extend(batch_annotations)

        span_by_id = {span.span_id: span for span in resolved_spans}
        merged_block_ids: dict[str, list[str]] = {}
        merged_reasons: dict[str, list[str]] = {}
        for annotation in annotations:
            block_ids = merged_block_ids.setdefault(annotation.span_id, [])
            for block_id in annotation.block_ids:
                if block_id not in block_ids:
                    block_ids.append(block_id)
            reasons = merged_reasons.setdefault(annotation.span_id, [])
            if annotation.reason and annotation.reason not in reasons:
                reasons.append(annotation.reason)
        annotations = [
            CitationAnnotation(
                annotation_id="pending",
                span_id=span.span_id,
                kind=span.kind,
                start_index=span.start_index,
                end_index=span.end_index,
                text=span.text,
                block_ids=merged_block_ids[span.span_id],
                reason="；".join(merged_reasons.get(span.span_id, [])),
                context_fragments=_context_fragments_for_span(answer, span),
            )
            for span_id, _block_ids in merged_block_ids.items()
            if (span := span_by_id.get(span_id)) is not None
        ]
        annotations.sort(key=lambda item: (item.start_index, item.end_index))
        annotations = [
            annotation.model_copy(update={"annotation_id": f"annotation_{index:04d}"})
            for index, annotation in enumerate(annotations, start=1)
        ]
        return self._outcome(
            status="complete",
            model_id=model_id,
            spans=resolved_spans,
            evidence_blocks=evidence_blocks,
            annotations=annotations,
            model_calls=call_state.model_calls,
            input_tokens=call_state.input_tokens,
            output_tokens=call_state.output_tokens,
            messages=tuple(call_state.messages),
            raw_outputs=tuple(call_state.raw_outputs),
            batch_count=len(evidence_batches),
        )

    async def _annotate_span_group(
        self,
        *,
        question: str,
        answer: str,
        spans: list[CitationSpan],
        evidence_blocks: list[CitationEvidenceBlock],
        batch_index: int,
        batch_count: int,
        spent_input_tokens: int,
        spent_output_tokens: int,
        max_context_tokens: int,
        max_request_tokens: int,
        state: _AnnotationCallState,
    ) -> list[CitationAnnotation]:
        """标注一个 span 组；输出截断时递归二分，避免整批 pending。"""

        packet = CitationAnnotationInput(
            question=question,
            answer=answer,
            spans=spans,
            evidence_blocks=evidence_blocks,
        )
        messages = tuple(
            render_citation_annotation_messages(
                packet,
                prompt=self._prompt,
            )
        )
        state.messages.extend(messages)
        estimated_input_tokens = sum(
            self._token_counter.count(message.content) for message in messages
        )
        if estimated_input_tokens > max_context_tokens:
            raise CitationAnnotationError(
                "citation_annotation_context_exceeded",
                (
                    "citation annotation input exceeds max_context_tokens: "
                    f"batch {batch_index}/{batch_count}, spans={len(spans)}, "
                    f"{estimated_input_tokens}>{max_context_tokens}"
                ),
            )

        output_token_cap = citation_annotation_output_token_cap(len(spans))
        last_error: str | None = None
        for attempt in range(1, MAX_CITATION_ANNOTATION_ATTEMPTS + 1):
            if (
                spent_input_tokens
                + spent_output_tokens
                + state.input_tokens
                + state.output_tokens
                + estimated_input_tokens
                + output_token_cap
                > max_request_tokens
            ):
                raise CitationAnnotationError(
                    "citation_annotation_request_budget_exceeded",
                    (
                        "citation annotation would exceed the attempt request budget: "
                        f"batch {batch_index}/{batch_count}, spans={len(spans)}"
                    ),
                )
            try:
                result = await self._provider.complete(
                    messages,
                    max_output_tokens=output_token_cap,
                )
            except ChatProviderError as error:
                state.model_calls += 1
                state.input_tokens += estimated_input_tokens
                last_error = error.code
                if not error.retryable or attempt == MAX_CITATION_ANNOTATION_ATTEMPTS:
                    raise CitationAnnotationError(
                        "citation_annotation_model_failed",
                        (f"{error.message}: batch {batch_index}/{batch_count}, spans={len(spans)}"),
                    ) from error
                continue

            state.model_calls += 1
            state.input_tokens += result.input_tokens
            state.output_tokens += result.output_tokens
            state.raw_outputs.append(result.content)
            if result.finish_reason == "length":
                if len(spans) > 1:
                    midpoint = len(spans) // 2
                    left = await self._annotate_span_group(
                        question=question,
                        answer=answer,
                        spans=spans[:midpoint],
                        evidence_blocks=evidence_blocks,
                        batch_index=batch_index,
                        batch_count=batch_count,
                        spent_input_tokens=spent_input_tokens,
                        spent_output_tokens=spent_output_tokens,
                        max_context_tokens=max_context_tokens,
                        max_request_tokens=max_request_tokens,
                        state=state,
                    )
                    right = await self._annotate_span_group(
                        question=question,
                        answer=answer,
                        spans=spans[midpoint:],
                        evidence_blocks=evidence_blocks,
                        batch_index=batch_index,
                        batch_count=batch_count,
                        spent_input_tokens=spent_input_tokens,
                        spent_output_tokens=spent_output_tokens,
                        max_context_tokens=max_context_tokens,
                        max_request_tokens=max_request_tokens,
                        state=state,
                    )
                    return [*left, *right]
                last_error = "citation_annotation_output_truncated"
                if attempt == MAX_CITATION_ANNOTATION_ATTEMPTS:
                    raise CitationAnnotationError(
                        last_error,
                        (
                            "citation annotation output reached the token limit: "
                            f"batch {batch_index}/{batch_count}, span={spans[0].span_id}"
                        ),
                    )
                continue

            try:
                return _parse_annotations(
                    result.content,
                    spans=spans,
                    evidence_blocks=evidence_blocks,
                    answer=answer,
                )
            except CitationAnnotationError as error:
                last_error = error.code
                if attempt == MAX_CITATION_ANNOTATION_ATTEMPTS:
                    raise CitationAnnotationError(
                        error.code,
                        (f"{error}: batch {batch_index}/{batch_count}, spans={len(spans)}"),
                    ) from error

        raise CitationAnnotationError(
            last_error or "citation_annotation_failed",
            f"citation annotation failed: batch {batch_index}/{batch_count}",
        )

    def _outcome(
        self,
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
        batch_count: int = 0,
    ) -> CitationAnnotationOutcome:
        """构造成功或无需标注的 outcome。"""

        return CitationAnnotationOutcome(
            artifact=CitationAnnotationArtifact(
                status=status,
                span_version=CLAIM_SPAN_VERSION,
                model_id=model_id,
                prompt_version=self._prompt.version,
                prompt_hash=self._prompt.content_hash,
                span_count=len(spans),
                evidence_count=len(evidence_blocks),
                batch_count=batch_count,
                batch_version=CITATION_ANNOTATION_BATCH_VERSION,
                annotations=annotations or [],
            ),
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            messages=messages,
            raw_outputs=raw_outputs,
        )

    def _pending_outcome(
        self,
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
        batch_count: int = 0,
    ) -> CitationAnnotationOutcome:
        """构造保留正文事实的 pending annotation outcome。"""

        return CitationAnnotationOutcome(
            artifact=CitationAnnotationArtifact(
                status="pending",
                span_version=CLAIM_SPAN_VERSION,
                model_id=model_id,
                prompt_version=self._prompt.version,
                prompt_hash=self._prompt.content_hash,
                span_count=len(spans),
                evidence_count=len(evidence_blocks),
                batch_count=batch_count,
                batch_version=CITATION_ANNOTATION_BATCH_VERSION,
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
        if _NAVIGATION_LINE_PATTERN.match(raw_line):
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
                structure_role="body_table_row",
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
                structure_role="body_list_item",
                split_semicolons=False,
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


def _context_fragments_for_span(
    answer: str,
    span: CitationSpan,
) -> list[CitationContextFragment]:
    """按 Markdown 容器为 focus span 附加确定性上下文，不调用模型猜测依赖。"""

    lines = _indexed_answer_lines(answer)
    focus_line = next(
        (
            index
            for index, (_start, end, _text, kind) in enumerate(lines)
            if kind != "blank" and span.start_index < end
        ),
        None,
    )
    if focus_line is None:
        return []
    if span.kind == "list_item":
        bounds = _list_context_bounds(answer, lines, focus_line)
        kind: CitationContextKind = "list"
    elif span.kind == "table_row":
        bounds = _table_context_bounds(lines, focus_line)
        kind = "table"
    elif span.kind == "sentence":
        bounds = _paragraph_context_bounds(lines, focus_line)
        kind = "paragraph"
    else:
        return []
    if bounds is None:
        return []
    start, end = _trim_answer_range(answer, *bounds)
    if start >= end:
        return []
    return [
        CitationContextFragment(
            fragment_id=f"context_{kind}_{start:08d}_{end:08d}",
            kind=kind,
            start_index=start,
            end_index=end,
            text=answer[start:end],
        )
    ]


def _indexed_answer_lines(
    answer: str,
) -> list[tuple[int, int, str, Literal["blank", "heading", "list_item", "table", "text"]]]:
    """记录答案每一行的精确范围与结构类型。"""

    lines: list[
        tuple[int, int, str, Literal["blank", "heading", "list_item", "table", "text"]]
    ] = []
    offset = 0
    for raw_line in answer.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        stripped = content.strip()
        if not stripped:
            kind: Literal["blank", "heading", "list_item", "table", "text"] = "blank"
        elif _HEADING_PATTERN.match(content):
            kind = "heading"
        elif _LIST_ITEM_PATTERN.match(content):
            kind = "list_item"
        elif _TABLE_ROW_PATTERN.match(content) or _TABLE_SEPARATOR_PATTERN.match(content):
            kind = "table"
        else:
            kind = "text"
        lines.append((offset, offset + len(content), content, kind))
        offset += len(raw_line)
    if not lines and answer:
        lines.append((0, len(answer), answer, "text"))
    return lines


def _paragraph_context_bounds(
    lines: list[tuple[int, int, str, Literal["blank", "heading", "list_item", "table", "text"]]],
    focus_line: int,
) -> tuple[int, int] | None:
    """返回焦点句子所在连续正文段落的精确范围。"""

    if lines[focus_line][3] != "text":
        return None
    start_line = focus_line
    while start_line > 0 and lines[start_line - 1][3] == "text":
        start_line -= 1
    end_line = focus_line
    while end_line + 1 < len(lines) and lines[end_line + 1][3] == "text":
        end_line += 1
    return lines[start_line][0], lines[end_line][1]


def _list_context_bounds(
    answer: str,
    lines: list[tuple[int, int, str, Literal["blank", "heading", "list_item", "table", "text"]]],
    focus_line: int,
) -> tuple[int, int] | None:
    """返回列表项所属连续列表容器，并在需要时并入紧邻的列表引导段。"""

    block_start = focus_line
    while block_start > 0 and lines[block_start - 1][3] in {"list_item", "text"}:
        block_start -= 1
    block_end = focus_line
    while block_end + 1 < len(lines) and lines[block_end + 1][3] in {"list_item", "text"}:
        block_end += 1
    if not any(lines[index][3] == "list_item" for index in range(block_start, block_end + 1)):
        return None

    first_item = next(
        index for index in range(block_start, block_end + 1) if lines[index][3] == "list_item"
    )
    context_start = lines[block_start][0]
    if block_start < first_item and not _is_adjacent_list_lead_in(
        "".join(line[2] for line in lines[block_start:first_item]),
    ):
        context_start = lines[first_item][0]
        block_start = first_item

    if block_start == first_item:
        candidate_end = block_start
        while candidate_end > 0 and lines[candidate_end - 1][3] == "blank":
            candidate_end -= 1
        lead_start = candidate_end
        while lead_start > 0 and lines[lead_start - 1][3] == "text":
            lead_start -= 1
        if lead_start < candidate_end:
            lead_text = answer[lines[lead_start][0] : lines[candidate_end - 1][1]]
            if _is_adjacent_list_lead_in(lead_text):
                context_start = lines[lead_start][0]

    return context_start, lines[block_end][1]


def _is_adjacent_list_lead_in(text: str) -> bool:
    """判断紧邻文本是否明确承担列表引导作用。"""

    normalized = text.rstrip()
    if normalized.endswith((":", "\uff1a")):
        return True
    return any(
        cue in normalized
        for cue in (
            "\u4ee5\u4e0b",
            "\u5982\u4e0b",
            "\u4e0b\u5217",
            "\u5305\u62ec",
            "\u5206\u522b\u662f",
            "\u5206\u4e3a",
            "\u82e5\u5e72",
            "\u4e09\u4e2a",
            "\u4e09\u9879",
            "\u4e09\u70b9",
            "\u51e0\u4e2a",
            "\u51e0\u9879",
            "\u591a\u4e2a",
        )
    )


def _table_context_bounds(
    lines: list[tuple[int, int, str, Literal["blank", "heading", "list_item", "table", "text"]]],
    focus_line: int,
) -> tuple[int, int] | None:
    """返回表格行所属完整表格容器的精确范围。"""

    if lines[focus_line][3] != "table":
        return None
    start_line = focus_line
    while start_line > 0 and lines[start_line - 1][3] == "table":
        start_line -= 1
    end_line = focus_line
    while end_line + 1 < len(lines) and lines[end_line + 1][3] == "table":
        end_line += 1
    return lines[start_line][0], lines[end_line][1]


def _trim_answer_range(answer: str, start: int, end: int) -> tuple[int, int]:
    """去除结构容器边缘的空白，同时保持片段与答案原文严格对应。"""

    while start < end and answer[start].isspace():
        start += 1
    while end > start and answer[end - 1].isspace():
        end -= 1
    return start, end


def _append_text_spans(
    spans: list[CitationSpan],
    text: str,
    base_offset: int,
    *,
    kind: CitationSpanKind,
    structure_role: CitationSpanRole | None = None,
    split_semicolons: bool = False,
) -> None:
    """在一个 Markdown 语义单元内按句末标点追加有界 span。"""

    for start, end in _claim_ranges(text, split_semicolons=split_semicolons):
        value = text[start:end]
        if not any(char.isalnum() or "\u4e00" <= char <= "\u9fff" for char in value):
            continue
        if _is_heading_like_claim(value):
            continue
        spans.append(
            CitationSpan(
                span_id="pending",
                kind=kind,
                structure_role=structure_role
                or (
                    "body_list_item"
                    if kind == "list_item"
                    else "body_table_row"
                    if kind == "table_row"
                    else "body_line"
                    if kind == "line"
                    else "body_sentence"
                ),
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
    reference_to_block_id = {
        citation_evidence_reference(index): block.block_id
        for index, block in enumerate(evidence_blocks, start=1)
    }
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
        unknown_ids = [
            block_id
            for block_id in selection.block_ids
            if block_id not in reference_to_block_id and block_id not in evidence_ids
        ]
        if unknown_ids:
            raise CitationAnnotationError(
                "citation_annotation_source_invalid",
                f"annotation references evidence outside this attempt: {unknown_ids}",
            )
        block_ids = [
            reference_to_block_id.get(block_id, block_id) for block_id in selection.block_ids
        ]
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
                reason=selection.reason,
            )
        )
    return sorted(annotations, key=lambda item: (item.start_index, item.end_index))


_HEADING_PATTERN = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_ITEM_PATTERN = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_NAVIGATION_LINE_PATTERN = re.compile(r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?\[[^\]]+\]\(#[^)]+\)\s*$")
_BOLD_ONLY_PATTERN = re.compile(
    r"^(?:\*\*(?:(?!\*\*).)+\*\*|__(?:(?!__).)+__)$",
    re.DOTALL,
)
_TABLE_ROW_PATTERN = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR_PATTERN = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*$")
_ORDERED_LIST_MARKER_PATTERN = re.compile(r"^\s*(?:\*\*|__)?\d+\.$")


def _is_heading_like_claim(value: str) -> bool:
    """判断 span 是否只是 Markdown 标题或列表项标题。"""

    text = value.strip().rstrip(" \t。.!！?？:：")
    if _LIST_ITEM_PATTERN.match(text) is not None:
        text = _LIST_ITEM_PATTERN.sub("", text, count=1).strip()
        return _BOLD_ONLY_PATTERN.fullmatch(text) is not None
    if _BOLD_ONLY_PATTERN.fullmatch(text) is None:
        return False
    inner_text = text[2:-2].strip()
    if re.match(r"^\d+[.)]\s+", inner_text):
        return True
    return not inner_text.endswith(("。", "！", "？", ".", "!", "?"))
