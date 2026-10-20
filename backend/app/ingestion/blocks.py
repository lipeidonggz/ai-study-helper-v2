from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from app.ingestion.errors import IngestFailure
from app.ingestion.language import detect_block_language
from app.ingestion.models import (
    BlockRecord,
    BlockSet,
    ComponentVersion,
    ExtractedDocument,
    SectionStats,
    SectionTree,
    SemanticNode,
    TableContext,
)

CHUNKER_VERSION = "1"
ESTIMATOR_VERSION = "cjk-ascii-v1"
TARGET_CONTEXT_TOKENS = 450
TABLE_ATOMIC_MAX = 4000
NODE_SEPARATOR = "\n"

_CJK_PATTERN = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
_LATIN_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")
_SENTENCE_PATTERN = re.compile(r".+?(?:[。！？.!?]+|$)", re.DOTALL)


@dataclass(slots=True)
class _TextSpan:
    text: str
    start: int
    end: int


@dataclass(slots=True)
class _SegmentPlan:
    spans: list[_TextSpan]
    used_character_fallback: bool = False


@dataclass(slots=True)
class _BlockUnit:
    section_path: list[str]
    text: str
    char_start: int
    char_end: int
    source_node_ordinals: list[int]
    first_body_index: int
    last_body_index: int
    mergeable: bool
    table_context: TableContext | None = None
    embedding_hints: dict[str, str] | None = None


def estimate_context_tokens(text: str) -> int:
    """Return a deterministic approximate token count for chunk planning."""

    cjk_count = len(_CJK_PATTERN.findall(text))
    latin_tokens = _LATIN_TOKEN_PATTERN.findall(text)
    latin_count = sum(max(1, math.ceil(len(token) / 4)) for token in latin_tokens)
    punctuation_count = len(re.findall(r"[^\w\s]", text))
    return max(1, cjk_count + latin_count + math.ceil(punctuation_count / 4))


def _line_spans(text: str) -> list[_TextSpan]:
    spans: list[_TextSpan] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        visible = line.rstrip("\r\n")
        spans.append(_TextSpan(visible, offset, offset + len(line)))
        offset += len(line)
    if not spans and text:
        spans.append(_TextSpan(text, 0, len(text)))
    return spans


def _sentence_spans(text: str) -> list[_TextSpan]:
    matches = list(_SENTENCE_PATTERN.finditer(text))
    if not matches:
        return [_TextSpan(text, 0, len(text))]
    spans: list[_TextSpan] = []
    for match in matches:
        value = match.group(0)
        if value:
            spans.append(_TextSpan(value, match.start(), match.end()))
    return spans or [_TextSpan(text, 0, len(text))]


def _max_prefix_within_target(text: str, target: int) -> int:
    """Find the longest prefix whose deterministic estimate stays within target."""

    low = 1
    high = len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_context_tokens(text[:middle]) <= target:
            low = middle
        else:
            high = middle - 1
    return low


def _last_soft_boundary(text: str, limit: int) -> int | None:
    """Return the last whitespace or punctuation boundary before limit."""

    # Avoid tiny fragments caused by an early comma or space. If the target
    # prefix is shorter than this guard, the caller falls back to a character
    # boundary instead.
    minimum = min(32, limit)
    for index in range(limit - 1, minimum - 1, -1):
        character = text[index]
        if character.isspace() or unicodedata.category(character).startswith("P"):
            return index + 1
    return None


def _split_at_character_limit(
    text: str,
    start: int,
    target: int,
    end_limit: int | None = None,
) -> list[_TextSpan]:
    """Split text at deterministic character boundaries without semantic guessing."""

    spans: list[_TextSpan] = []
    cursor = 0
    while cursor < len(text):
        remaining = text[cursor:]
        if estimate_context_tokens(remaining) <= target:
            spans.append(
                _TextSpan(
                    remaining,
                    start + cursor,
                    end_limit if end_limit is not None else start + len(text),
                )
            )
            break
        split_at = cursor + _max_prefix_within_target(remaining, target)
        spans.append(_TextSpan(text[cursor:split_at], start + cursor, start + split_at))
        cursor = split_at
    return spans


