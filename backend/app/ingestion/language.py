from __future__ import annotations

import math
import re

from app.ingestion.models import DocumentLanguage

LANGUAGE_DETECTOR_VERSION = "script-ratio-v1"
MIN_LANGUAGE_SIGNAL_CHARACTERS = 20
MIXED_SCRIPT_MIN_PERCENT = 20
DOMINANT_SCRIPT_MIN_PERCENT = 80

_CJK_CHARACTER_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_LETTER_PATTERN = re.compile(r"[A-Za-z]")


def normalize_declared_language(value: str | None) -> DocumentLanguage | None:
    """Map a declared zh/en language tag to the first-version language set."""

    if not value:
        return None
    primary_tag = value.strip().lower().replace("_", "-").split("-", maxsplit=1)[0]
    if primary_tag == "zh":
        return "zh"
    if primary_tag == "en":
        return "en"
    return None


def classify_text_language(text: str) -> DocumentLanguage:
    """Classify text by deterministic Chinese-character and Latin-letter ratios."""

    cjk_count = len(_CJK_CHARACTER_PATTERN.findall(text))
    latin_count = len(_LATIN_LETTER_PATTERN.findall(text))
    signal_count = cjk_count + latin_count
    if signal_count < MIN_LANGUAGE_SIGNAL_CHARACTERS:
        return "unknown"

    mixed_minimum = max(
        1,
        math.ceil(signal_count * MIXED_SCRIPT_MIN_PERCENT / 100),
    )
    if cjk_count >= mixed_minimum and latin_count >= mixed_minimum:
        return "mixed"
    if cjk_count * 100 >= signal_count * DOMINANT_SCRIPT_MIN_PERCENT:
        return "zh"
    if latin_count * 100 >= signal_count * DOMINANT_SCRIPT_MIN_PERCENT:
        return "en"
    return "unknown"


def detect_document_language(
    declared_language: str | None,
    body_text: str,
) -> DocumentLanguage:
    """Resolve document language from reliable metadata, then body text."""

    declared = normalize_declared_language(declared_language)
    return declared if declared is not None else classify_text_language(body_text)


def detect_block_language(
    text: str,
    document_language: DocumentLanguage,
) -> DocumentLanguage:
    """Classify a block, using the document language only for insufficient signals."""

    detected = classify_text_language(text)
    if detected == "unknown" and document_language in {"zh", "en"}:
        return document_language
    return detected
