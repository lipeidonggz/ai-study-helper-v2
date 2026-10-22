from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_TOOL_ACTION_PATTERN = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)\[(?P<arguments>.*)\]$")


class ActionProtocolError(ValueError):
    """模型响应不符合 Thought／Action 协议。"""


@dataclass(frozen=True, slots=True)
class ToolAction:
    """要求 Harness 执行一个白名单工具的 Action。"""

    thought: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FinishAction:
    """结束 Agent 循环并携带最终回答。"""

    thought: str
    answer: str


ParsedAction = ToolAction | FinishAction


def parse_action(response: str) -> ParsedAction:
    """解析唯一一个 Thought 和一个 Action，拒绝其他输出形式。"""

    lines = response.splitlines()
    non_empty = [(index, line) for index, line in enumerate(lines) if line.strip()]
    if not non_empty:
        raise ActionProtocolError("response is empty")
    thought_lines = [index for index, line in non_empty if line.lstrip().startswith("Thought:")]
    action_lines = [index for index, line in non_empty if line.lstrip().startswith("Action:")]
    if len(thought_lines) != 1:
        raise ActionProtocolError("response must contain exactly one Thought")
    if len(action_lines) != 1:
        raise ActionProtocolError("response must contain exactly one Action")
    thought_index = thought_lines[0]
    action_index = action_lines[0]
    if non_empty[0][0] != thought_index or thought_index >= action_index:
        raise ActionProtocolError("Thought must appear before Action")
    thought = "\n".join(line.strip() for line in lines[thought_index:action_index]).strip()
    if not thought.startswith("Thought:") or not thought[8:].strip():
        raise ActionProtocolError("Thought must not be empty")

    action_line = lines[action_index].lstrip()
    action_text = action_line.removeprefix("Action:").strip()
    if action_text.startswith("Finish["):
        finish_text = "\n".join([action_text, *lines[action_index + 1 :]])
        close_index = _last_non_empty_close_bracket(finish_text)
        if close_index < 0 or finish_text[close_index + 1 :].strip():
            raise ActionProtocolError("Finish must end with its closing bracket")
        answer = finish_text[len("Finish[") : close_index]
        if not answer.strip():
            raise ActionProtocolError("Finish answer must not be empty")
        return FinishAction(thought=thought.removeprefix("Thought:").strip(), answer=answer)

    if any(line.strip() for line in lines[action_index + 1 :]):
        raise ActionProtocolError("tool Action must be one line")
    match = _TOOL_ACTION_PATTERN.fullmatch(action_text)
    if match is None:
        raise ActionProtocolError("tool Action must use name[json_object]")
    try:
        arguments = json.loads(match.group("arguments"))
    except json.JSONDecodeError as error:
        raise ActionProtocolError("tool Action arguments are not valid JSON") from error
    if not isinstance(arguments, dict):
        raise ActionProtocolError("tool Action arguments must be a JSON object")
    return ToolAction(
        thought=thought.removeprefix("Thought:").strip(),
        tool_name=match.group("name"),
        arguments=arguments,
    )


def _last_non_empty_close_bracket(value: str) -> int:
    index = len(value) - 1
    while index >= 0 and value[index].isspace():
        index -= 1
    return index if index >= 0 and value[index] == "]" else -1
