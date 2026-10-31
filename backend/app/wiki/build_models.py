from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.retrieval.models import RetrievalBlock, RetrievalErrorPayload

WikiContentLevel = Literal["catalog", "narrative"]
WikiMatchSource = Literal["title", "alias", "fts", "dense"]


class WikiBuildCounts(BaseModel):
    """一个 Wiki build 的主题、来源和内容等级统计。"""

    topics: int = Field(ge=0)
    catalog_only: int = Field(ge=0)
    narrative: int = Field(ge=0)
    evidence_refs: int = Field(ge=0)
    coverage_slots: int = Field(ge=0)


class WikiBuildResult(BaseModel):
    """一次 Wiki topic build 的公开结果。"""

    build_id: str = Field(pattern=r"^wb_[0-9a-f]{16}$")
    snapshot_id: str
    snapshot_manifest_hash: str
    base_build_id: str | None
    status: Literal["ready"]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_id: str
    topic_map_ids: list[str]
    counts: WikiBuildCounts
    created_at: str
    published_at: str


class WikiCoverageSlotView(BaseModel):
    """搜索或页面读取时展示的一个覆盖槽。"""

    slot_id: str
    label: str
    question: str
    source_block_ids: list[str]


class WikiTopicSearchItem(BaseModel):
    """Wiki 主题搜索返回的轻量入口，不携带完整证据和覆盖问题。"""

    topic_id: str
    title: str
    slug: str
    summary: str
    matched_aliases: list[str] = Field(default_factory=list)
    semantic_tags: list[str] = Field(default_factory=list)
    content_level: WikiContentLevel
    source_doc_count: int = Field(ge=0)
    source_doc_titles: list[str]
    evidence_count: int = Field(ge=1)
    coverage_slot_labels: list[str]
    coverage_slot_count: int = Field(ge=0)
    match_sources: list[WikiMatchSource]
    dense_score: float | None = None


class WikiTopicDetail(BaseModel):
    """单个 Wiki 主题的完整元数据、证据引用和覆盖槽。"""

    topic_id: str
    title: str
    slug: str
    summary: str
    semantic_tags: list[str] = Field(default_factory=list)
    content_level: WikiContentLevel
    source_doc_ids: list[str]
    evidence_block_ids: list[str]
    coverage_slots: list[WikiCoverageSlotView]


class WikiTopicSearchResult(BaseModel):
    """绑定固定 snapshot 与 Wiki build 的主题搜索结果。"""

    snapshot_id: str
    build_id: str
    topics: list[WikiTopicSearchItem]
    total: int = Field(ge=0)
    dense_profile: dict[str, object]


class WikiTopicEvidenceGroup(BaseModel):
    """一个 topic 以及它的全部直接证据块。"""

    topic_id: str
    title: str
    blocks: list[RetrievalBlock]


class WikiTopicEvidenceResult(BaseModel):
    """一个或多个 topic 的完整直接证据。"""

    snapshot_id: str
    build_id: str
    topics: list[WikiTopicEvidenceGroup]


class WikiNarrativeParagraph(BaseModel):
    """叙述页中可独立回源的一个段落。"""

    text: str = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)


class WikiNarrativeSection(BaseModel):
    """叙述页的一个结构化章节。"""

    heading: str = Field(min_length=1)
    paragraphs: list[WikiNarrativeParagraph] = Field(min_length=1)


class WikiNarrativePageDraft(BaseModel):
    """叙述页模型唯一允许返回的草稿结构。"""

    summary: str
    sections: list[WikiNarrativeSection]
    related_topic_ids: list[str] = Field(default_factory=list)


class WikiNarrativePage(BaseModel):
    """一个 topic 的完整结构化叙述页。"""

    schema_version: Literal["wiki-page-v2"] = "wiki-page-v2"
    page_id: str = Field(pattern=r"^wp_[0-9a-f]{16}$")
    topic_id: str
    title: str
    aliases: list[str]
    summary: str = Field(min_length=1)
    content_level: Literal["catalog", "narrative"]
    sections: list[WikiNarrativeSection]
    related_topic_ids: list[str]
    artifact_ref: str = ""
    artifact_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")


class WikiPageReadResult(BaseModel):
    """读取一个 topic 页面时的统一结果。"""

    snapshot_id: str
    build_id: str
    page: WikiNarrativePage


class WikiPagesResult(BaseModel):
    """一次读取一个或多个 topic 页面的结果。"""

    snapshot_id: str
    build_id: str
    pages: list[WikiNarrativePage]


class WikiNarrativeMaterializeResult(BaseModel):
    """一次 narrative materialize 的页面和失败统计。"""

    snapshot_id: str
    build_id: str
    pages: list[WikiNarrativePage]
    catalog_only_topic_ids: list[str]


class WikiCompilationResult(BaseModel):
    """一次全量或增量 Wiki 编译的公开摘要。"""

    snapshot_id: str
    build: WikiBuildResult
    ledger_id: str
    topic_map_ids: list[str]
    changed_document_ids: list[str]
    removed_document_ids: list[str]
    affected_topic_ids: list[str]
    materialized_pages: int = Field(ge=0)
    copied_pages: int = Field(ge=0)


class WikiToolErrorResult(BaseModel):
    """Wiki tool 的公共错误 payload。"""

    error: RetrievalErrorPayload
