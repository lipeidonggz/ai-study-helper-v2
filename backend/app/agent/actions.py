from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ActionProtocolError(ValueError):
    """模型响应不符合结构化 ReAct 协议。"""


@dataclass(frozen=True, slots=True)
class ToolAction:
    """要求 Harness 执行一个白名单工具。"""

    decision_summary: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FinishAction:
    """结束 Agent 循环并携带最终回答。"""

    answer: str
    decision_summary: str = ""


ParsedAction = ToolAction | FinishAction


class _ToolCallResponse(BaseModel):
    """模型可返回的工具调用 JSON。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["tool_call"]
    decision_summary: str = Field(min_length=1)
    tool: str = Field(min_length=1)
    arguments: dict[str, Any]

    @field_validator("decision_summary")
    @classmethod
    def validate_decision_summary(cls, value: str) -> str:
        """拒绝只有空白的决策摘要。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("decision_summary must not be empty")
        return normalized

    @field_validator("tool")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        """工具名必须是稳定标识符，具体白名单由 ToolAdapter 校验。"""

        if _TOOL_NAME_PATTERN.fullmatch(value) is None:
            raise ValueError("tool must be a stable identifier")
        return value


class _FinishResponse(BaseModel):
    """模型可返回的最终答案 JSON。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["finish"]
    answer: str = Field(min_length=1)
    decision_summary: str = ""

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, value: str) -> str:
        """拒绝只有空白的最终答案。"""

        if not value.strip():
            raise ValueError("answer must not be empty")
        return value


def parse_action(response: str) -> ParsedAction:
    """严格解析唯一一个 tool_call 或 finish JSON 对象。"""

    try:
        payload = json.loads(
            _escape_control_characters_in_strings(response),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise ActionProtocolError("response must be one valid JSON object") from error
    if not isinstance(payload, dict):
        raise ActionProtocolError("response must be a JSON object")
    response_type = payload.get("type")
    try:
        if response_type == "tool_call":
            tool_call = _ToolCallResponse.model_validate(payload)
            return ToolAction(
                decision_summary=tool_call.decision_summary,
                tool_name=tool_call.tool,
                arguments=tool_call.arguments,
            )
        if response_type == "finish":
            finish = _FinishResponse.model_validate(payload)
            return FinishAction(
                answer=finish.answer,
                decision_summary=finish.decision_summary.strip(),
            )
    except ValidationError as error:
        raise ActionProtocolError("response does not match its action schema") from error
    raise ActionProtocolError("response type must be tool_call or finish")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝 JSON 重复键，避免后写字段覆盖先写字段。"""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> Any:
    """JSON 协议不接受 NaN 或 Infinity。"""

    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _escape_control_characters_in_strings(value: str) -> str:
    """把模型漏转义的 JSON 字符串控制字符转成合法转义。"""

    escaped: list[str] = []
    in_string = False
    after_backslash = False
    replacements = {
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    for char in value:
        if in_string and after_backslash:
            escaped.append(char)
            after_backslash = False
            continue
        if char == "\\" and in_string:
            escaped.append(char)
            after_backslash = True
            continue
        if char == '"':
            in_string = not in_string
            escaped.append(char)
            continue
        if in_string and ord(char) < 0x20:
            escaped.append(replacements.get(char, f"\\u{ord(char):04x}"))
            continue
        escaped.append(char)
    return "".join(escaped)
