from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.indexing.models import EmbeddingInputPolicy, EmbeddingProfile, KeywordProfile
from app.ingestion.models import ComponentVersion

SnapshotStatus = Literal["building", "ready", "ready_partial", "failed"]
SnapshotCompleteness = Literal["complete", "partial"]


class SnapshotArtifact(BaseModel):
    """快照内可校验的一份文档 artifact。"""

    kind: Literal[
        "source",
        "source_metadata",
        "extracted",
        "section_tree",
        "block_set",
        "index_bundle",
        "document",
    ]
    snapshot_ref: str
    source_ref: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)


class SnapshotVersions(BaseModel):
    """collection 级固定版本组；成功文档必须共享这组版本。"""

    extractor: ComponentVersion
    section_builder: ComponentVersion
    chunker: ComponentVersion
    sizing_estimator: str
    embedding_profile: EmbeddingProfile
    embedding_input_policy: EmbeddingInputPolicy
    keyword_profile: KeywordProfile
    index_schema: str


class SnapshotDocument(BaseModel):
    """manifest 中一个完整成功的文档版本。"""

    doc_id: str
    doc_version: int = Field(ge=1)
    title: str
    source_label: str
    section_count: int = Field(ge=0)
    block_count: int = Field(ge=0)
    table_block_count: int = Field(ge=0)
    embedded_block_count: int = Field(ge=0)
    keyword_indexed_block_count: int = Field(ge=0)
    artifact_refs: list[SnapshotArtifact]


class SnapshotFailure(BaseModel):
    """manifest 中一个失败的输入文档。"""

    job_id: str
    doc_id: str | None = None
    doc_version: int | None = None
    stage: str
    code: str
    message: str
    retryable: bool = False


class SnapshotTotals(BaseModel):
    """snapshot 内成功文档和索引的汇总。"""

    documents: int = Field(ge=0)
    sections: int = Field(ge=0)
    blocks: int = Field(ge=0)
    table_blocks: int = Field(ge=0)
    embedding_requests: int = Field(ge=0)
    embedding_input_tokens: int = Field(ge=0)


class CandidateSnapshotManifest(BaseModel):
    """S6 的不可发布／可发布候选快照构建证书。"""

    manifest_version: Literal["s6-v1"] = "s6-v1"
    snapshot_id: str
    parent_snapshot_id: str | None = None
    status: SnapshotStatus
    completeness: SnapshotCompleteness
    created_at: str
    versions: SnapshotVersions | None = None
    documents: list[SnapshotDocument] = Field(default_factory=list)
    failed_documents: list[SnapshotFailure] = Field(default_factory=list)
    totals: SnapshotTotals
    index_ref: str | None = None
    keyword_index_ref: str | None = None
    manifest_hash: str = Field(default="", pattern=r"^([0-9a-f]{64})?$")


class ActiveCollectionPointer(BaseModel):
    """当前活动 collection 的唯一事实源。"""

    snapshot_id: str
    manifest_hash: str
    generation: int = Field(ge=1)
    previous_snapshot_id: str | None = None
    published_at: str


class PublishRecord(BaseModel):
    """一次发布尝试的审计记录。"""

    publish_id: str
    from_snapshot_id: str | None
    to_snapshot_id: str | None
    expected_generation: int = Field(ge=0)
    result: Literal["committed", "aborted"]
    reason: str | None = None
    started_at: str
    finished_at: str | None = None
    verification: dict[str, object] = Field(default_factory=dict)


class PublishCollectionRequest(BaseModel):
    """内部 API 的候选 collection 发布请求。"""

    job_ids: list[str] = Field(min_length=1)


class RollbackCollectionRequest(BaseModel):
    """内部 API 的 collection 回滚请求。"""

    expected_generation: int | None = Field(default=None, ge=1)


class PublishCollectionResponse(BaseModel):
    """一次 S6 构建和发布的结果。"""

    manifest: CandidateSnapshotManifest
    pointer: ActiveCollectionPointer | None
    publish_record: PublishRecord


class CurrentCollectionResponse(BaseModel):
    """当前活动指针及其已验证 manifest。"""

    pointer: ActiveCollectionPointer
    manifest: CandidateSnapshotManifest
