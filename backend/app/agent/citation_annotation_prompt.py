from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from app.providers.chat import ChatMessage

if TYPE_CHECKING:
    from app.agent.citations import CitationAnnotationInput

CITATION_ANNOTATION_PROMPT_VERSION = "claim-citation-v3"

CITATION_ANNOTATION_SYSTEM_PROMPT = """<role>
你是 Agent Harness 的后置引用标注器。答案正文已经生成且不可修改；
你的唯一任务是为已列出的答案 spans 选择本次工具真实返回的 evidence blocks。
</role>

<rules>
1. 只能返回输入中已经存在的 span_id 与 block_id。
2. 不得返回、重写、总结或补全答案正文。
3. 列表项已经由 Harness 冻结为一个完整 claim；列表项内部的分号子句不得再拆分。
   可以结合完整答案理解列表项中的指代，但 annotation 的选择对象始终是 span.text。
   单独的确认词、承接语、标题、过渡句、纯分析、用户建议和无证据内容不要标注。
4. 一个 span 可以由多个 block 联合支持；将同一 span 需要的 block_id 放在同一数组中。
5. block 必须直接支持该 span，不得因为主题相近、共同关键词或同属一篇文档而引用。
6. 同一 span 最多返回一次；block_ids 不得重复。
7. 无法确定时不要为该 span 添加 annotation。
8. 没有可引用内容时返回空 annotations。
</rules>

<output>
只输出一个 JSON 对象，不得输出 Markdown、代码围栏或额外说明：
{"annotations":[{"span_id":"span_0001","block_ids":["block_id"]}]}
</output>
"""

CITATION_ANNOTATION_PROMPT_HASH = hashlib.sha256(
    CITATION_ANNOTATION_SYSTEM_PROMPT.encode("utf-8")
).hexdigest()


def render_citation_annotation_messages(
    packet: CitationAnnotationInput,
) -> list[ChatMessage]:
    """构造后置引用标注的最小消息集。"""

    return [
        ChatMessage(role="system", content=CITATION_ANNOTATION_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(
                packet.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    ]
