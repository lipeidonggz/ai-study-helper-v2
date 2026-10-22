from __future__ import annotations

import re

from app.agent.actions import ActionProtocolError

_FINISH_ACTION_PATTERN = re.compile(r"(?m)^\s*Action:\s*Finish\[")


class FinishAnswerDeltaExtractor:
    """从原始模型流中提取安全的 Finish 正文增量，不暴露 Thought 或 Action。"""

    def __init__(self) -> None:
        self._raw = ""
        self._emitted = ""
        self._answer_start: int | None = None

    def feed(self, delta: str) -> str:
        """接收一段原始模型输出，返回当前可以安全展示的最终答案增量。"""

        if not delta:
            return ""
        self._raw += delta
        if self._answer_start is None:
            match = _FINISH_ACTION_PATTERN.search(self._raw)
            if match is None:
                return ""
            self._answer_start = match.end()
        return self._drain(final=False)

    def finish(self, parsed_answer: str) -> str:
        """流结束后输出最后一个安全增量，并校验已经展示的内容是最终答案前缀。"""

        if not parsed_answer.startswith(self._emitted):
            raise ActionProtocolError("streamed answer diverged from parsed Finish answer")
        remaining = parsed_answer[len(self._emitted) :]
        self._emitted = parsed_answer
        return remaining

    @property
    def raw_content(self) -> str:
        """返回当前累计的完整模型原始输出。"""

        return self._raw

    def _drain(self, *, final: bool) -> str:
        if self._answer_start is None:
            return ""
        available_end = len(self._raw)
        trailing = self._raw.rstrip()
        if not final and trailing.endswith("]"):
            available_end = len(trailing) - 1
        available = self._raw[self._answer_start : available_end]
        if not available.startswith(self._emitted):
            raise ActionProtocolError("streamed answer buffer changed non-monotonically")
        new_text = available[len(self._emitted) :]
        self._emitted = available
        return new_text
