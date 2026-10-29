from __future__ import annotations

from app.evaluation.evidence import JudgeCriterionInput, JudgeInputPacket
from app.evaluation.judge_prompt import (
    JUDGE_PROMPT_V2_VERSION,
    JUDGE_PROMPT_V3_VERSION,
    JUDGE_PROMPT_VERSION,
    SUPPORTED_JUDGE_PROMPT_VERSIONS,
    judge_prompt_for_version,
    judge_prompt_hash_for_version,
    render_judge_messages,
)


def test_reason_first_prompt_is_versioned_without_mutating_v2() -> None:
    v2_prompt = judge_prompt_for_version(JUDGE_PROMPT_V2_VERSION)
    v3_prompt = judge_prompt_for_version(JUDGE_PROMPT_V3_VERSION)
    v4_prompt = judge_prompt_for_version(JUDGE_PROMPT_VERSION)

    assert JUDGE_PROMPT_VERSION == "judge-rubric-v4"
    assert {
        JUDGE_PROMPT_V2_VERSION,
        JUDGE_PROMPT_V3_VERSION,
        JUDGE_PROMPT_VERSION,
    } == SUPPORTED_JUDGE_PROMPT_VERSIONS
    assert len({v2_prompt, v3_prompt, v4_prompt}) == 3
    assert judge_prompt_hash_for_version(JUDGE_PROMPT_V2_VERSION) != (
        judge_prompt_hash_for_version(JUDGE_PROMPT_V3_VERSION)
    )
    assert judge_prompt_hash_for_version(JUDGE_PROMPT_V3_VERSION) != (
        judge_prompt_hash_for_version(JUDGE_PROMPT_VERSION)
    )
    assert v4_prompt.index('"reason"') < v4_prompt.index('"verdict"')
    assert "verdict 必须与理由最后一句的结论一致" in v4_prompt
    assert "answer 任意位置的语义等价内容都应计入满足" in v4_prompt
    assert "answer 任意位置的语义等价内容都应计入满足" not in v3_prompt


def test_render_judge_messages_uses_criterion_prompt_version() -> None:
    packet = JudgeInputPacket(
        answer="测试答案",
        criterion=JudgeCriterionInput(rule_type="R", rule_text="测试规则"),
    )

    messages = render_judge_messages(packet, prompt_version=JUDGE_PROMPT_V2_VERSION)

    assert messages[0].content == judge_prompt_for_version(JUDGE_PROMPT_V2_VERSION)
