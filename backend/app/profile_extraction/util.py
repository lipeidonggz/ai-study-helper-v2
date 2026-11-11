from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from app.retrieval.models import RetrievalBlock

NAME_NORMALIZATION_VERSION = "nfkc-casefold-v1"
BLOCK_WINDOW_POLICY_VERSION = "blocks-16-chars-12000-v1"
BLOCK_WINDOW_SIZE = 16
BLOCK_WINDOW_CHARS = 12_000
DEFAULT_PROFILE_ID = "default"


def json_dumps(value: Any) -> str:
    """按稳定键序编码 JSON，避免无意义 hash 漂移。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads_list(value: str) -> list[Any]:
    """解析数据库中预期为数组的 JSON，并拒绝错误结构。"""

    parsed = json.loads(value)
    if not isinstance(parsed, list):
        raise ValueError("stored JSON must be an array")
    return parsed


def json_loads_object(value: str) -> dict[str, Any]:
    """解析数据库中预期为对象的 JSON，并拒绝错误结构。"""

    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("stored JSON must be an object")
    return parsed


def json_loads_string_list(value: str) -> list[str]:
    """解析数据库中预期为字符串数组的 JSON。"""

    parsed = json_loads_list(value)
    if not all(isinstance(item, str) for item in parsed):
        raise ValueError("stored JSON must contain only strings")
    return [str(item) for item in parsed]


def json_loads_object_list(value: str) -> list[dict[str, object]]:
    """解析数据库中预期为对象数组的 JSON。"""

    parsed = json_loads_list(value)
    if not all(isinstance(item, dict) for item in parsed):
        raise ValueError("stored JSON must contain only objects")
    return [dict(item) for item in parsed]


def sha256_text(value: str) -> str:
    """返回 UTF-8 文本的完整 SHA-256。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_hash(value: Mapping[str, Any] | Sequence[Any]) -> str:
    """对 JSON 可序列化值计算稳定 SHA-256。"""

    return sha256_text(json_dumps(value))


def new_identifier(prefix: str) -> str:
    """生成带业务前缀的不可猜本地标识。"""

    return f"{prefix}_{uuid4().hex[:16]}"


def normalize_name(value: str) -> str:
    """把候选名称归一为第一阶段的精确去重键。"""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", " ", normalized).strip()


def ordered_unique(values: Iterable[str]) -> list[str]:
    """保留首次出现顺序并去掉完全相同的字符串。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def ordered_unique_normalized_text(values: Iterable[str]) -> list[str]:
    """按内容归一化键去重，同时保留首次出现的原始文本顺序。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(value.split())
        key = normalize_name(text)
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def split_blocks(
    blocks: Sequence[RetrievalBlock],
    *,
    max_blocks: int = BLOCK_WINDOW_SIZE,
    max_chars: int = BLOCK_WINDOW_CHARS,
) -> list[list[RetrievalBlock]]:
    """按稳定 block 数和字符上限把完整文档划分为连续窗口。"""

    if max_blocks < 1 or max_chars < 1:
        raise ValueError("window limits must be positive")
    windows: list[list[RetrievalBlock]] = []
    current: list[RetrievalBlock] = []
    current_chars = 0
    for block in blocks:
        block_chars = len(block.text)
        if current and (len(current) >= max_blocks or current_chars + block_chars > max_chars):
            windows.append(current)
            current = []
            current_chars = 0
        current.append(block)
        current_chars += block_chars
    if current:
        windows.append(current)
    return windows


def estimate_tokens(text: str) -> int:
    """提供稳定的保守字符估算，不作为供应商账单承诺。"""

    return max(1, (len(text) + 2) // 3)
