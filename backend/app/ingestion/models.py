from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DocumentLanguage = Literal["zh", "en", "mixed", "unknown"]


class IngestPreviewRequest(BaseModel):
    """预览接口输入：一个本地文件路径或公开 URL。"""

    source_kind: Literal["file", "url"]
    source_value: str = Field(min_length=1)


class PersistIngestRequest(BaseModel):
    """正式保存请求：引用一个已完成的预览 job。"""

    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class SourceArtifact(BaseModel):
    """S0 留痕后的不可变来源描述。"""

    source_kind: Literal["file", "url"]
    source_key: str
    source_uri: str
    media_type: str
    raw_artifact_ref: str
    content_hash: str
    raw_byte_length: int
    fetched_at: str
    final_url: str | None = None
    http_status: int | None = None
    acquisition_mode: Literal["file", "http"]
    charset: str
    warnings: list[str] = Field(default_factory=list)


class ComponentVersion(BaseModel):
    """参与确定性构建的组件名称与版本。"""

    name: str
    version: str
    config_hash: str | None = None


class TitleSource(BaseModel):
    """候选标题来源及其可回查节点。"""

    kind: Literal["heading", "metadata", "filename"]
    node_ordinal: int | None = None


class ListNode(BaseModel):
    """列表项文本与嵌套层级。"""

    text: str
    level: int


class SemanticNode(BaseModel):
    """HTML 提取后按原文顺序排列的一个语义节点。"""

    ordinal: int
    kind: Literal[
        "heading",
        "paragraph",
        "list",
        "quote",
        "code",
        "table",
        "figure",
    ]
    text: str
    heading_level: int | None = None
    list_ordered: bool | None = None
    list_items: list[ListNode] | None = None
    table_rows: list[list[str]] | None = None
    table_caption: str | None = None
    code_language: str | None = None
    image_alt: str | None = None
    source_locator: str


class ExtractionStats(BaseModel):
    """单篇提取结果的规模统计。"""

    node_count: int
    character_count: int
    heading_count: int


class ExtractedDocument(BaseModel):
    """S1 输出的格式中立文档中间表示。"""

    source_artifact_ref: str
    effective_content_ref: str
    extractor: ComponentVersion
    extraction_hash: str
    title_candidate: str
    title_source: TitleSource
    site_name_candidate: str | None = None
    language: DocumentLanguage
    nodes: list[SemanticNode]
    warnings: list[str]
    stats: ExtractionStats


class SectionRecord(BaseModel):
    """章节树中的一条扁平章节记录。"""

    path: list[str]
    parent_path: list[str]
    title: str
    heading_level: int | None
    depth: int
    ordinal: str
    order_index: int
    heading_node_ordinal: int | None
    own_node_ordinals: list[int]
    first_line: str


class SectionTree(BaseModel):
    """S3 输出的稳定章节结构与节点归属。"""

    doc_id: str
    doc_version: int
    builder: ComponentVersion
    tree_hash: str
    sections: list[SectionRecord]
    warnings: list[str]


class TableContext(BaseModel):
    """拆分表格时保留的表题、表头和行组上下文。"""

    caption: str | None = None
    headers: list[str]
    group_index: int
    group_count: int


class BlockRecord(BaseModel):
    """可检索、可引用、可确定回溯的单个证据块。"""

    block_id: str
    section_path: list[str]
    block_index: int
    language: DocumentLanguage
    text: str
    char_start: int
    char_end: int
    content_hash: str
    source_node_ordinals: list[int]
    table_context: TableContext | None = None
    embedding_hints: dict[str, str] = Field(default_factory=dict)
    context_token_estimate: int
    estimator_version: str


class SectionStats(BaseModel):
    """按章节聚合的直属与子树块数和近似 token。"""

    section_path: list[str]
    own_blocks: int
    subtree_blocks: int
    own_context_tokens: int
    subtree_context_tokens: int
    first_line: str


class BlockSet(BaseModel):
    """S4 输出的完整 block 集合与语义 hash。"""

    doc_id: str
    doc_version: int
    block_set_hash: str
    chunker: ComponentVersion
    chunk_sizing: dict[str, int | float | str]
    canonical_body_text: str
    blocks: list[BlockRecord]
    warnings: list[str]
    section_stats: list[SectionStats]


class PreviewDocument(BaseModel):
    """预览接口使用的临时文档展示信息。"""

    preview_doc_id: str
    title: str
    source_label: str
    language: DocumentLanguage
    aliases: list[str] = Field(default_factory=list)


class IngestPreviewResponse(BaseModel):
    """HTML 预览接口的完整响应。"""

    job_id: str
    source: SourceArtifact
    document: PreviewDocument
    extracted: ExtractedDocument
    section_tree: SectionTree
    block_set: BlockSet
    warnings: list[str]


class DocumentRecord(BaseModel):
    """持久化的稳定文档身份与当前展示信息。"""

    doc_id: str
    source_key: str
    source_kind: Literal["file", "url"]
    current_doc_version: int
    title: str
    source_label: str
    aliases: list[str] = Field(default_factory=list)
    language: DocumentLanguage
    created_at: str
    updated_at: str


class DocumentVersionRecord(BaseModel):
    """同一文档下不可覆盖的一次内容版本记录。"""

    doc_id: str
    doc_version: int
    extraction_hash: str
    source_artifact_ref: str
    extracted_document_ref: str
    section_tree_ref: str | None = None
    block_set_ref: str | None = None
    title: str
    source_label: str
    aliases: list[str] = Field(default_factory=list)
    language: DocumentLanguage
    extractor_name: str
    extractor_version: str
    created_at: str


class IngestDocumentResponse(BaseModel):
    """正式保存接口的文档、版本和派生产物响应。"""

    job_id: str
    source: SourceArtifact
    document: DocumentRecord
    version: DocumentVersionRecord
    extracted: ExtractedDocument
    section_tree: SectionTree
    block_set: BlockSet
    warnings: list[str]
    is_new_document: bool
    is_new_version: bool