def _split_at_soft_boundaries(
    text: str,
    start: int,
    target: int,
    end_limit: int | None = None,
) -> _SegmentPlan:
    """Prefer prose punctuation and whitespace before character fallback."""

    spans: list[_TextSpan] = []
    used_character_fallback = False
    cursor = 0
    while cursor < len(text):
        remaining = text[cursor:]
        if estimate_context_tokens(remaining) <= target:
            spans.append(
                _TextSpan(
                    remaining,
                    start + cursor,
                    end_limit if end_limit is not None else start + len(text),
                )
            )
            break
        prefix_length = _max_prefix_within_target(remaining, target)
        boundary = _last_soft_boundary(remaining, prefix_length)
        if boundary is None:
            used_character_fallback = True
        split_length = boundary if boundary is not None else prefix_length
        split_at = cursor + split_length
        spans.append(_TextSpan(text[cursor:split_at], start + cursor, start + split_at))
        cursor = split_at
    return _SegmentPlan(spans, used_character_fallback)


def _group_spans(
    spans: list[_TextSpan],
    *,
    target: int,
    split_oversized: Callable[[_TextSpan, int], _SegmentPlan],
) -> _SegmentPlan:
    groups: list[_TextSpan] = []
    used_character_fallback = False
    current_start: int | None = None
    current_end: int | None = None
    current_parts: list[str] = []
    for span in spans:
        candidate = "\n".join([*current_parts, span.text])
        if current_parts and estimate_context_tokens(candidate) > target:
            groups.append(
                _TextSpan(
                    "\n".join(current_parts),
                    current_start if current_start is not None else span.start,
                    current_end if current_end is not None else span.end,
                )
            )
            current_parts = []
            current_start = None
            current_end = None
        if estimate_context_tokens(span.text) > target:
            if current_parts:
                groups.append(
                    _TextSpan(
                        "\n".join(current_parts),
                        current_start if current_start is not None else span.start,
                        current_end if current_end is not None else span.end,
                    )
                )
                current_parts = []
                current_start = None
                current_end = None
            split = split_oversized(span, target)
            groups.extend(split.spans)
            used_character_fallback = used_character_fallback or split.used_character_fallback
            continue
        if not current_parts:
            current_start = span.start
        current_parts.append(span.text)
        current_end = span.end
    if current_parts:
        groups.append(
            _TextSpan(
                "\n".join(current_parts),
                current_start if current_start is not None else 0,
                current_end if current_end is not None else 0,
            )
        )
    return _SegmentPlan(groups, used_character_fallback)


def _split_prose_span(span: _TextSpan, target: int) -> _SegmentPlan:
    return _split_at_soft_boundaries(span.text, span.start, target, span.end)


def _split_list_item(span: _TextSpan, target: int) -> _SegmentPlan:
    sentence_spans = [
        _TextSpan(match.group(0), span.start + match.start(), span.start + match.end())
        for match in _SENTENCE_PATTERN.finditer(span.text)
        if match.group(0)
    ]
    return _group_spans(
        sentence_spans or [span],
        target=target,
        split_oversized=_split_prose_span,
    )


def _split_code_line(span: _TextSpan, target: int) -> _SegmentPlan:
    return _SegmentPlan(_split_at_character_limit(span.text, span.start, target, span.end))


def _keep_oversized_row(span: _TextSpan, _target: int) -> _SegmentPlan:
    # A table row is the minimum semantic unit. Exceeding a sizing target must
    # produce an oversized atomic block rather than splitting a cell's text.
    return _SegmentPlan([span])


def _prose_segments(node: SemanticNode, target: int) -> _SegmentPlan:
    if estimate_context_tokens(node.text) <= target:
        return _SegmentPlan([_TextSpan(node.text, 0, len(node.text))])
    return _group_spans(
        _sentence_spans(node.text),
        target=target,
        split_oversized=_split_prose_span,
    )


def _list_segments(node: SemanticNode, target: int) -> _SegmentPlan:
    if estimate_context_tokens(node.text) <= target:
        return _SegmentPlan([_TextSpan(node.text, 0, len(node.text))])
    return _group_spans(
        _line_spans(node.text),
        target=target,
        split_oversized=_split_list_item,
    )


def _code_segments(node: SemanticNode, target: int) -> _SegmentPlan:
    if estimate_context_tokens(node.text) <= target:
        return _SegmentPlan([_TextSpan(node.text, 0, len(node.text))])
    return _group_spans(
        _line_spans(node.text),
        target=target,
        split_oversized=_split_code_line,
    )


