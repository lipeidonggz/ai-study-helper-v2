from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from app.providers.chat import ChatMessage

CITATION_SUPPORT_PROMPT_VERSION = "citation-rubric-v3"

CITATION_SUPPORT_SYSTEM_PROMPT = """<role>
你是本地文档问答的引用支持检查器。
你每次检查一批答案中的显式引用，逐个判断所引证据是否支持答案局部上下文中的相关主张。
</role>

<inputs>
你只会收到：
1. contexts：答案中的局部语义上下文，可能是段落、列表项、表格行或普通行；
2. citations：答案中显式引用到的证据块；
3. occurrences：一个 context 与一个 citation 的关系。

每个 context 只包含：
- context_id；
- kind；
- text。

每个 citation 只包含：
- block_id；
- doc_title；
- section_path；
- text。

每个 occurrence 只包含：
- occurrence_id；
- block_id；
- context_id。
</inputs>

<answer_citation_format>
答案中的显式引用固定写作：【来源：block_id】。
多个 block 写作：【来源：block_1，block_2】。
引用标记可能位于句子、段落、列表项、表格行或普通行末尾。
context.text 是引用所在的完整局部语义单元，不保证只有一句话。
若无法从 occurrence 对应的 context 判断相关主张，返回 unknown。
</answer_citation_format>

<judgment_rules>
1. 必须逐个 occurrence 独立判断，每个输入 occurrence_id 恰好返回一个结果。
2. 只使用输入中的 contexts、citations 和 occurrences，不得使用模型外部知识补充证据。
3. context 可能包含多句话或多个列表内容；应识别由该 occurrence 引用标记承诺支持的相关主张，
   不能默认只检查紧邻引用前的一句话。
4. supported：该 citation 能支持该 occurrence 对应的全部可识别相关主张。
5. unsupported：证据原文不能支持或明确反驳任一相关主张，或者答案声称的文档归属与 citation 不符。
6. unknown：无法从 context 确定该引用对应的主张，或仅凭该 citation 无法可靠判断。
7. reason 必须直接说明证据如何支持、反驳或不支持相关主张。
8. evidence_quotes 必须逐字来自该 occurrence 对应 citation 的 text，不得改写、拼接或跨 block 引用。
</judgment_rules>

<output>
只输出一个 JSON 对象，不输出 Markdown、代码围栏或额外说明。
citations 必须与输入中的 occurrence_id 集合完全一致，不得增加、遗漏或重复。

{
  "citations": [
    {
      "occurrence_id": "citation_0001",
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


class CitationContextInput(BaseModel):
    """引用所在的答案局部语义上下文。"""

    context_id: str
    kind: Literal["paragraph", "list_item", "table_row", "line"]
    text: str


class CitationOccurrenceInput(BaseModel):
    """一个引用 block 在一个答案上下文中的一次检查关系。"""

    occurrence_id: str
    block_id: str
    context_id: str


class CitationSupportInput(BaseModel):
    """一次有界批次中的引用支持检查输入。"""

    contexts: list[CitationContextInput] = Field(default_factory=list)
    citations: list[CitationEvidenceInput] = Field(default_factory=list)
    occurrences: list[CitationOccurrenceInput] = Field(default_factory=list)


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
