from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DocumentLanguage = Literal["zh", "en", "mixed", "unknown"]


class IngestPreviewRequest(BaseModel):
    source_kind: Literal["file", "url"]
    source_value: str = Field(min_length=1)


class SourceArtifact(BaseModel):
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
    name: str
    version: str
    config_hash: str | None = None


class TitleSource(BaseModel):
    kind: Literal["heading", "metadata", "filename"]
    node_ordinal: int | None = None


class ListNode(BaseModel):
    text: str
    level: int


class SemanticNode(BaseModel):
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
    node_count: int
    character_count: int
    heading_count: int


class ExtractedDocument(BaseModel):
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
    builder: ComponentVersion
    tree_hash: str
    sections: list[SectionRecord]
    warnings: list[str]


class TableContext(BaseModel):
    caption: str | None = None
    headers: list[str]
    group_index: int
    group_count: int


class BlockRecord(BaseModel):
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
    section_path: list[str]
    own_blocks: int
    subtree_blocks: int
    own_context_tokens: int
    subtree_context_tokens: int
    first_line: str


class BlockSet(BaseModel):
    chunker: ComponentVersion
    chunk_sizing: dict[str, int | float | str]
    canonical_body_text: str
    blocks: list[BlockRecord]
    warnings: list[str]
    section_stats: list[SectionStats]


class PreviewDocument(BaseModel):
    preview_doc_id: str
    title: str
    source_label: str
    language: DocumentLanguage
    aliases: list[str] = Field(default_factory=list)


class IngestPreviewResponse(BaseModel):
    job_id: str
    source: SourceArtifact
    document: PreviewDocument
    extracted: ExtractedDocument
    section_tree: SectionTree
    block_set: BlockSet
    warnings: list[str]
