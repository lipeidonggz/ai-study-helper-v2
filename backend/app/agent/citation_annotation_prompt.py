from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.providers.chat import ChatMessage

if TYPE_CHECKING:
    from app.agent.citations import CitationAnnotationInput

CITATION_ANNOTATION_SYSTEM_PROMPT = """<role>
你是 Agent Harness 的后置引用标注器。答案正文已经生成且不可修改；
你的唯一任务是为已列出的答案 spans 选择本次工具真实返回的 evidence blocks。
</role>

<rules>
1. 只能返回输入中已经存在的 span_id 与 block_id。
   block_id 使用 evidence_blocks[].block_id 中的短引用，必须逐字复制，
   不得添加、删除或改写。
2. 不得返回、重写、总结或补全答案正文。
3. 列表项已经由 Harness 冻结为一个完整 claim；列表项内部的分号子句不得再拆分。
   可以结合完整答案理解列表项中的指代，但 annotation 的选择对象始终是 span.text。
   单独的确认词、承接语、标题、过渡句、纯分析、用户建议和无证据内容不要标注。
4. 一个 span 可以由多个 block 联合支持；将同一 span 需要的 block_id 放在同一数组中。
5. block 必须直接支持该 span，不得因为主题相近、共同关键词或同属一篇文档而引用。
6. 同一 span 最多返回一次；block_ids 不得重复。
7. 无法确定时不要为该 span 添加 annotation。
8. 没有可引用内容时返回空 annotations。
9. 每个 annotation 必须给出简短 reason，说明所选 blocks 如何支持该 span；
   不得输出隐藏思维链，只说明可核验的证据对应关系。
10. 当 span.structure_role=body_table_row 时，必须结合答案中的表头识别各列，
    并分别核验每个非空单元格。回答自身对表格的重述不算证据；所选 blocks 的联合内容
    必须覆盖全部非空单元格。来源表格可以转置或重排，不能因为表格形状相似就认为支持。
    reason 必须说明各单元格分别由哪些证据内容支持。
</rules>

<output>
只输出一个 JSON 对象，不得输出 Markdown、代码围栏或额外说明：
{"annotations":[{"span_id":"span_0001","block_ids":["evidence_001"],"reason":"简短依据"}]}
</output>
"""


@dataclass(frozen=True, slots=True)
class CitationAnnotationPrompt:
    """一次后置引用标注使用的不可变提示词版本。"""

    version: str
    system_prompt: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("citation annotation prompt version must not be empty")
        if not self.system_prompt.strip():
            raise ValueError("citation annotation prompt must not be empty")

    @property
    def content_hash(self) -> str:
        """返回系统提示词内容的稳定审计 hash。"""

        return hashlib.sha256(self.system_prompt.encode("utf-8")).hexdigest()


DEFAULT_CITATION_ANNOTATION_PROMPT = CitationAnnotationPrompt(
    version="claim-citation-v7",
    system_prompt=CITATION_ANNOTATION_SYSTEM_PROMPT,
)
CITATION_ANNOTATION_PROMPT_VERSION = DEFAULT_CITATION_ANNOTATION_PROMPT.version
CITATION_ANNOTATION_PROMPT_HASH = DEFAULT_CITATION_ANNOTATION_PROMPT.content_hash


def render_citation_annotation_messages(
    packet: CitationAnnotationInput,
    prompt: CitationAnnotationPrompt = DEFAULT_CITATION_ANNOTATION_PROMPT,
) -> list[ChatMessage]:
    """构造后置引用标注的最小消息集。"""

    payload = packet.model_dump(mode="json")
    for index, evidence_block in enumerate(payload["evidence_blocks"], start=1):
        evidence_block["block_id"] = citation_evidence_reference(index)
    return [
        ChatMessage(role="system", content=prompt.system_prompt),
        ChatMessage(
            role="user",
            content=json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    ]


def citation_evidence_reference(index: int) -> str:
    """返回批次内 evidence block 的稳定短引用。"""

    if index <= 0:
        raise ValueError("evidence reference index must be positive")
    return f"evidence_{index:03d}"
