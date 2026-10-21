from __future__ import annotations

import hashlib
import json
import unicodedata

from pydantic import BaseModel

from app.indexing.models import EmbeddingInputPolicy
from app.ingestion.errors import IngestFailure
from app.ingestion.models import BlockRecord

EMBEDDING_INPUT_POLICY_VERSION = "embedding-input-v1"


class EmbeddingInput(BaseModel):
    """绑定 block 身份和实际送入 embedding 模型的文本。"""

    block_id: str
    input_hash: str
    text: str


_TEMPLATE_FIELDS = [
    "文档标题：{document_title}",
    "章节：{section_path}",
    "{context_lines}",
    "正文：",
    "{block_text}",
]


def _template_hash() -> str:
    """对字段顺序和模板字面量计算稳定 hash，不混入单次正文。"""

    payload = json.dumps(
        {
            "version": EMBEDDING_INPUT_POLICY_VERSION,
            "fields": _TEMPLATE_FIELDS,
            "section_path_separator": " > ",
            "empty_section_label": "根章节",
            "context_key_order": "sorted",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


EMBEDDING_INPUT_POLICY = EmbeddingInputPolicy(
    version=EMBEDDING_INPUT_POLICY_VERSION,
    template_hash=_template_hash(),
)


def _has_visible_text(text: str) -> bool:
    """拒绝空白和 Unicode 控制／格式字符，不把它们当作合法正文。"""

    for character in text:
        category = unicodedata.category(character)
        if not character.isspace() and not category.startswith("C"):
            return True
    return False


def _context_lines(block: BlockRecord) -> list[str]:
    """按固定顺序生成表格与代码提示，不改变 block 原文。"""

    lines: list[str] = []
    table = block.table_context
    if table is not None:
        caption = table.caption or "未命名"
        headers = " | ".join(table.headers) if table.headers else "无"
        lines.append(
            f"表格：{caption}；表头：{headers}；行组：{table.group_index}/{table.group_count}"
        )
    for key in sorted(block.embedding_hints):
        value = block.embedding_hints[key]
        if key == "code_language":
            lines.append(f"代码语言：{value}")
        else:
            lines.append(f"{key}：{value}")
    return lines


def build_embedding_inputs(
    document_title: str,
    blocks: list[BlockRecord],
) -> tuple[EmbeddingInputPolicy, list[EmbeddingInput]]:
    """按固定模板为每个 block 构造 embedding 输入并计算内容 hash。"""

    title = unicodedata.normalize("NFC", document_title.strip())
    if not title:
        raise IngestFailure(
            "embedding_document_title_empty",
            "document title is required for embedding input",
            422,
        )

    inputs: list[EmbeddingInput] = []
    for block in blocks:
        if not _has_visible_text(block.text):
            raise IngestFailure(
                "embedding_input_empty",
                f"block {block.block_id} has no visible text for embedding",
                422,
            )
        section_path = " > ".join(block.section_path) or "根章节"
        context_lines = _context_lines(block)
        fields = [
            f"文档标题：{title}",
            f"章节：{section_path}",
            *context_lines,
            "正文：",
            block.text,
        ]
        input_text = "\n".join(fields)
        inputs.append(
            EmbeddingInput(
                block_id=block.block_id,
                input_hash=hashlib.sha256(input_text.encode("utf-8")).hexdigest(),
                text=input_text,
            )
        )
    return EMBEDDING_INPUT_POLICY, inputs
