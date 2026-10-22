from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""

    return datetime.now(UTC)


def utc_iso(value: datetime) -> str:
    """把持久化时间输出为带明确 UTC offset 的 ISO 字符串。

    SQLite 不保存时区信息，SQLAlchemy 读回 ``DateTime(timezone=True)`` 时可能得到
    不带 ``tzinfo`` 的 ``datetime``。系统写入时间始终使用 UTC，因此在存储边界按 UTC
    补齐缺失时区，避免浏览器把 UTC 数值误判为用户本地时间。
    """

    normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return normalized.astimezone(UTC).isoformat()


class Base(DeclarativeBase):
    """SQLAlchemy 主数据库声明基类。"""

    pass


class SourceArtifactRow(Base):
    """原始来源 artifact 的持久元数据。"""

    __tablename__ = "source_artifacts"

    artifact_ref: Mapped[str] = mapped_column(String(512), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    source_key: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    raw_byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    fetched_at: Mapped[str] = mapped_column(String(40), nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    acquisition_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    charset: Mapped[str] = mapped_column(String(64), nullable=False)
    warnings_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class DocumentRow(Base):
    """稳定文档身份和当前展示字段。"""

    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    current_doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_label: Mapped[str] = mapped_column(Text, nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class DocumentVersionRow(Base):
    """文档内容版本及其候选派生物引用。"""

    __tablename__ = "document_versions"

    doc_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("documents.doc_id", ondelete="CASCADE"),
        primary_key=True,
    )
    doc_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    extraction_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_artifact_ref: Mapped[str] = mapped_column(
        String(512),
        ForeignKey("source_artifacts.artifact_ref"),
        nullable=False,
    )
    extracted_document_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    section_tree_ref: Mapped[str | None] = mapped_column(String(512))
    block_set_ref: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_label: Mapped[str] = mapped_column(Text, nullable=False)
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    extractor_name: Mapped[str] = mapped_column(String(128), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    __table_args__ = (Index("ix_document_versions_doc_hash", "doc_id", "extraction_hash"),)


class IngestionJobRow(Base):
    """可查询的入库 job 状态与最后阶段。"""

    __tablename__ = "ingestion_jobs"

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_kind: Mapped[str] = mapped_column(String(8), nullable=False)
    source_value: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    doc_id: Mapped[str | None] = mapped_column(
        String(40),
        ForeignKey("documents.doc_id"),
    )
    doc_version: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )
