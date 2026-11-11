from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.storage.models import Base, utc_now


class ExtractionProfileRow(Base):
    """用户可配置的抽取 Profile；draft 可变，冻结后不可原地修改。"""

    __tablename__ = "extraction_profiles"

    profile_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    domain_context: Mapped[str] = mapped_column(Text, nullable=False, default="")
    primary_uses_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    representative_questions_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )
    extraction_channels_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )
    exclude_criteria_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    granularity: Mapped[str] = mapped_column(String(32), nullable=False, default="standard")
    additional_guidance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_from_profile_id: Mapped[str | None] = mapped_column(String(40))
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
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
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_extraction_profiles_status", "status"),
        Index("ix_extraction_profiles_system", "is_system"),
    )


class KnowledgeBaseProfileSelectionRow(Base):
    """单知识库当前选定的冻结 Profile；本表不保存 Profile 状态。"""

    __tablename__ = "knowledge_base_profile_selection"

    knowledge_base_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    selected_profile_id: Mapped[str | None] = mapped_column(
        String(40),
        ForeignKey("extraction_profiles.profile_id"),
    )
    selected_profile_hash: Mapped[str | None] = mapped_column(String(64))
    selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProfilePreviewRunRow(Base):
    """一次不可变 Profile Preview 的执行状态、配置快照和诊断。"""

    __tablename__ = "profile_preview_runs"

    run_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    snapshot_manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_config_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    document_scope_json: Mapped[str] = mapped_column(Text, nullable=False)
    document_versions_json: Mapped[str] = mapped_column(Text, nullable=False)
    document_hashes_json: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    name_normalization_version: Mapped[str] = mapped_column(String(64), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_profile_json: Mapped[str] = mapped_column(Text, nullable=False)
    window_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    window_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    execution_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    preview_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    semantic_audit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    semantic_audit_prompt_version: Mapped[str | None] = mapped_column(String(64))
    name_scope_audit_prompt_version: Mapped[str | None] = mapped_column(String(64))
    name_meaning_audit_prompt_version: Mapped[str | None] = mapped_column(String(64))
    document_anchor_prompt_version: Mapped[str | None] = mapped_column(String(64))
    document_anchor_policy_version: Mapped[str | None] = mapped_column(String(64))
    document_anchor_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    semantic_audit_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="disabled",
    )
    semantic_audit_error: Mapped[str | None] = mapped_column(Text)
    semantic_audit_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    usage_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    diagnostics_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_profile_preview_runs_fingerprint_status",
            "preview_fingerprint",
            "status",
        ),
        Index("ix_profile_preview_runs_created", "created_at"),
    )


class PreviewCandidateRow(Base):
    """Profile Preview 中经过文档级精确去重的最终候选。"""

    __tablename__ = "preview_candidates"

    candidate_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("profile_preview_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    doc_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    provisional_name: Mapped[str | None] = mapped_column(Text)
    title_kind: Mapped[str | None] = mapped_column(String(32))
    title_standalone_clear: Mapped[bool | None] = mapped_column(Boolean)
    missing_anchor_types_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )
    name_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    aliases_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summaries_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    selection_reasons_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    name_anchor_source_block_ids_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="[]",
    )
    name_anchor_reason: Mapped[str | None] = mapped_column(Text)
    source_block_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_blocks_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    channel_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    __table_args__ = (
        Index(
            "ix_preview_candidates_run_doc_name",
            "run_id",
            "doc_id",
            "normalized_name",
        ),
    )


class EvidenceTranslationRow(Base):
    """按原文内容、模型和 Prompt 版本缓存的证据中文翻译。"""

    __tablename__ = "evidence_translations"

    translation_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_text_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class DocumentChecklistRow(Base):
    """绑定文档版本与 snapshot 的局部判断清单。"""

    __tablename__ = "document_checklists"

    checklist_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    doc_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    doc_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    evaluation_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    items_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    checklist_hash: Mapped[str] = mapped_column(String(64), nullable=False)
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
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProfileComparisonRow(Base):
    """两个 completed Preview Run 在冻结文档清单上的不可变比较。"""

    __tablename__ = "profile_comparisons"

    comparison_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_a_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("profile_preview_runs.run_id"),
        nullable=False,
    )
    run_b_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("profile_preview_runs.run_id"),
        nullable=False,
    )
    profile_a_id: Mapped[str] = mapped_column(String(40), nullable=False)
    profile_b_id: Mapped[str] = mapped_column(String(40), nullable=False)
    checklist_refs_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    document_results_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    outcome: Mapped[str | None] = mapped_column(String(32))
    outcome_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comparison_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_profile_comparisons_runs", "run_a_id", "run_b_id"),)
