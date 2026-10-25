from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field

from app.providers.chat import ChatMessage

CITATION_SUPPORT_PROMPT_VERSION = "citation-rubric-v4"

CITATION_SUPPORT_SYSTEM_PROMPT = """<role>
你是本地文档问答的引用支持检查器。
你每次检查一批 claim 与证据的引用支持关系，逐个判断所引证据是否支持对应主张。
</role>

<inputs>
你只会收到：
1. contexts：Harness 切出的 claim，或历史答案中的局部语义上下文；
2. citations：本次 occurrence 对应的真实工具证据块；
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

<context_source>
1. 新 attempt 由 Harness 提供 context.kind=claim；
   context.text 是引用标注器选中并由原始答案精确校验的 claim。
2. 历史 attempt 可能由后端从 inline 标记【来源：block_id】回退生成
   paragraph、list_item、table_row 或 line context。
3. citation 的 block_id、标题、section_path 和原文只来自该 attempt 的真实工具结果。
4. 若无法从 occurrence 对应的 context 判断相关主张，返回 unknown。
</context_source>

<judgment_rules>
1. 必须逐个 occurrence 独立判断，每个输入 occurrence_id 恰好返回一个结果。
2. 只使用输入中的 contexts、citations 和 occurrences，不得使用模型外部知识补充证据。
3. context.kind=claim 时，context.text 是 Harness 已冻结并精确切出的单一主张范围；只检查这段文本，
   不得扩展到前后句或同段其他内容。
4. context.kind 为其他值时，context 可能包含多句话或多个列表内容；应识别由该 occurrence
   引用标记承诺支持的相关主张，不能默认只检查紧邻引用前的一句话。
5. supported：该 citation 能支持该 occurrence 对应的全部可识别相关主张。
6. unsupported：证据原文不能支持或明确反驳任一相关主张，或者答案声称的文档归属与 citation 不符。
7. unknown：无法从 context 确定该引用对应的主张，或仅凭该 citation 无法可靠判断。
8. reason 必须直接说明证据如何支持、反驳或不支持相关主张。
9. evidence_quotes 必须逐字来自该 occurrence 对应 citation 的 text，不得改写、拼接或跨 block 引用。
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
    kind: Literal["claim", "paragraph", "list_item", "table_row", "line"]
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
