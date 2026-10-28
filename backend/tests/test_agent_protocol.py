from __future__ import annotations

import json

import pytest

from app.agent.actions import (
    ActionProtocolError,
    FinishAction,
    ToolAction,
    parse_action,
)
from app.agent.finish_stream import FinishAnswerDeltaExtractor
from app.agent.prompt import release_prompt_bundle
from app.agent.token_counter import ConservativeObservationTokenCounter
from app.agent.tool_adapter import TOOL_CONTRACT_VERSION, tool_descriptors


def test_parse_finish_action_with_multiline_answer() -> None:
    response = json.dumps(
        {
            "type": "finish",
            "answer": "第一行\n第二行",
            "decision_summary": "工具结果已经足够。",
        },
        ensure_ascii=False,
    )

    action = parse_action(response)

    assert isinstance(action, FinishAction)
    assert action.answer == "第一行\n第二行"
    assert action.decision_summary == "工具结果已经足够。"


def test_finish_allows_omitted_summary_and_repairs_unencoded_newlines() -> None:
    action = parse_action('{"type":"finish","answer":"第一行\n第二行"}')

    assert isinstance(action, FinishAction)
    assert action.decision_summary == ""
    assert action.answer == "第一行\n第二行"


def test_finish_recovers_extra_closing_brace() -> None:
    first = json.dumps({"type": "finish", "answer": "完整答案"}, ensure_ascii=False)

    action = parse_action(first + "}")

    assert isinstance(action, FinishAction)
    assert action.answer == "完整答案"


def test_finish_recovers_exact_duplicate_object() -> None:
    first = json.dumps({"type": "finish", "answer": "完整答案"}, ensure_ascii=False)

    action = parse_action(first + "\n" + first)

    assert isinstance(action, FinishAction)
    assert action.answer == "完整答案"


def test_parse_tool_action_with_json_object() -> None:
    response = json.dumps(
        {
            "type": "tool_call",
            "decision_summary": "需要检索当前知识库。",
            "tool": "search_blocks",
            "arguments": {"query": "MCP", "doc_ids": ["doc_a"]},
        },
        ensure_ascii=False,
    )

    action = parse_action(response)

    assert isinstance(action, ToolAction)
    assert action.tool_name == "search_blocks"
    assert action.arguments == {"query": "MCP", "doc_ids": ["doc_a"]}
    assert action.decision_summary == "需要检索当前知识库。"


@pytest.mark.parametrize(
    "response",
    [
        "",
        "Thought: old protocol\nAction: Finish[answer]",
        ('{"type":"tool_call","decision_summary":"x","tool":"search_blocks","arguments":[]}'),
        ('{"type":"tool_call","decision_summary":"x","tool":"search blocks","arguments":{}}'),
        '{"type":"tool_result","harness_status":"ok","payload":{}}',
        ('{"type":"finish","answer":"x","decision_summary":"x","extra":true}'),
        ('{"type":"finish","type":"tool_call","answer":"x","decision_summary":"x"}'),
        (
            '{"type":"finish","answer":"x","decision_summary":"x"}\n'
            '{"type":"finish","answer":"y","decision_summary":"y"}'
        ),
        '```json\n{"type":"finish","answer":"x","decision_summary":"x"}\n```',
        '{"type":"tool_call","decision_summary":"x",'
        '"tool":"search_blocks","arguments":{"score":NaN}}',
        '{"type":"finish","answer":"x","decision_summary":"x"',
    ],
)
def test_invalid_action_protocol_is_rejected(response: str) -> None:
    with pytest.raises(ActionProtocolError):
        parse_action(response)


def test_structured_finish_stream_emits_only_answer_content() -> None:
    extractor = FinishAnswerDeltaExtractor()
    chunks = [
        '{"type":"finish","answer":"第一行\\n',
        '第二行 \\"引用\\" \\u4e2d',
        '\\u6587","decision_summary":"证据足够"}',
    ]

    emitted = "".join(extractor.feed(chunk) for chunk in chunks)
    emitted += extractor.finish('第一行\n第二行 "引用" 中文')

    assert emitted == '第一行\n第二行 "引用" 中文'
    assert "decision_summary" not in emitted


def test_structured_tool_call_stream_emits_no_answer() -> None:
    extractor = FinishAnswerDeltaExtractor()
    raw = json.dumps(
        {
            "type": "tool_call",
            "decision_summary": "需要工具。",
            "tool": "search_blocks",
            "arguments": {"query": "MCP"},
        },
        ensure_ascii=False,
    )

    assert extractor.feed(raw) == ""


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

    assert first.version == "chat-v15"
    assert first.template_hash == second.template_hash
    assert len(first.template_hash) == 64
    assert "{{RESPONSE_POLICY}}" in first.template
    rendered = first.render(tools=[], collection_status={})
    assert '"type":"tool_call"' in rendered
    assert '"type":"finish"' in rendered
    assert "GitHub Flavored Markdown" in first.template
    assert "禁止输出原始 HTML" in first.template
    assert "收益与代价" in first.template
    assert "原因与结果" in first.template
    assert "不能改变各事实在关系中的角色" in first.template
    assert "收益是……；代价是……" in first.template
    assert "不得使用“以……为代价”等单一连接词" in first.template
    assert "section_path 只描述该 block" in first.template
    assert "不是完整文档目录或章节数量声明" in first.template
    assert "不得把它自行改写为“文章开头”" in first.template
    assert "只有工具结果明确给出可核验的先后关系" in first.template
    assert "区分“通用知识”和“文档事实”" in first.template
    assert "以下是通用背景，不是原文表述" in first.template
    assert "不能用模型知识代替" in first.template
    assert "对同时包含通用解释和文档事实的问题" in first.template
    for case_specific_token in ("83%", "93%", "17%", "0.4%", "84%", "gVisor", "MCP"):
        assert case_specific_token not in first.template


def test_finalization_prompt_requires_structured_finish() -> None:
    prompt = release_prompt_bundle().render(
        tools=[],
        collection_status={},
        finalization_mode=True,
    )

    assert "<finalization_mode>" in prompt
    assert "工具已禁用" in prompt
    assert '"type":"finish"' in prompt
    assert "内容未完" in prompt


def test_outline_contract_distinguishes_default_root_from_single_section() -> None:
    outline = next(item for item in tool_descriptors() if item.name == "get_outline")

    assert TOOL_CONTRACT_VERSION == "retrieval-tools-v5"
    assert "path=[]" in outline.description
    assert "解析 fallback" in outline.description
    assert "只有一个章节" in outline.description


def test_tool_descriptors_render_input_and_output_schemas() -> None:
    descriptors = tool_descriptors()

    assert {item.name for item in descriptors} == {
        "list_documents",
        "get_outline",
        "search_blocks",
        "list_blocks",
        "get_blocks",
        "scan_term",
    }
    assert all(item.output_schema for item in descriptors)
    rendered = release_prompt_bundle().render(
        tools=descriptors,
        collection_status={},
    )
    assert rendered.count("输入 schema:") == len(descriptors)
    assert rendered.count("输出 schema:") == len(descriptors)
    assert '"error"' in rendered
    assert '"section_context"' in rendered


def test_list_blocks_contract_requires_explicit_descendant_scope() -> None:
    descriptor = next(item for item in tool_descriptors() if item.name == "list_blocks")
    properties = descriptor.input_schema["properties"]

    assert "include_descendants" in properties
    assert "省略 section_paths 表示整篇" in descriptor.description
    assert "include_descendants=false" in release_prompt_bundle().template
