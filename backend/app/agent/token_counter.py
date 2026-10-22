from __future__ import annotations

import json
import math
from typing import Any, Protocol

from app.ingestion.blocks import estimate_context_tokens


class ObservationTokenCounter(Protocol):
    """Harness 对模型可见数据计算 token 的最小接口。"""

    @property
    def version(self) -> str:
        """返回影响估算结果的方法版本。"""
        ...

    def count(self, value: Any) -> int:
        """估算一个可 JSON 序列化对象进入模型上下文后的 token 数。"""
        ...


class ConservativeObservationTokenCounter:
    """不依赖供应商 tokenizer artifact 的保守估算器。"""

    @property
    def version(self) -> str:
        """返回估算器版本。"""

        return "chat-budget-conservative-v1"

    def count(self, value: Any) -> int:
        """按规范 JSON 估算 token，并加入固定安全余量。"""

        canonical = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        base = estimate_context_tokens(canonical)
        return max(1, math.ceil(base * 1.15) + 8)
