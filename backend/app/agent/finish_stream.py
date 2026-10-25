from __future__ import annotations

import json
import re

from app.agent.actions import ActionProtocolError

_FINISH_PREFIX_PATTERN = re.compile(r'^\s*\{\s*"type"\s*:\s*"finish"\s*,\s*"answer"\s*:\s*"')


class FinishAnswerDeltaExtractor:
    """从结构化 finish JSON 中安全提取最终答案增量。"""

    def __init__(self) -> None:
        self._raw = ""
        self._emitted = ""
        self._answer_start: int | None = None
        self._cursor = 0
        self._answer_complete = False

    def feed(self, delta: str) -> str:
        """接收一段原始输出，返回当前可安全展示的 answer 增量。"""

        if not delta:
            return ""
        self._raw += delta
        if self._answer_complete:
            return ""
        if self._answer_start is None:
            match = _FINISH_PREFIX_PATTERN.search(self._raw)
            if match is None:
                return ""
            self._answer_start = match.end()
            self._cursor = self._answer_start
        return self._drain(final=False)

    def finish(self, parsed_answer: str) -> str:
        """结束流并补齐尚未解析到或未增量展示的答案。"""

        if not parsed_answer.startswith(self._emitted):
            raise ActionProtocolError("streamed answer diverged from parsed finish answer")
        remaining = parsed_answer[len(self._emitted) :]
        self._emitted = parsed_answer
        return remaining

    @property
    def raw_content(self) -> str:
        """返回当前累计的完整模型原始输出。"""

        return self._raw

    def _drain(self, *, final: bool) -> str:
        if self._answer_start is None or self._answer_complete:
            return ""
        visible_parts: list[str] = []
        while self._cursor < len(self._raw):
            char = self._raw[self._cursor]
            if char == '"':
                self._cursor += 1
                self._answer_complete = True
                break
            if char != "\\":
                visible_parts.append(char)
                self._cursor += 1
                continue
            decoded = self._decode_escape()
            if decoded is None:
                if not final:
                    break
                raise ActionProtocolError("finish answer ends in an incomplete JSON escape")
            value, consumed = decoded
            visible_parts.append(value)
            self._cursor += consumed
        visible = "".join(visible_parts)
        self._emitted += visible
        return visible

    def _decode_escape(self) -> tuple[str, int] | None:
        if self._cursor + 1 >= len(self._raw):
            return None
        escape = self._raw[self._cursor + 1]
        simple = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if escape in simple:
            return simple[escape], 2
        if escape != "u":
            raise ActionProtocolError("finish answer contains an invalid JSON escape")
        if self._cursor + 6 > len(self._raw):
            return None
        digits = self._raw[self._cursor + 2 : self._cursor + 6]
        try:
            return json.loads(f'"\\u{digits}"'), 6
        except json.JSONDecodeError as error:
            raise ActionProtocolError("finish answer contains an invalid Unicode escape") from error
