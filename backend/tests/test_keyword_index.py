from __future__ import annotations

import unicodedata

import lancedb

from app.indexing.keywords import (
    KEYWORD_PROFILE,
    analyze_keyword_terms,
    build_keyword_search_text,
)
from app.ingestion.models import BlockRecord, TableContext


def _block() -> BlockRecord:
    return BlockRecord(
        block_id="doc_test#v0001#c1#b00001",
        section_path=["边界", "网络控制"],
        block_index=1,
        language="mixed",
        text="LangGraph 的 doc_id 指向 workspace-safe 网络边界。",
        char_start=0,
        char_end=54,
        content_hash="a" * 64,
        source_node_ordinals=[1],
        table_context=TableContext(
            caption="工具表",
            headers=["HTTP_Server", "权限"],
            group_index=1,
            group_count=1,
        ),
        embedding_hints={},
        context_token_estimate=20,
        estimator_version="cjk-ascii-v1",
    )


def test_keyword_search_text_includes_title_path_table_and_body() -> None:
    text = build_keyword_search_text("Agent 隔离设计", _block())

    assert "Agent 隔离设计" in text
    assert "边界 > 网络控制" in text
    assert "工具表" in text
    assert "HTTP_Server | 权限" in text
    assert _block().text in text


def test_keyword_analyzer_preserves_identifiers_and_adds_subtokens() -> None:
    terms = analyze_keyword_terms("LangGraph doc_id HTTP_Server v1.2 网络边界")

    assert "langgraph" in terms
    assert "lang" in terms
    assert "graph" in terms
    assert "doc_id" in terms
    assert "doc" in terms
    assert "id" in terms
    assert "http_server" in terms
    assert "http" in terms
    assert "server" in terms
    assert "v1.2" in terms
    assert "v1" in terms
    assert "2" in terms
    assert "网" in terms
    assert "网络" in terms
    assert "络边" in terms
    assert "边界" in terms


def test_keyword_analyzer_uses_nfc_and_case_folding() -> None:
    decomposed = "Cafe\u0301 LANGGRAPH"
    terms = analyze_keyword_terms(decomposed)

    assert unicodedata.normalize("NFC", decomposed) != decomposed
    assert "café" in terms
    assert "langgraph" in terms
    assert analyze_keyword_terms(decomposed) == analyze_keyword_terms(
        unicodedata.normalize("NFC", decomposed)
    )


def test_keyword_analyzer_does_not_stem_or_expand_synonyms() -> None:
    terms = analyze_keyword_terms("running protections")

    assert "running" in terms
    assert "run" not in terms
    assert "protection" not in terms
    assert "security" not in terms


def test_keyword_analyzer_is_deterministic() -> None:
    value = "A clean-room HTML 双索引 analyzer-v1"

    assert analyze_keyword_terms(value) == analyze_keyword_terms(value)


def test_keyword_profile_does_not_claim_engine_managed_bm25_values() -> None:
    assert KEYWORD_PROFILE.engine == "lancedb-native-bm25"
    assert KEYWORD_PROFILE.engine_version == lancedb.__version__
    assert KEYWORD_PROFILE.bm25_k1 is None
    assert KEYWORD_PROFILE.bm25_b is None
    assert KEYWORD_PROFILE.bm25_parameters_source == "lancedb-engine-managed"
