from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, Field

from app.providers.chat import ChatMessage

CITATION_SUPPORT_PROMPT_VERSION = "citation-rubric-v2"

CITATION_SUPPORT_SYSTEM_PROMPT = """<role>
你是本地文档问答的引用支持检查器。
你每次只检查一个 Agent 答案中的显式引用，逐个判断所引证据是否支持答案中的相关主张。
</role>

<inputs>
你只会收到：
1. Agent 最终答案；
2. 答案中显式引用到的证据块。

每个证据块只包含：
- block_id；
- doc_title；
- section_path；
- text。
</inputs>

<answer_citation_format>
答案中的显式引用固定写作：【来源：block_id】。
多个 block 写作：【来源：block_1，block_2】。
引用标记位于被支持句子或列表项末尾。
每个输入 citation 都对应答案中的一个或多个此类标记。
若无法判断某条 citation 对应答案中的哪个主张，返回 unknown。
</answer_citation_format>

<judgment_rules>
1. 必须逐个 citation 独立判断，每个输入 block_id 恰好返回一个结果。
2. 只使用输入中的 answer 和 citations，不得使用模型外部知识补充证据。
3. supported：doc_title、section_path 和 text 能够支持答案中与该引用相关的主张。
4. unsupported：证据原文不能支持或明确反驳相关主张，或者答案声称的文档归属与 citation 不符。
5. unknown：无法从答案中确定该引用对应的主张，或仅凭该 citation 无法可靠判断。
6. 一个 citation 如果对应多个相关主张，任一主张被原文明确反驳时判为 unsupported。
7. reason 必须直接说明证据如何支持、反驳或不支持相关主张。
8. evidence_quotes 必须逐字来自该 citation 的 text，不得改写、拼接或跨 block 引用。
</judgment_rules>

<output>
只输出一个 JSON 对象，不输出 Markdown、代码围栏或额外说明。
citations 必须与输入中的 block_id 集合完全一致，不得增加、遗漏或重复。

{
  "citations": [
    {
      "block_id": "block_1",
      "verdict": "supported | unsupported | unknown",
      "reason": "简短且可核验的理由",
      "evidence_quotes": ["逐字引用该 block 的原文"]
    }
  ]
}
</output>
"""

CITATION_SUPPORT_PROMPT_HASH = hashlib.sha256(CITATION_SUPPORT_SYSTEM_PROMPT.encode()).hexdigest()


class CitationEvidenceInput(BaseModel):
    """引用支持检查可见的单块证据。"""

    block_id: str
    doc_title: str
    section_path: list[str]
    text: str


class CitationSupportInput(BaseModel):
    """一次答案的显式引用支持检查输入。"""

    answer: str
    citations: list[CitationEvidenceInput] = Field(default_factory=list)


def render_citation_messages(packet: CitationSupportInput) -> list[ChatMessage]:
    """构造一次最小引用支持检查消息。"""

    return [
        ChatMessage(role="system", content=CITATION_SUPPORT_SYSTEM_PROMPT),
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
