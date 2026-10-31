from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.models import Base, utc_now


class WikiBuildRow(Base):
    """一个绑定 collection snapshot 的不可变 Wiki build。"""

    __tablename__ = "wiki_builds"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    base_build_id: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    counts_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WikiBuildDocumentMapRow(Base):
    """build 内每篇文档对应的局部 topic map。"""

    __tablename__ = "wiki_build_document_maps"

    build_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("wiki_builds.build_id", ondelete="CASCADE"),
        primary_key=True,
    )
    doc_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    topic_map_id: Mapped[str] = mapped_column(String(40), nullable=False)


class WikiTopicRow(Base):
    """一个 build 内的统一 Wiki topic。"""

    __tablename__ = "wiki_topics"

    build_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("wiki_builds.build_id", ondelete="CASCADE"),
        primary_key=True,
    )
    topic_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    semantic_tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content_level: Mapped[str] = mapped_column(String(16), nullable=False)


class WikiTopicAliasRow(Base):
    """build 内 topic 的规范化别名。"""

    __tablename__ = "wiki_topic_aliases"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    normalized_alias: Mapped[str] = mapped_column(Text, primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(40), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)


class WikiTopicEvidenceRow(Base):
    """topic 对一个 snapshot block 的直接来源引用。"""

    __tablename__ = "wiki_topic_evidence"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    block_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(40), nullable=False)
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    relation: Mapped[str] = mapped_column(String(16), nullable=False, default="direct")


class WikiCoverageSlotRow(Base):
    """一个 topic 的语料观察型覆盖槽。"""

    __tablename__ = "wiki_coverage_slots"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slot_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(40), nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)


class WikiSlotEvidenceRow(Base):
    """coverage slot 的 block 证据。"""

    __tablename__ = "wiki_slot_evidence"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slot_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    block_id: Mapped[str] = mapped_column(String(255), primary_key=True)


class WikiTopicVectorRow(Base):
    """topic 搜索文本的 dense 向量投影。"""

    __tablename__ = "wiki_topic_vectors"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    norm: Mapped[float] = mapped_column(Float, nullable=False)


class ActiveWikiPointerRow(Base):
    """单行 Wiki activity pointer，generation 提供原子切换。"""

    __tablename__ = "active_wiki_pointer"

    pointer_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    build_id: Mapped[str] = mapped_column(String(40), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_build_id: Mapped[str | None] = mapped_column(String(40))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class WikiPageRow(Base):
    """一个 topic 当前已物化的结构化页面。"""

    __tablename__ = "wiki_pages"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    page_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(40), nullable=False)
    content_level: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint("build_id", "topic_id", name="uq_wiki_pages_build_topic"),)


class WikiPageParagraphRow(Base):
    """叙述页段落到原文 block 的回源引用。"""

    __tablename__ = "wiki_page_paragraphs"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    page_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source_block_ids_json: Mapped[str] = mapped_column(Text, nullable=False)


class WikiPageLinkRow(Base):
    """页面级导航链接。"""

    __tablename__ = "wiki_page_links"

    build_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    from_topic_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    to_topic_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_block_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