def _table_segments(node: SemanticNode) -> _SegmentPlan:
    if estimate_context_tokens(node.text) <= TABLE_ATOMIC_MAX:
        return _SegmentPlan([_TextSpan(node.text, 0, len(node.text))])
    return _group_spans(
        _line_spans(node.text),
        target=TARGET_CONTEXT_TOKENS,
        split_oversized=_keep_oversized_row,
    )


def _node_segments(node: SemanticNode) -> _SegmentPlan:
    if node.kind in {"paragraph", "quote", "figure"}:
        return _prose_segments(node, TARGET_CONTEXT_TOKENS)
    if node.kind == "list":
        return _list_segments(node, TARGET_CONTEXT_TOKENS)
    if node.kind == "code":
        return _code_segments(node, TARGET_CONTEXT_TOKENS)
    if node.kind == "table":
        return _table_segments(node)
    return _SegmentPlan([])


def _canonical_text(
    extracted: ExtractedDocument,
) -> tuple[str, dict[int, tuple[int, int, int, int]], list[int]]:
    body_nodes = [node for node in extracted.nodes if node.kind != "heading"]
    text_parts: list[str] = []
    cursor = 0
    spans: dict[int, tuple[int, int, int, int]] = {}
    for body_index, node in enumerate(body_nodes):
        if text_parts:
            text_parts.append(NODE_SEPARATOR)
            cursor += len(NODE_SEPARATOR)
        start = cursor
        end = start + len(node.text)
        text_parts.append(node.text)
        cursor = end
        spans[node.ordinal] = (start, end, body_index, body_index)
    canonical = "".join(text_parts)

    # A separator belongs to the preceding node so block ranges partition the
    # canonical text without holes while each next block starts at its own text.
    for ordinal, (start, end, body_index, _) in list(spans.items()):
        if body_index < len(body_nodes) - 1:
            spans[ordinal] = (start, end + len(NODE_SEPARATOR), body_index, body_index)
    return canonical, spans, [node.ordinal for node in body_nodes]


