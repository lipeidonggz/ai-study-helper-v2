from __future__ import annotations

import pytest

from app.agent.actions import ActionProtocolError
from app.agent.finish_stream import FinishAnswerDeltaExtractor


def test_finish_stream_extracts_only_answer_across_structured_json_chunks() -> None:
    extractor = FinishAnswerDeltaExtractor()

    emitted = "".join(
        extractor.feed(chunk)
        for chunk in (
            '{"type":"finish",',
            '"answer":"第一段，含 [方括号]',
            '\\n第二行","decision_summary":"证据足够"}',
        )
    )
    emitted += extractor.finish("第一段，含 [方括号]\n第二行")

    assert emitted == "第一段，含 [方括号]\n第二行"
    assert "decision_summary" not in emitted


def test_finish_stream_decodes_split_json_escapes() -> None:
    extractor = FinishAnswerDeltaExtractor()

    assert extractor.feed('{"type":"finish","answer":"前\\') == "前"
    assert extractor.feed("n") == "\n"
    assert extractor.feed('后 \\"引用\\"') == '后 "引用"'
    assert extractor.feed('","decision_summary":"完成"}') == ""
    assert extractor.finish('前\n后 "引用"') == ""


def test_finish_stream_does_not_emit_tool_call_content() -> None:
    extractor = FinishAnswerDeltaExtractor()
    raw = (
        '{"type":"tool_call","decision_summary":"需要检索",'
        '"tool":"search_blocks","arguments":{"query":"MCP"}}'
    )

    assert extractor.feed(raw) == ""


def test_finish_stream_rejects_divergent_parsed_answer() -> None:
    extractor = FinishAnswerDeltaExtractor()
    assert extractor.feed('{"type":"finish","answer":"原答案') == "原答案"

    with pytest.raises(ActionProtocolError):
        extractor.finish("不同答案")
