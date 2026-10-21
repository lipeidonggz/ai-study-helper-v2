from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.ingestion.models import BlockRecord, DocumentLanguage, SectionRecord
from app.tasks.models import BackgroundTaskRecord


class DocumentSourceDetail(BaseModel):
    """详情页展示的来源信息，不暴露 artifact 存放路径。"""

    source_kind: Literal["file", "url"]
    source_uri: str
    final_url: str | None = None
    content_hash: str
    acquisition_mode: str
    charset: str


class DocumentVersionDetail(BaseModel):
    """同一文档的一个已登记版本。"""

    doc_version: int = Field(ge=1)
    title: str
    language: DocumentLanguage
    extractor_name: str
    extractor_version: str
    created_at: str
    is_active: bool


class DocumentTechnicalDetail(BaseModel):
    """折叠展示的解析、切块和索引配置。"""

    extractor_name: str
    extractor_version: str
    section_builder_name: str
    section_builder_version: str
    chunker_name: str
    chunker_version: str
    embedding_model_id: str
    embedding_dimension: int = Field(ge=1)
    extraction_hash: str


class DocumentDetailResponse(BaseModel):
    """当前活动快照中一篇文档的完整只读详情。"""

    snapshot_id: str
    published_at: str
    doc_id: str
    doc_version: int = Field(ge=1)
    title: str
    source_label: str
    language: DocumentLanguage
    source: DocumentSourceDetail
    sections: list[SectionRecord]
    blocks: list[BlockRecord]
    versions: list[DocumentVersionDetail]
    activities: list[BackgroundTaskRecord]
    technical: DocumentTechnicalDetail
    warnings: list[str] = Field(default_factory=list)