def build_block_set(
    *,
    preview_doc_id: str,
    extracted: ExtractedDocument,
    section_tree: SectionTree,
) -> BlockSet:
    """Build complete, non-overlapping evidence blocks for a preview document."""

    canonical, spans, body_ordinals = _canonical_text(extracted)
    nodes = {node.ordinal: node for node in extracted.nodes}
    body_position = {ordinal: index for index, ordinal in enumerate(body_ordinals)}
    warnings = list(section_tree.warnings)
    units: list[_BlockUnit] = []

    for section in section_tree.sections:
        for ordinal in section.own_node_ordinals:
            node = nodes[ordinal]
            node_start, _, body_index, _ = spans[ordinal]
            segment_plan = _node_segments(node)
            segments = segment_plan.spans
            if not segments:
                continue
            if node.kind in {"paragraph", "quote"} and len(segments) > 1:
                warnings.append(
                    "text_boundary_split"
                    if segment_plan.used_character_fallback
                    else "text_semantic_split"
                )
            elif node.kind == "list" and len(segments) > 1:
                warnings.append("list_item_split")
            elif node.kind == "code" and len(segments) > 1:
                warnings.append("code_line_split")
            elif node.kind == "table" and len(segments) > 1:
                warnings.append("table_row_group_split")
            if node.kind == "table":
                data_rows = _line_spans(node.text)[1:]
                if any(estimate_context_tokens(row.text) > TABLE_ATOMIC_MAX for row in data_rows):
                    warnings.append("oversized_atomic_table_row")

            table_headers = node.table_rows[0] if node.kind == "table" and node.table_rows else []
            for segment_index, segment in enumerate(segments):
                absolute_start = node_start + segment.start
                absolute_end = node_start + segment.end
                if segment_index == len(segments) - 1 and body_index < len(body_ordinals) - 1:
                    absolute_end += len(NODE_SEPARATOR)
                table_context = (
                    TableContext(
                        caption=node.table_caption,
                        headers=table_headers,
                        group_index=segment_index + 1,
                        group_count=len(segments),
                    )
                    if node.kind == "table" and len(segments) > 1
                    else None
                )
                units.append(
                    _BlockUnit(
                        section_path=section.path,
                        text=canonical[absolute_start:absolute_end],
                        char_start=absolute_start,
                        char_end=absolute_end,
                        source_node_ordinals=[ordinal],
                        first_body_index=body_index,
                        last_body_index=body_index,
                        mergeable=node.kind not in {"code", "table"},
                        table_context=table_context,
                        embedding_hints=(
                            {"code_language": node.code_language} if node.code_language else None
                        ),
                    )
                )

    merged_units: list[_BlockUnit] = []
    for unit in units:
        if (
            merged_units
            and merged_units[-1].section_path == unit.section_path
            and merged_units[-1].mergeable
            and unit.mergeable
            and unit.first_body_index - merged_units[-1].last_body_index <= 1
        ):
            previous = merged_units[-1]
            candidate_text = canonical[previous.char_start : unit.char_end]
            if estimate_context_tokens(candidate_text) <= TARGET_CONTEXT_TOKENS:
                previous.text = candidate_text
                previous.char_end = unit.char_end
                previous.source_node_ordinals = list(
                    dict.fromkeys([*previous.source_node_ordinals, *unit.source_node_ordinals])
                )
                previous.last_body_index = unit.last_body_index
                if unit.table_context:
                    previous.table_context = unit.table_context
                if unit.embedding_hints:
                    previous.embedding_hints = {
                        **(previous.embedding_hints or {}),
                        **unit.embedding_hints,
                    }
                continue
        merged_units.append(unit)

    blocks: list[BlockRecord] = []
    nonempty_units = [unit for unit in merged_units if unit.text.strip()]
    for block_index, unit in enumerate(nonempty_units, start=1):
        block_id = f"{preview_doc_id}#b{block_index:05d}"
        block_text = unit.text.strip()
        blocks.append(
            BlockRecord(
                block_id=block_id,
                section_path=unit.section_path,
                block_index=block_index,
                language=detect_block_language(block_text, extracted.language),
                text=block_text,
                char_start=unit.char_start,
                char_end=unit.char_end,
                content_hash=hashlib.sha256(block_text.encode("utf-8")).hexdigest(),
                source_node_ordinals=unit.source_node_ordinals,
                table_context=unit.table_context,
                embedding_hints=unit.embedding_hints or {},
                context_token_estimate=estimate_context_tokens(block_text),
                estimator_version=ESTIMATOR_VERSION,
            )
        )

    covered_ordinals = [ordinal for block in blocks for ordinal in block.source_node_ordinals]
    if set(covered_ordinals) != set(body_position):
        raise IngestFailure(
            "block_integrity_failed",
            "block generation did not cover every body node",
            500,
        )
    expected_start = 0
    for block in blocks:
        if block.char_start != expected_start:
            raise IngestFailure(
                "block_integrity_failed",
                "block character ranges contain gaps or overlap",
                500,
            )
        expected_start = block.char_end
    if expected_start != len(canonical):
        raise IngestFailure(
            "block_integrity_failed",
            "block character ranges do not cover canonical body text",
            500,
        )

    section_stats = _section_stats(section_tree, blocks)
    return BlockSet(
        chunker=ComponentVersion(name="semantic-block-chunker", version=CHUNKER_VERSION),
        chunk_sizing={
            "target_context_tokens": TARGET_CONTEXT_TOKENS,
            "table_atomic_max": TABLE_ATOMIC_MAX,
            "node_separator": NODE_SEPARATOR,
            "estimator_version": ESTIMATOR_VERSION,
        },
        canonical_body_text=canonical,
        blocks=blocks,
        warnings=list(dict.fromkeys(warnings)),
        section_stats=section_stats,
    )


def _section_stats(section_tree: SectionTree, blocks: list[BlockRecord]) -> list[SectionStats]:
    stats: list[SectionStats] = []
    for section in section_tree.sections:
        path = tuple(section.path)
        own_blocks = [block for block in blocks if tuple(block.section_path) == path]
        subtree_blocks = [
            block for block in blocks if tuple(block.section_path[: len(path)]) == path
        ]
        first_line = own_blocks[0].text.splitlines()[0] if own_blocks else ""
        if not first_line and subtree_blocks:
            first_line = subtree_blocks[0].text.splitlines()[0]
        stats.append(
            SectionStats(
                section_path=section.path,
                own_blocks=len(own_blocks),
                subtree_blocks=len(subtree_blocks),
                own_context_tokens=sum(block.context_token_estimate for block in own_blocks),
                subtree_context_tokens=sum(
                    block.context_token_estimate for block in subtree_blocks
                ),
                first_line=first_line[:120],
            )
        )
    return stats
