"""高召回、确定性、可版本化的 5.1 候选出现扫描器。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
from collections.abc import Sequence
from dataclasses import dataclass

import jieba
import jieba.posseg as jieba_posseg
from nltk.tokenize import NLTKWordTokenizer

from app.dual_entry_index.models import (
    CandidateGroup,
    CandidateOccurrence,
    ScanBlockStatus,
    ScanContract,
    ScanResult,
    SourceBlock,
)
from app.dual_entry_index.normalization import (
    NORMALIZATION_VERSION,
    canonical_hash,
    deterministic_identifier,
    normalize_lexical_key,
)

SCAN_IMPLEMENTATION_VERSION = "dual-entry-scanner-v3"
CHINESE_POS_MAPPING_VERSION = "jieba-content-pos-v1"
CHINESE_BOUNDARY_VERSION = "chinese-boundary-v1"
ENGLISH_BOUNDARY_VERSION = "english-boundary-v1"
ENGLISH_MODEL_FILTER_VERSION = "english-model-filter-v1"
COMPOUND_IDENTIFIER_VERSION = "compound-identifier-v1"
NOISE_PATTERN_VERSION = "noise-pattern-v1"
SENTENCE_BOUNDARY_VERSION = "sentence-boundary-v1"

_URL_PATTERN = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()\[\]{}]+")
_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_UUID_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f])"
)
_PREFIXED_HASH_PATTERN = re.compile(
    r"(?i)(?<![0-9a-z])(?:sha(?:1|224|256|384|512)[:-])?"
    r"(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-z])"
)
_COMPOUND_IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:"
    r"[A-Za-z][A-Za-z0-9]*(?:[-_.:/][A-Za-z0-9]+)+"
    r"|[A-Z]{2,}[A-Z0-9-]*"
    r"|[A-Za-z]*[a-z][A-Z][A-Za-z0-9]*"
    r"|[A-Za-z]+\d+[A-Za-z0-9-]*"
    r"|\d+[A-Za-z][A-Za-z0-9-]*"
    r")"
    r"(?![A-Za-z0-9_])"
)
_CJK_RUN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_CJK_CHAR_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ASCII_WORD_RUN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’+]*")
_SENTENCE_HARD_BOUNDARY_PATTERN = re.compile(r"[。！？!?；;\n]+")
_ASCII_PERIOD_BOUNDARY_PATTERN = re.compile(r"(?<![A-Za-z0-9])\.(?=\s|$)")

_CHINESE_CONTENT_POS_PREFIXES = ("n", "v", "a", "b", "i", "j", "l", "eng")
_CHINESE_BOUNDARY_WORDS = frozenset(
    {
        "的",
        "了",
        "和",
        "与",
        "及",
        "或",
        "而",
        "但",
        "并",
        "且",
        "在",
        "从",
        "到",
        "对",
        "为",
        "被",
        "把",
        "将",
        "由",
        "于",
        "中",
        "上",
        "下",
        "里",
        "后",
        "前",
        "时",
        "同时",
        "已经",
        "可以",
        "能够",
        "可能",
        "需要",
        "进行",
        "通过",
        "以及",
        "只有",
        "才能",
        "不能",
        "成为",
        "一个",
        "一种",
        "这些",
        "那些",
        "这个",
        "那个",
        "例如",
        "其中",
        "之后",
        "之前",
    }
)
_ENGLISH_BOUNDARY_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "must",
        "not",
        "of",
        "on",
        "or",
        "our",
        "shall",
        "she",
        "should",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)

# 这些词即使已经进入高召回候选，也不具备独立术语资格。过滤只作用于整个候选恰为这些词或
# 英文缩写残片的情况；包含这些词的长短语仍交给模型结合上下文判断，避免把名词短语错误拆掉。
_ENGLISH_MODEL_EXCLUSION_WORDS = frozenset(
    {
        "after",
        "against",
        "almost",
        "also",
        "another",
        "around",
        "before",
        "both",
        "down",
        "each",
        "either",
        "enough",
        "even",
        "every",
        "few",
        "first",
        "here",
        "however",
        "just",
        "less",
        "likely",
        "many",
        "more",
        "most",
        "mostly",
        "much",
        "neither",
        "no",
        "often",
        "once",
        "one",
        "ones",
        "only",
        "other",
        "out",
        "outside",
        "over",
        "quite",
        "rather",
        "roughly",
        "same",
        "second",
        "several",
        "similar",
        "so",
        "some",
        "something",
        "still",
        "such",
        "through",
        "too",
        "two",
        "very",
        "while",
        "without",
        "yet",
    }
)
_ENGLISH_CONTRACTION_SUFFIXES = frozenset({"d", "ll", "re", "s", "t", "ve"})


class ScanContractError(RuntimeError):
    """扫描契约、分词器或原文偏移无法可靠建立时抛出的失败。"""


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    text: str
    is_content: bool


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError as error:
        raise ScanContractError(f"required tokenizer package is missing: {distribution}") from error


def current_scan_contract() -> ScanContract:
    """构造当前实现对应的不可变扫描契约。"""

    jieba_version = _package_version("jieba")
    nltk_version = _package_version("nltk")
    if jieba_version != "0.42.1":
        raise ScanContractError(f"unexpected jieba version: {jieba_version}")
    if nltk_version != "3.10.4":
        raise ScanContractError(f"unexpected nltk version: {nltk_version}")
    try:
        with jieba.get_dict_file() as dictionary_file:
            dictionary_hash = hashlib.sha256(dictionary_file.read()).hexdigest()
    except OSError as error:
        raise ScanContractError("cannot read jieba default dictionary") from error
    payload = {
        "contract_version": "stage51-scan-v2",
        "implementation_version": SCAN_IMPLEMENTATION_VERSION,
        "jieba_version": jieba_version,
        "nltk_version": nltk_version,
        "chinese_dictionary_sha256": dictionary_hash,
        "chinese_pos_mapping_version": CHINESE_POS_MAPPING_VERSION,
        "chinese_boundary_version": CHINESE_BOUNDARY_VERSION,
        "english_boundary_version": ENGLISH_BOUNDARY_VERSION,
        "english_model_filter_version": ENGLISH_MODEL_FILTER_VERSION,
        "compound_identifier_version": COMPOUND_IDENTIFIER_VERSION,
        "noise_pattern_version": NOISE_PATTERN_VERSION,
        "sentence_boundary_version": SENTENCE_BOUNDARY_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "chinese_content_pos_prefixes": list(_CHINESE_CONTENT_POS_PREFIXES),
        "chinese_boundary_words": sorted(_CHINESE_BOUNDARY_WORDS),
        "english_boundary_words": sorted(_ENGLISH_BOUNDARY_WORDS),
        "english_model_exclusion_words": sorted(_ENGLISH_MODEL_EXCLUSION_WORDS),
        "english_contraction_suffixes": sorted(_ENGLISH_CONTRACTION_SUFFIXES),
        "compound_identifier_patterns": [_COMPOUND_IDENTIFIER_PATTERN.pattern],
        "cjk_run_pattern": _CJK_RUN_PATTERN.pattern,
        "cjk_char_pattern": _CJK_CHAR_PATTERN.pattern,
        "ascii_word_run_pattern": _ASCII_WORD_RUN_PATTERN.pattern,
        "noise_patterns": [
            _URL_PATTERN.pattern,
            _EMAIL_PATTERN.pattern,
            _UUID_PATTERN.pattern,
            _PREFIXED_HASH_PATTERN.pattern,
        ],
        "sentence_boundary_patterns": [
            _SENTENCE_HARD_BOUNDARY_PATTERN.pattern,
            _ASCII_PERIOD_BOUNDARY_PATTERN.pattern,
        ],
    }
    return ScanContract(**payload, contract_hash=canonical_hash(payload))


class CandidateScanner:
    """从原文块产生高召回候选出现，不判断术语资格或概念语义。"""

    def __init__(self, contract: ScanContract | None = None) -> None:
        self._contract = contract or current_scan_contract()
        jieba.initialize()
        # 直接使用规则型分词器，明确禁止隐式下载 NLTK 资源。
        self._english_tokenizer = NLTKWordTokenizer()

    @property
    def contract(self) -> ScanContract:
        """返回本次扫描冻结的契约。"""

        return self._contract

    def scan(self, snapshot_id: str, blocks: tuple[SourceBlock, ...]) -> ScanResult:
        """扫描全部原文块，任一契约或偏移错误都使整次扫描失败。"""

        occurrences: list[CandidateOccurrence] = []
        statuses: list[ScanBlockStatus] = []
        excluded_counts: dict[str, int] = {}
        for block in blocks:
            if block.snapshot_id != snapshot_id:
                raise ScanContractError("source block snapshot does not match scan snapshot")
            try:
                block_candidates, block_exclusions = self._scan_block(block)
            except Exception as error:
                statuses.append(
                    ScanBlockStatus(
                        block_id=block.block_id,
                        content_hash=block.content_hash,
                        candidate_count=0,
                        status="failed",
                        error_code=type(error).__name__,
                    )
                )
                raise ScanContractError(f"failed to scan block {block.block_id}") from error
            occurrences.extend(block_candidates)
            for reason, count in block_exclusions.items():
                excluded_counts[reason] = excluded_counts.get(reason, 0) + count
            statuses.append(
                ScanBlockStatus(
                    block_id=block.block_id,
                    content_hash=block.content_hash,
                    candidate_count=len(block_candidates),
                    status="succeeded",
                )
            )
        occurrences.sort(
            key=lambda item: (
                item.block_ordinal,
                item.start,
                item.end,
                item.normalized_key,
            )
        )
        deduplicated: list[CandidateOccurrence] = []
        seen: set[tuple[str, int, int, str]] = set()
        for occurrence in occurrences:
            identity = (
                occurrence.block_id,
                occurrence.start,
                occurrence.end,
                occurrence.normalized_key,
            )
            if identity not in seen:
                seen.add(identity)
                deduplicated.append(occurrence)
        blocks_by_id = {block.block_id: block for block in blocks}
        deduplicated, lexical_exclusion_counts = self._apply_model_judgement_filter(
            deduplicated,
            blocks_by_id,
        )
        candidate_count_by_block: dict[str, int] = {}
        for occurrence in deduplicated:
            candidate_count_by_block[occurrence.block_id] = (
                candidate_count_by_block.get(occurrence.block_id, 0) + 1
            )
        statuses = [
            status.model_copy(
                update={"candidate_count": candidate_count_by_block.get(status.block_id, 0)}
            )
            if status.status == "succeeded"
            else status
            for status in statuses
        ]
        candidate_hash = canonical_hash([item.model_dump(mode="json") for item in deduplicated])
        candidate_groups = self._build_candidate_groups(deduplicated)
        return ScanResult(
            contract=self._contract,
            occurrences=tuple(deduplicated),
            candidate_groups=tuple(candidate_groups),
            block_statuses=tuple(statuses),
            excluded_noise_counts=dict(sorted(excluded_counts.items())),
            lexical_exclusion_counts=lexical_exclusion_counts,
            candidate_set_hash=candidate_hash,
        )

    @staticmethod
    def _build_candidate_groups(
        occurrences: Sequence[CandidateOccurrence],
    ) -> list[CandidateGroup]:
        """把块内同一规范化词面的出现折叠成一个模型判断单元。"""

        grouped: dict[tuple[str, str], list[CandidateOccurrence]] = {}
        for occurrence in occurrences:
            key = (occurrence.block_id, occurrence.normalized_key)
            grouped.setdefault(key, []).append(occurrence)
        groups: list[CandidateGroup] = []
        for members in grouped.values():
            ordered = sorted(
                members,
                key=lambda item: (item.start, item.end, item.occurrence_id),
            )
            first = ordered[0]
            model_required = any(item.model_judgement_required for item in ordered)
            exclusion_reasons = (
                ()
                if model_required
                else tuple(
                    sorted(
                        {
                            item.lexical_exclusion_reason
                            for item in ordered
                            if item.lexical_exclusion_reason is not None
                        }
                    )
                )
            )
            identity = (
                first.snapshot_id,
                first.block_id,
                first.block_content_hash,
                first.normalized_key,
                first.scan_contract_hash,
            )
            groups.append(
                CandidateGroup(
                    candidate_group_id=deterministic_identifier("cg51", identity),
                    snapshot_id=first.snapshot_id,
                    doc_id=first.doc_id,
                    block_id=first.block_id,
                    block_index=first.block_index,
                    block_ordinal=first.block_ordinal,
                    block_content_hash=first.block_content_hash,
                    normalized_key=first.normalized_key,
                    display_text=first.display_text,
                    occurrence_ids=tuple(item.occurrence_id for item in ordered),
                    first_start=first.start,
                    scan_contract_hash=first.scan_contract_hash,
                    model_judgement_required=model_required,
                    lexical_exclusion_reasons=exclusion_reasons,
                )
            )
        groups.sort(
            key=lambda item: (
                item.block_ordinal,
                item.first_start,
                item.normalized_key,
                item.candidate_group_id,
            )
        )
        return groups

    def _apply_model_judgement_filter(
        self,
        occurrences: list[CandidateOccurrence],
        blocks_by_id: dict[str, SourceBlock],
    ) -> tuple[list[CandidateOccurrence], dict[str, int]]:
        """标记无需模型判断的高精度词法噪声，同时保留完整候选审计记录。"""

        filtered: list[CandidateOccurrence] = []
        counts: dict[str, int] = {}
        for occurrence in occurrences:
            reason = self._model_judgement_exclusion_reason(
                occurrence,
                blocks_by_id[occurrence.block_id],
            )
            if reason is None:
                filtered.append(occurrence)
                continue
            counts[reason] = counts.get(reason, 0) + 1
            filtered.append(
                occurrence.model_copy(
                    update={
                        "model_judgement_required": False,
                        "lexical_exclusion_reason": reason,
                    }
                )
            )
        return filtered, dict(sorted(counts.items()))

    @staticmethod
    def _model_judgement_exclusion_reason(
        occurrence: CandidateOccurrence,
        block: SourceBlock,
    ) -> str | None:
        """返回确定性排除原因；只排除整个候选，不因短语包含功能词而删除短语。"""

        key = occurrence.normalized_key
        # 全大写短词可能是缩写或标识符，不能因为与英文普通词同形而过滤。
        if key in _ENGLISH_MODEL_EXCLUSION_WORDS and not (
            len(occurrence.display_text) > 1 and occurrence.display_text.isupper()
        ):
            return "english_function_word"
        if key == "n't":
            return "english_contraction_fragment"
        if (
            key in _ENGLISH_CONTRACTION_SUFFIXES
            and occurrence.start > 0
            and block.text[occurrence.start - 1] in {"'", "’"}
        ):
            return "english_contraction_fragment"
        if key == "ca" and block.text[occurrence.end : occurrence.end + 3].casefold() in {
            "n't",
            "n’t",
        }:
            return "english_contraction_fragment"
        return None

    def _scan_block(self, block: SourceBlock) -> tuple[list[CandidateOccurrence], dict[str, int]]:
        masked_text, exclusions = self._mask_noise(block.text)
        occurrences: list[CandidateOccurrence] = []
        for sentence_start, sentence_end in self._sentence_spans(masked_text):
            sentence_text = masked_text[sentence_start:sentence_end]
            tokens = self._tokens_for_sentence(sentence_text, sentence_start)
            runs: list[list[_Token]] = []
            current_run: list[_Token] = []
            previous_end: int | None = None
            for token in tokens:
                gap = masked_text[previous_end : token.start] if previous_end is not None else ""
                if previous_end is not None and gap.strip() and current_run:
                    runs.append(current_run)
                    current_run = []
                if token.is_content:
                    current_run.append(token)
                else:
                    if current_run:
                        runs.append(current_run)
                    current_run = []
                previous_end = token.end
            if current_run:
                runs.append(current_run)
            for run in runs:
                for token in run:
                    self._append_candidate(
                        occurrences,
                        block,
                        block.text[token.start : token.end],
                        token.start,
                        token.end,
                    )
                if len(run) > 1:
                    phrase_start = run[0].start
                    phrase_end = run[-1].end
                    self._append_candidate(
                        occurrences,
                        block,
                        block.text[phrase_start:phrase_end],
                        phrase_start,
                        phrase_end,
                    )
        return occurrences, exclusions

    def _append_candidate(
        self,
        occurrences: list[CandidateOccurrence],
        block: SourceBlock,
        display_text: str,
        start: int,
        end: int,
    ) -> None:
        normalized_key = normalize_lexical_key(display_text)
        if not normalized_key:
            return
        occurrence_id = deterministic_identifier(
            "occ",
            (
                block.snapshot_id,
                block.block_id,
                block.content_hash,
                start,
                end,
                normalized_key,
                self._contract.contract_hash,
            ),
        )
        occurrences.append(
            CandidateOccurrence(
                occurrence_id=occurrence_id,
                snapshot_id=block.snapshot_id,
                normalized_key=normalized_key,
                display_text=block.text[start:end],
                doc_id=block.doc_id,
                block_id=block.block_id,
                block_index=block.block_index,
                block_ordinal=block.block_ordinal,
                block_content_hash=block.content_hash,
                start=start,
                end=end,
                scan_contract_hash=self._contract.contract_hash,
            )
        )

    def _mask_noise(self, text: str) -> tuple[str, dict[str, int]]:
        characters = list(text)
        counts: dict[str, int] = {}
        patterns = (
            ("url", _URL_PATTERN),
            ("email", _EMAIL_PATTERN),
            ("uuid", _UUID_PATTERN),
            ("hash", _PREFIXED_HASH_PATTERN),
        )
        for reason, pattern in patterns:
            for match in pattern.finditer(text):
                counts[reason] = counts.get(reason, 0) + 1
                for index in range(match.start(), match.end()):
                    characters[index] = " "
        return "".join(characters), counts

    def _sentence_spans(self, text: str) -> list[tuple[int, int]]:
        boundaries = {match.end() for match in _SENTENCE_HARD_BOUNDARY_PATTERN.finditer(text)}
        boundaries.update(match.end() for match in _ASCII_PERIOD_BOUNDARY_PATTERN.finditer(text))
        boundaries.add(len(text))
        spans: list[tuple[int, int]] = []
        start = 0
        for end in sorted(boundaries):
            if end > start:
                spans.append((start, end))
            start = end
        return spans

    def _tokens_for_sentence(self, sentence: str, sentence_offset: int) -> list[_Token]:
        raw_compound_spans = sorted(
            {
                (match.start(), match.end())
                for match in _COMPOUND_IDENTIFIER_PATTERN.finditer(sentence)
            },
            key=lambda item: (item[0], -(item[1] - item[0])),
        )
        compound_spans: list[tuple[int, int]] = []
        for candidate_span in raw_compound_spans:
            if any(
                candidate_span[0] < selected_end and candidate_span[1] > selected_start
                for selected_start, selected_end in compound_spans
            ):
                continue
            compound_spans.append(candidate_span)
        tokens: list[_Token] = [
            _Token(
                start=sentence_offset + start,
                end=sentence_offset + end,
                text=sentence[start:end],
                is_content=True,
            )
            for start, end in compound_spans
        ]
        covered = [False] * len(sentence)
        for start, end in compound_spans:
            for index in range(start, end):
                covered[index] = True
        cursor = 0
        while cursor < len(sentence):
            if covered[cursor]:
                cursor += 1
                continue
            gap_end = cursor
            while gap_end < len(sentence) and not covered[gap_end]:
                gap_end += 1
            segment = sentence[cursor:gap_end]
            for match in _CJK_RUN_PATTERN.finditer(segment):
                run_start = cursor + match.start()
                run_end = cursor + match.end()
                local_cursor = run_start
                for word, flag in jieba_posseg.cut(
                    sentence[run_start:run_end],
                    HMM=False,
                ):
                    word_start = local_cursor
                    word_end = word_start + len(word)
                    if sentence[word_start:word_end] != word:
                        raise ScanContractError("jieba token cannot be aligned to source text")
                    is_content = flag.startswith(_CHINESE_CONTENT_POS_PREFIXES)
                    if word in _CHINESE_BOUNDARY_WORDS:
                        is_content = False
                    tokens.append(
                        _Token(
                            start=sentence_offset + word_start,
                            end=sentence_offset + word_end,
                            text=word,
                            is_content=is_content,
                        )
                    )
                    local_cursor = word_end
            nltk_word_spans: list[tuple[int, int]] = []
            for relative_start, relative_end in self._english_tokenizer.span_tokenize(segment):
                word = segment[relative_start:relative_end]
                if _ASCII_WORD_RUN_PATTERN.fullmatch(word) is None:
                    continue
                word_start = cursor + relative_start
                word_end = cursor + relative_end
                if sentence[word_start:word_end] != word:
                    raise ScanContractError("English token cannot be aligned to source text")
                nltk_word_spans.append((relative_start, relative_end))
                if word.casefold() not in _ENGLISH_BOUNDARY_WORDS:
                    tokens.append(
                        _Token(
                            start=sentence_offset + word_start,
                            end=sentence_offset + word_end,
                            text=word,
                            is_content=True,
                        )
                    )
            # NLTK 会把紧贴中文的拉丁片段整体保留为一个非英文 token。再用纯 ASCII
            # 字符游程补扫尚未被 NLTK 英文 token 覆盖的部分，避免 `未知词Xyz` 丢失 `Xyz`。
            for match in _ASCII_WORD_RUN_PATTERN.finditer(segment):
                if any(
                    match.start() < end and match.end() > start for start, end in nltk_word_spans
                ):
                    continue
                left_is_cjk = match.start() > 0 and _CJK_CHAR_PATTERN.fullmatch(
                    segment[match.start() - 1]
                )
                right_is_cjk = match.end() < len(segment) and _CJK_CHAR_PATTERN.fullmatch(
                    segment[match.end()]
                )
                if not left_is_cjk and not right_is_cjk:
                    continue
                word = match.group(0)
                word_start = cursor + match.start()
                word_end = cursor + match.end()
                if word.casefold() in _ENGLISH_BOUNDARY_WORDS:
                    continue
                tokens.append(
                    _Token(
                        start=sentence_offset + word_start,
                        end=sentence_offset + word_end,
                        text=word,
                        is_content=True,
                    )
                )
            cursor = gap_end
        tokens.sort(key=lambda token: (token.start, token.end))
        for previous, current in zip(tokens, tokens[1:], strict=False):
            if current.start < previous.end:
                raise ScanContractError("token overlap cannot be represented deterministically")
        return tokens
