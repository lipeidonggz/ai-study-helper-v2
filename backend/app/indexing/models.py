from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class EmbeddingProfile(BaseModel):
    """一次索引构建固定的 embedding 供应商与向量契约。"""

    provider: str
    model_id: str
    model_version: str
    dimension: int = Field(gt=0)
    output_type: Literal["dense"]
    text_type: Literal["document"]


class EmbeddingInputPolicy(BaseModel):
    """embedding 文本模板的固定版本和规范 hash。"""

    version: str
    template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class KeywordProfile(BaseModel):
    """关键词 search text、analyzer 与 BM25 引擎的版本化元数据。

    ``bm25_k1`` 和 ``bm25_b`` 只在调用方能够真实覆写引擎时记录数值。
    LanceDB 原生 FTS 不暴露这两个参数，因此以 ``None`` 表示由引擎内部管理，
    不能把未知默认值写成看似已生效的配置。
    """

    engine: str
    engine_version: str
    analyzer: str
    analyzer_version: str
    search_text_policy_version: str
    bm25_k1: float | None
    bm25_b: float | None
    bm25_parameters_source: str


class UsagePreview(BaseModel):
    """调用 embedding API 前可展示的规模预估。"""

    block_count: int = Field(ge=0)
    unique_input_count: int = Field(ge=0)
    batch_count: int = Field(ge=0)


class EmbeddingUsage(BaseModel):
    """一次 S5 构建的实际请求、token 与重试记录。"""

    request_count: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    ambiguous_retry_count: int = Field(ge=0)
    request_ids: list[str] = Field(default_factory=list)


class BlockIndexRecord(BaseModel):
    """一个 block 在候选双索引中的可对账引用。"""

    block_id: str
    block_index: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dense_vector_ref: str


class IndexBundle(BaseModel):
    """S5 输出的不可发布候选索引证书。"""

    index_id: str
    status: Literal["candidate"]
    doc_id: str
    doc_version: int = Field(ge=1)
    block_set_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    embedding_profile: EmbeddingProfile
    embedding_input_policy: EmbeddingInputPolicy
    keyword_profile: KeywordProfile
    block_indexes: list[BlockIndexRecord]
    lancedb_ref: str
    keyword_index_ref: str
    artifact_ref: str
    usage_preview: UsagePreview
    usage: EmbeddingUsage
    created_at: str
    errors: list[str] = Field(default_factory=list)


class IndexFailureEvidence(BaseModel):
    """S5 失败时保留已经发生的 embedding 请求与 token usage。"""

    schema_version: str = "1"
    status: Literal["failed"]
    job_id: str
    doc_id: str
    doc_version: int = Field(ge=1)
    stage: Literal["S5"]
    code: str
    message: str
    retryable: bool
    usage_preview: UsagePreview
    usage: EmbeddingUsage
    created_at: str
