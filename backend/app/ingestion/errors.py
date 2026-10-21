from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IngestFailure(Exception):
    """入库流程可安全映射到本地 API 的稳定领域错误。"""

    """A classified ingestion failure safe to expose through the local API."""

    code: str
    message: str
    http_status: int = 400
    retryable: bool = False

    def __str__(self) -> str:
        """返回包含错误码的可诊断文本。"""

        return self.message
