from __future__ import annotations

import pytest

from app.ingestion.extract import extract_html_document
from app.ingestion.language import (
    classify_text_language,
    detect_block_language,
    detect_document_language,
    normalize_declared_language,
)
from tests.helpers import source_artifact_for_bytes


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("这是一段用于识别中文的正文内容，包含足够多的汉字。", "zh"),
        ("This paragraph contains enough English words for deterministic detection.", "en"),
        (
            "这是一段中文说明，并包含足够多的 English technical terms "
            "to make the block genuinely mixed. 继续使用中文解释实现细节。",
            "mixed",
        ),
        ("短文本", "unknown"),
        ("1234 !@#$ 5678", "unknown"),
    ],
)
def test_classify_text_language_is_deterministic(text: str, expected: str) -> None:
    assert classify_text_language(text) == expected


def test_declared_language_takes_precedence_for_document() -> None:
    assert normalize_declared_language("zh-CN") == "zh"
    assert normalize_declared_language("en_US") == "en"
    assert normalize_declared_language("ja-JP") is None
    assert (
        detect_document_language(
            "zh-CN",
            "This English body would otherwise be classified as English.",
        )
        == "zh"
    )


def test_block_language_uses_document_only_for_insufficient_signal() -> None:
    assert (
        detect_block_language(
            "This block is clearly written in English and contains enough signal.",
            "zh",
        )
        == "en"
    )
    assert detect_block_language("OK", "zh") == "zh"
    assert detect_block_language("OK", "unknown") == "unknown"


def test_extraction_accepts_content_language_metadata() -> None:
    raw = (
        b"<!doctype html><html><head><title>Metadata language</title>"
        b"<meta http-equiv='Content-Language' content='en-US'></head>"
        b"<body><article><h1>Metadata language</h1><p>"
        b"This paragraph exists to provide enough readable English text for extraction."
        b"</p></article></body></html>"
    )
    extracted = extract_html_document(
        source_artifact_for_bytes("metadata-language.html", raw),
        raw.decode("utf-8"),
    )

    assert extracted.language == "en"


def test_extraction_skips_unsupported_html_lang_and_uses_supported_metadata() -> None:
    raw = (
        b"<!doctype html><html lang='ja'><head><title>Supported metadata</title>"
        b"<meta name='language' content='en-US'></head>"
        b"<body><article><h1>Supported metadata</h1><p>"
        b"This English paragraph should use the supported metadata language.</p>"
        b"</article></body></html>"
    )
    extracted = extract_html_document(
        source_artifact_for_bytes("supported-metadata.html", raw),
        raw.decode("utf-8"),
    )

    assert extracted.language == "en"


def test_extraction_and_blocks_detect_mixed_language() -> None:
    text = (
        "这是用于说明实现边界的足够长中文内容，确保中文信号不会被忽略。"
        "This English explanation adds enough Latin signal to make the block mixed."
    )
    raw = (
        "<!doctype html><html><head><title>Mixed language</title></head>"
        f"<body><article><h1>Mixed language</h1><h2>Boundary</h2><p>{text}</p>"
        "</article></body></html>"
    ).encode()
    extracted = extract_html_document(
        source_artifact_for_bytes("mixed-language.html", raw),
        raw.decode("utf-8"),
    )

    assert extracted.language == "mixed"
    assert extracted.nodes[-1].text == text
