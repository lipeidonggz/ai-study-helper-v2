from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.wiki.models import WikiProviderProfile


class TopicCoverageSlotDraft(BaseModel):
    """模型在单个证据窗口内观察到的一个主题检查面。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    question: str = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)


class TopicCandidateDraft(BaseModel):
    """模型返回的统一主题候选，不包含硬实体／概念分类。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    semantic_tags: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(min_length=1)
    coverage_slots: list[TopicCoverageSlotDraft] = Field(default_factory=list)


class TopicExtractionResponse(BaseModel):
    """单个文档窗口的严格抽取输出。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[TopicCandidateDraft]


class TopicCoverageSlot(BaseModel):
    """topic map 中一个已校验的主题覆盖槽。"""

    slot_id: str = Field(pattern=r"^ws_[0-9a-f]{16}$")
    label: str
    question: str
    source_block_ids: list[str] = Field(min_length=1)


class WikiCandidate(BaseModel):
    """文档局部、尚待跨文档归一的主题候选。"""

    candidate_id: str = Field(pattern=r"^wc_[0-9a-f]{16}$")
    doc_id: str
    doc_version: int = Field(ge=1)
    doc_title: str
    name: str
    normalized_name: str
    aliases: list[str]
    summary: str
    semantic_tags: list[str]
    source_block_ids: list[str] = Field(min_length=1)
    coverage_slots: list[TopicCoverageSlot]


class TopicMapDocument(BaseModel):
    """一篇文档的局部 topic map。"""

    doc_id: str
    doc_version: int = Field(ge=1)
    doc_title: str
    source_label: str
    windows: int = Field(ge=0)
    processed_block_ids: list[str]
    candidates: list[WikiCandidate]


class TopicMapUsage(BaseModel):
    """一个文档窗口的实际模型 usage。"""

    doc_id: str
    window_index: int = Field(ge=0)
    request_id: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class WikiTopicMapManifest(BaseModel):
    """一个不可变 snapshot topic map 的完整审计清单。"""

    schema_version: Literal["topic-map-v1"] = "topic-map-v1"
    map_id: str = Field(pattern=r"^tm_[0-9a-f]{16}$")
    snapshot_id: str
    snapshot_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    compiler_version: str
    prompt_version: str
    prompt_template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: WikiProviderProfile
    granularity: Literal["focused", "standard", "exhaustive"]
    block_window_size: int = Field(ge=1)
    block_window_chars: int = Field(ge=1)
    reused_from_map_id: str | None = None
    documents: list[TopicMapDocument]
    usage: list[TopicMapUsage]
    candidate_count: int = Field(ge=0)
    block_count: int = Field(ge=0)
    created_at: str
    manifest_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")


class WikiTopicMapResult(BaseModel):
    """topic map 编译结果。"""

    manifest: WikiTopicMapManifest
    documents: list[TopicMapDocument]


class TopicIdentityMergeRequest(BaseModel):
    """身份归一对之间发给模型的严格输入。"""

    model_config = ConfigDict(extra="forbid")

    candidate_groups: list[TopicIdentityGroup]


class TopicIdentityGroup(BaseModel):
    """一组名称／别名完全相连的候选。"""

    group_id: str = Field(pattern=r"^wg_[0-9a-f]{16}$")
    names: list[str] = Field(min_length=1)
    aliases: list[str]
    summary: str
    evidence_count: int = Field(ge=1)
    candidate_compare_groups: list[TopicIdentityCompareGroup] = Field(default_factory=list)


class TopicIdentityCompareGroup(BaseModel):
    """供模型判断是否同一身份的少量近邻组。"""

    group_id: str = Field(pattern=r"^wg_[0-9a-f]{16}$")
    names: list[str]
    aliases: list[str]
    summary: str


class TopicIdentityMergeResponse(BaseModel):
    """模型只允许用给定组 ID 表达合并。"""

    model_config = ConfigDict(extra="forbid")

    merges: dict[str, str] = Field(default_factory=dict)


class WikiTopicLedgerEntry(BaseModel):
    """身份归一后的统一主题台账条目。"""

    topic_id: str = Field(pattern=r"^wt_[0-9a-f]{16}$")
    title: str
    canonical_name: str
    normalized_name: str
    slug: str
    aliases: list[str]
    summary: str
    semantic_tags: list[str]
    source_doc_ids: list[str]
    evidence_block_ids: list[str] = Field(min_length=1)
    coverage_slots: list[TopicCoverageSlot]
    candidate_ids: list[str] = Field(min_length=1)


class WikiTopicLedgerManifest(BaseModel):
    """多篇 topic maps 归一后的不可变主题台账。"""

    schema_version: Literal["topic-ledger-v1"] = "topic-ledger-v1"
    ledger_id: str = Field(pattern=r"^tl_[0-9a-f]{16}$")
    snapshot_id: str
    snapshot_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    topic_map_ids: list[str] = Field(min_length=1)
    base_ledger_id: str | None = None
    resolver_version: str
    prompt_version: str
    prompt_template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: WikiProviderProfile
    topics: list[WikiTopicLedgerEntry]
    candidate_count: int = Field(ge=0)
    unresolved_merge_count: int = Field(ge=0)
    created_at: str
    manifest_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")


class WikiTopicLedgerResult(BaseModel):
    """topic ledger 编译结果。"""

    manifest: WikiTopicLedgerManifest
    topics: list[WikiTopicLedgerEntry]


TopicIdentityGroup.model_rebuild()
TopicIdentityMergeRequest.model_rebuild()
