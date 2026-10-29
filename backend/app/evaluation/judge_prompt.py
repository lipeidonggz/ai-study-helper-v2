from __future__ import annotations

import hashlib
import json

from app.evaluation.evidence import JudgeInputPacket
from app.providers.chat import ChatMessage

JUDGE_PROMPT_V2_VERSION = "judge-rubric-v2"
JUDGE_PROMPT_VERSION = "judge-rubric-v3"

JUDGE_V2_SYSTEM_PROMPT = """<role>
你是评测判官。每次只判断一个 criterion 是否被 answer 满足。
</role>

<criterion_rules>
criterion.rule_text 是唯一判定标准。
使用正常语言理解判断同义表达、事实蕴含、遗漏和矛盾，但不得增加 criterion 未写明的条件。

对 R 判定项：
- satisfied：答案满足该判定标准；
- unsatisfied：答案明确违反、遗漏或错误满足该判定标准；
- unknown：现有输入不足以可靠判断。

对 X 判定项：
- violated：答案明确出现该边界违规；
- not_violated：现有答案没有出现该违规；
- unknown：现有输入不足以可靠判断。
</criterion_rules>

<reason_rules>
1. reason 必须直接说明答案如何满足、违反或不满足当前 criterion。
2. 不得用“看起来”“可能”“大概”等模糊理由代替三态结论。
</reason_rules>

<output>
只输出一个 JSON 对象，不输出 Markdown、代码围栏或额外说明。
字段必须完整：
{
  "verdict": "satisfied | unsatisfied | unknown | violated | not_violated",
  "reason": "可核验的简短理由"
}
</output>
"""

JUDGE_SYSTEM_PROMPT = """<role>
你是评测判官。每次只判断一个 criterion 是否被 answer 满足。
</role>

<criterion_rules>
criterion.rule_text 是唯一判定标准。
使用正常语言理解判断同义表达、事实蕴含、遗漏和矛盾，但不得增加 criterion 未写明的条件。
</criterion_rules>

<decision_order>
1. 先逐项核对 criterion 的要求以及 answer 中对应的事实，写出可核验的判断理由。
2. 理由必须明确说明全部要求是否满足；若存在遗漏、矛盾或无法判断，必须在理由中先写明。
3. 完成理由后，再根据同一段理由选择最终 verdict。
4. verdict 必须与理由最后一句的结论一致，不得出现理由认定缺少必要内容、verdict 却表示满足的矛盾。
</decision_order>

<verdict_rules>
对 R 判定项：
- satisfied：答案满足该判定标准；
- unsatisfied：答案明确违反、遗漏或错误满足该判定标准；
- unknown：现有输入不足以可靠判断。

对 X 判定项：
- violated：答案明确出现该边界违规；
- not_violated：现有答案没有出现该违规；
- unknown：现有输入不足以可靠判断。
</verdict_rules>

<output>
只输出一个 JSON 对象，不输出 Markdown、代码围栏或额外说明。
字段必须按以下顺序输出：
{
  "reason": "可核验的简短理由，先明确说明是否缺少必要内容",
  "verdict": "satisfied | unsatisfied | unknown | violated | not_violated"
}
</output>
"""

JUDGE_V2_PROMPT_HASH = hashlib.sha256(JUDGE_V2_SYSTEM_PROMPT.encode()).hexdigest()
JUDGE_PROMPT_HASH = hashlib.sha256(JUDGE_SYSTEM_PROMPT.encode()).hexdigest()
JUDGE_SYSTEM_PROMPTS = {
    JUDGE_PROMPT_V2_VERSION: JUDGE_V2_SYSTEM_PROMPT,
    JUDGE_PROMPT_VERSION: JUDGE_SYSTEM_PROMPT,
}
JUDGE_PROMPT_HASHES = {
    JUDGE_PROMPT_V2_VERSION: JUDGE_V2_PROMPT_HASH,
    JUDGE_PROMPT_VERSION: JUDGE_PROMPT_HASH,
}
SUPPORTED_JUDGE_PROMPT_VERSIONS = frozenset(JUDGE_SYSTEM_PROMPTS)


def judge_prompt_for_version(version: str) -> str:
    """返回指定版本的判官提示词，未知版本必须显式失败。"""

    try:
        return JUDGE_SYSTEM_PROMPTS[version]
    except KeyError as error:
        raise ValueError(f"unsupported judge prompt version: {version}") from error


def judge_prompt_hash_for_version(version: str) -> str:
    """返回指定版本判官提示词的稳定哈希。"""

    try:
        return JUDGE_PROMPT_HASHES[version]
    except KeyError as error:
        raise ValueError(f"unsupported judge prompt version: {version}") from error


def render_judge_messages(
    packet: JudgeInputPacket,
    *,
    prompt_version: str = JUDGE_PROMPT_VERSION,
) -> list[ChatMessage]:
    """构造单 criterion Judge 调用消息。"""

    user_prompt = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        ChatMessage(role="system", content=judge_prompt_for_version(prompt_version)),
        ChatMessage(role="user", content=user_prompt),
    ]
