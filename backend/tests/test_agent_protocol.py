from __future__ import annotations

import pytest

from app.agent.actions import (
    ActionProtocolError,
    FinishAction,
    ToolAction,
    parse_action,
)
from app.agent.prompt import release_prompt_bundle
from app.agent.token_counter import ConservativeObservationTokenCounter


def test_parse_finish_action_with_multiline_answer() -> None:
    action = parse_action("Thought: 证据已经足够。\nAction: Finish[第一行\n第二行]")

    assert isinstance(action, FinishAction)
    assert action.answer == "第一行\n第二行"


def test_parse_tool_action_with_json_object() -> None:
    action = parse_action(
        'Thought: 需要检索。\nAction: search_blocks[{"query":"MCP","doc_ids":["doc_a"]}]'
    )

    assert isinstance(action, ToolAction)
    assert action.tool_name == "search_blocks"
    assert action.arguments == {"query": "MCP", "doc_ids": ["doc_a"]}


@pytest.mark.parametrize(
    "response",
    [
        "",
        "Action: Finish[answer]",
        "Thought: only a thought",
        'Thought: one\nAction: search_blocks[{"query":"x"}]\nThought: two\nAction: Finish[x]',
        "Thought: bad json\nAction: search_blocks[{broken}]",
        "Thought: array\nAction: search_blocks[[]]",
        'Thought: trailing\nAction: search_blocks[{"query":"x"}] extra',
        "Thought: finish\nAction: Finish[answer] trailing",
    ],
)
def test_invalid_action_protocol_is_rejected(response: str) -> None:
    with pytest.raises(ActionProtocolError):
        parse_action(response)


def test_conservative_observation_counter_is_stable_and_versioned() -> None:
    counter = ConservativeObservationTokenCounter()
    value = {"tool": "scan_term", "payload": {"matches": [{"text": "沙箱"}]}}

    first = counter.count(value)
    second = counter.count(value)

    assert counter.version == "chat-budget-conservative-v1"
    assert first == second
    assert first > 0


def test_release_prompt_bundle_has_stable_version_and_hash() -> None:
    first = release_prompt_bundle()
    second = release_prompt_bundle()

    assert first.version == "chat-v2"
    assert first.template_hash == second.template_hash
    assert len(first.template_hash) == 64
    assert "GitHub Flavored Markdown" in first.template
    assert "禁止输出原始 HTML" in first.template
