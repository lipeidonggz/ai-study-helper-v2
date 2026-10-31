from __future__ import annotations


class WikiCompilationError(Exception):
    """可分类的 Wiki 编译或 artifact 读取失败。"""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def __str__(self) -> str:
        """返回不包含模型正文或凭证的错误摘要。"""

        return self.message
