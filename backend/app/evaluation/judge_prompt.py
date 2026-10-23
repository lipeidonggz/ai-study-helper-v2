from __future__ import annotations

import hashlib
import json

from app.evaluation.evidence import JudgeInputPacket
from app.providers.chat import ChatMessage

JUDGE_PROMPT_VERSION = "judge-rubric-v2"

JUDGE_SYSTEM_PROMPT = """<role>
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

JUDGE_PROMPT_HASH = hashlib.sha256(JUDGE_SYSTEM_PROMPT.encode()).hexdigest()


def render_judge_messages(packet: JudgeInputPacket) -> list[ChatMessage]:
    """构造单 criterion Judge 调用消息。"""

    user_prompt = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        ChatMessage(role="system", content=JUDGE_SYSTEM_PROMPT),
        ChatMessage(role="user", content=user_prompt),
    ]
