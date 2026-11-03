from __future__ import annotations


class ProfileExtractionError(Exception):
    """Profile 抽取流程中可稳定映射为本地 API 错误的领域异常。"""

    def __init__(self, code: str, message: str, http_status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = http_status >= 500
