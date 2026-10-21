from __future__ import annotations

import re
import unicodedata

from app.indexing.lancedb_index import LANCEDB_ENGINE_VERSION
from app.indexing.models import KeywordProfile
from app.ingestion.models import BlockRecord

KEYWORD_ANALYZER_VERSION = "1"
SEARCH_TEXT_POLICY_VERSION = "keyword-search-text-v1"
# LanceDB 原生 FTS 不开放 k1/b 覆写；null 表示参数由引擎内部管理，不是漏配。
KEYWORD_PROFILE = KeywordProfile(
    engine="lancedb-native-bm25",
    engine_version=LANCEDB_ENGINE_VERSION,
    analyzer="unicode-cjk-code-v1",
    analyzer_version=KEYWORD_ANALYZER_VERSION,
    search_text_policy_version=SEARCH_TEXT_POLICY_VERSION,
    bm25_k1=None,
    bm25_b=None,
    bm25_parameters_source="lancedb-engine-managed",
)

_CJK_RUN_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_TECH_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9À-ÖØ-öø-ÿ]+(?:[._:/\-][A-Za-z0-9À-ÖØ-öø-ÿ]+)*")
_TECH_SEPARATOR_PATTERN = re.compile(r"[._:/\-]+")
_CAMEL_PART_PATTERN = re.compile(r"[A-Z]+(?=[A-Z][a-z]|[0-9]|$)|[A-Z]?[a-z]+|[0-9]+|[A-Z]+")


def build_keyword_search_text(document_title: str, block: BlockRecord) -> str:
    """组合标题、章节、表格上下文与原文，供关键词 analyzer 建索引。"""

    section_path = " > ".join(block.section_path) or "根章节"
    lines = [
        unicodedata.normalize("NFC", document_title),
        section_path,
    ]
    if block.table_context is not None:
        if block.table_context.caption:
            lines.append(block.table_context.caption)
        if block.table_context.headers:
            lines.append(" | ".join(block.table_context.headers))
    lines.append(block.text)
    return "\n".join(lines)


def _append_technical_token(terms: list[str], token: str) -> None:
    """保留原标识符，并生成由分隔符和驼峰边界定义的子词。"""

    normalized = token.casefold()
    terms.append(normalized)
    parts = [part for part in _TECH_SEPARATOR_PATTERN.split(token) if part]
    if len(parts) > 1:
        for part in parts:
            terms.append(part.casefold())
    for part in parts:
        camel_parts = _CAMEL_PART_PATTERN.findall(part)
        if len(camel_parts) > 1:
            terms.extend(camel_part.casefold() for camel_part in camel_parts)
    if len(parts) == 1:
        digit_suffix = re.fullmatch(r"([A-Za-z]+)([0-9]+)", parts[0])
        if digit_suffix is not None:
            terms.append(digit_suffix.group(1).casefold())
            terms.append(digit_suffix.group(2))


def _append_cjk_token(terms: list[str], token: str) -> None:
    """生成 CJK 单字与相邻双字，避免依赖外部词典。"""

    terms.extend(token)
    terms.extend(token[index : index + 2] for index in range(len(token) - 1))


def analyze_keyword_terms(text: str) -> list[str]:
    """按固定 `unicode-cjk-code-v1` 规则返回保留重复次数的 token 序列。"""

    normalized = unicodedata.normalize("NFC", text)
    matches: list[tuple[int, str, re.Match[str]]] = []
    matches.extend((match.start(), "cjk", match) for match in _CJK_RUN_PATTERN.finditer(normalized))
    matches.extend(
        (match.start(), "technical", match) for match in _TECH_TOKEN_PATTERN.finditer(normalized)
    )

    terms: list[str] = []
    for _, kind, match in sorted(matches, key=lambda item: item[0]):
        if kind == "cjk":
            _append_cjk_token(terms, match.group(0))
        else:
            _append_technical_token(terms, match.group(0))
    return terms


def build_keyword_terms(search_text: str) -> str:
    """把 analyzer token 序列编码为 LanceDB whitespace FTS 输入。"""

    return " ".join(analyze_keyword_terms(search_text))
