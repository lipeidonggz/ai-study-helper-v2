from __future__ import annotations

import pytest

from app.agent.actions import ActionProtocolError
from app.agent.finish_stream import FinishAnswerDeltaExtractor


def test_finish_stream_extracts_only_final_answer_across_chunks() -> None:
    extractor = FinishAnswerDeltaExtractor()

    emitted = "".join(
        extractor.feed(chunk)
        for chunk in (
            "Thought: 内部推理不应展示。\n",
            "Action: Fin",
            "ish[第一段",
            "，含 [方括号]",
            "内容",
            "]",
        )
    )
    emitted += extractor.finish("第一段，含 [方括号]内容")

    assert emitted == "第一段，含 [方括号]内容"
    assert "内部推理" not in emitted
    assert emitted.endswith("内容")


def test_finish_stream_holds_possible_closing_bracket_until_more_text() -> None:
    extractor = FinishAnswerDeltaExtractor()

    assert extractor.feed("Thought: 完成。\nAction: Finish[答案") == "答案"
    assert extractor.feed("]") == ""
    assert extractor.feed("继续]") == "]继续"
    assert extractor.finish("答案]继续") == ""


def test_finish_stream_rejects_divergent_parsed_answer() -> None:
    extractor = FinishAnswerDeltaExtractor()
    assert extractor.feed("Thought: 完成。\nAction: Finish[原答案]") == "原答案"

    with pytest.raises(ActionProtocolError):
        extractor.finish("不同答案")
