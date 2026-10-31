from __future__ import annotations

import hashlib
import json

from app.providers.chat import ChatMessage
from app.retrieval.models import RetrievalBlock
from app.wiki.models import WikiPageSpec

WIKI_PAGE_PROMPT_VERSION = "wiki-page-v1"

_SYSTEM_PROMPT = """你是知识库 Wiki 的单页编译器。你的任务是把给定证据块整理成一个可导航的主题页面。

必须遵守：
1. 只能使用用户消息中的 evidence_blocks，不得引入外部知识或猜测。
2. 每个段落必须给出 source_block_ids，且只能使用 evidence_blocks 中出现的 block_id。
3. 页面正文直接写证据支持的内容；不要输出成员清单来代替主题组织，不要虚构关系或结论。
4. 如果证据不足以形成页面，返回 {"paragraphs": []} 让程序显式失败。
5. 只返回 JSON 对象，结构必须是：
   {"paragraphs":[{"text":"...","source_block_ids":["..."]}]}
6. 不得增加结构外字段，不得用 Markdown 代码围栏包裹 JSON。"""

WIKI_PAGE_PROMPT_HASH = hashlib.sha256(_SYSTEM_PROMPT.encode("utf-8")).hexdigest()


def build_page_messages(
    page: WikiPageSpec,
    blocks: list[RetrievalBlock],
) -> list[ChatMessage]:
    """构造只包含调用方指定证据面的通用单页编译提示。"""

    payload = {
        "page": {
            "title": page.title,
            "aliases": page.aliases,
            "coverage_questions": page.coverage_questions,
        },
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
        ChatMessage(role="system", content=_SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        ),
    ]
