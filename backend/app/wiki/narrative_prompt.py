from __future__ import annotations

import hashlib
import json

from app.providers.chat import ChatMessage
from app.retrieval.models import RetrievalBlock
from app.wiki.build_models import WikiCoverageSlotView

NARRATIVE_PAGE_PROMPT_VERSION = "wiki-narrative-page-v1"

_NARRATIVE_SYSTEM_PROMPT = """你是知识库 Wiki 的单主题叙述页编译器。

必须遵守：
1. 只能使用用户消息中的 evidence_blocks，不得加入外部知识或猜测。
2. 每个段落必须给出 source_block_ids，且只能使用 evidence_blocks 中出现的 block_id。
3. 段落应贴近原文事实，允许去重、轻度重排和连接，不得为文风扩写，不得虚构关系。
4. coverage_slots 只用于提醒需要覆盖的语料内检查面；证据不足时不要补造内容。
5. related_topic_ids 只能使用 allowed_related_topic_ids 中的值，不得发明 topic ID。
6. 若证据不足以形成叙述页，返回 {"summary":"","sections":[],"related_topic_ids":[]}。
7. 只返回 JSON 对象，不得增加结构外字段，不得使用 Markdown 代码围栏。

输出结构：
{
  "summary": "一句话摘要",
  "sections": [
    {
      "heading": "章节标题",
      "paragraphs": [
        {"text": "由证据支持的段落", "source_block_ids": ["..."]}
      ]
    }
  ],
  "related_topic_ids": ["..."]
}"""

NARRATIVE_PAGE_PROMPT_HASH = hashlib.sha256(_NARRATIVE_SYSTEM_PROMPT.encode()).hexdigest()


def build_narrative_page_messages(
    *,
    topic_id: str,
    title: str,
    aliases: list[str],
    coverage_slots: list[WikiCoverageSlotView],
    related_topics: list[dict[str, str]],
    blocks: list[RetrievalBlock],
) -> list[ChatMessage]:
    """构造不含用例专属规则的通用叙述页提示。"""

    payload = {
        "topic": {
            "topic_id": topic_id,
            "title": title,
            "aliases": aliases,
            "coverage_slots": [
                {
                    "slot_id": slot.slot_id,
                    "label": slot.label,
                    "question": slot.question,
                }
                for slot in coverage_slots
            ],
        },
        "allowed_related_topic_ids": related_topics,
        "evidence_blocks": [
            {
                "block_id": block.block_id,
                "doc_title": block.doc_title,
                "section_path": block.section_path,
                "text": block.text,
            }
            for block in blocks
        ],
    }
    return [
        ChatMessage(role="system", content=_NARRATIVE_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ),
    ]
