from __future__ import annotations

from pydantic import BaseModel, Field

from app.ingestion.models import DocumentLanguage


class RetrievalErrorPayload(BaseModel):
    """检索工具可返回的公共错误结构。"""

    code: str
    message: str
    retryable: bool = False


class RetrievalDocumentSummary(BaseModel):
    """文档清单中的一篇可定位文档。"""

    doc_id: str
    title: str
    source_label: str
    aliases: list[str] = Field(default_factory=list)
    lang: DocumentLanguage
    sections: int = Field(ge=0)
    blocks: int = Field(ge=0)


class ListDocumentsResult(BaseModel):
    """`list_documents` 的领域结果。"""

    documents: list[RetrievalDocumentSummary]
    total: int = Field(ge=0)


class RetrievalOutlineSection(BaseModel):
    """章节大纲中的一个稳定章节节点。"""

    path: list[str]
    ordinal: str
    title: str
    depth: int = Field(ge=0)
    own_blocks: int = Field(ge=0)
    own_context_tokens: int = Field(ge=0)
    subtree_blocks: int = Field(ge=0)
    subtree_context_tokens: int = Field(ge=0)
    first_line: str


class RetrievalDocumentOutline(BaseModel):
    """一篇文档的章节大纲。"""

    doc_id: str
    doc_title: str
    source_label: str
    sections: list[RetrievalOutlineSection]


class RetrievalOutlineItem(BaseModel):
    """`get_outline` 对单个输入 doc_id 的成功或失败结果。"""

    doc_id: str
    document: RetrievalDocumentOutline | None = None
    error: RetrievalErrorPayload | None = None


class GetOutlineResult(BaseModel):
    """`get_outline` 的领域结果。"""

    documents: list[RetrievalOutlineItem]


class RetrievalBlock(BaseModel):
    """提供给检索上层的完整证据块。"""

    block_id: str
    doc_id: str
    doc_title: str
    source_label: str
    section_path: list[str]
    text: str


class ListBlocksResult(BaseModel):
    """一篇文档在指定章节范围内的完整块枚举。"""

    blocks: list[RetrievalBlock]
    total: int = Field(ge=0)


class ScanMatch(BaseModel):
    """字面扫描命中的一个块。"""

    block_id: str
    doc_id: str
    doc_title: str
    source_label: str
    section_path: list[str]
    snippet: str


class ScanTermResult(BaseModel):
    """字面扫描的全部命中块与命中数。"""

    term: str
    matches: list[ScanMatch]
    count: int = Field(ge=0)
