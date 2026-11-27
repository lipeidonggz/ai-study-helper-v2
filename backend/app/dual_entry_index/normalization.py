"""5.1 编译使用的确定性规范化和哈希函数。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

NORMALIZATION_VERSION = "nfkc-casefold-collapse-v1"
_WHITESPACE = re.compile(r"\s+")


def normalize_lexical_key(text: str) -> str:
    """生成只处理大小写、全半角和空白的术语规范化词面键。"""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()


def canonical_json_bytes(value: Any) -> bytes:
    """把 JSON 兼容对象编码为稳定字节序列。"""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    """返回字节序列的 SHA-256 十六进制摘要。"""

    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    """返回 UTF-8 文本的 SHA-256 十六进制摘要。"""

    return sha256_bytes(value.encode("utf-8"))


def canonical_hash(value: Any) -> str:
    """返回 JSON 兼容对象的稳定 SHA-256 摘要。"""

    return sha256_bytes(canonical_json_bytes(value))


def deterministic_identifier(prefix: str, parts: Sequence[str | int]) -> str:
    """根据有序身份字段生成稳定短标识符。"""

    payload = "|".join(str(part) for part in parts)
    return f"{prefix}_{sha256_text(payload)[:24]}"


def model_to_hashable(value: Any) -> Any:
    """把 Pydantic 模型或映射转换为可做 canonical hash 的普通 JSON 值。"""

    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): model_to_hashable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [model_to_hashable(item) for item in value]
    return value
