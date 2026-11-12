"""5.1 独立编译流程的边界对象与发布产物模型。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.dual_entry_index.normalization import canonical_hash


class TermDecision(StrEnum):
    """一个候选出现是否构成稳定术语。"""

    ACCEPTED = "accepted"
    NOISE = "noise"
    UNCERTAIN = "uncertain"


class SenseDecision(StrEnum):
    """已接受术语在当前上下文中的义项判断。"""

    RESOLVED = "resolved"
    MENTION_ONLY = "mention_only"
    UNCERTAIN = "uncertain"


class SummaryBasis(StrEnum):
    """概念义项摘要相对于原文的表达方式。"""

    EXPLICIT = "explicit"
    CONTEXT_INFERRED = "context_inferred"


class LinkBasis(StrEnum):
    """术语与概念义项之间可发布的原文依据。"""

    DIRECT_USE = "direct_use"


class ObjectStatus(StrEnum):
    """编译产物对象的当前状态。"""

    ACTIVE = "active"
    RETIRED = "retired"


class SourceBlock(BaseModel):
    """快照中的规范化原文块及其概念归纳所需元数据。"""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    doc_id: str
    doc_version: int
    doc_title: str
    block_id: str
    block_index: int
    block_ordinal: int = Field(ge=0)
    text: str
    content_hash: str = Field(min_length=64, max_length=64)
    section_path: list[str] = Field(default_factory=list)
    source_node_types: tuple[str, ...] = ()
    table_caption: str | None = None
    table_headers: list[str] = Field(default_factory=list)


class DocumentVersionIdentity(BaseModel):
    """编译契约中冻结的一篇文档及其版本。"""

    model_config = ConfigDict(frozen=True)

    doc_id: str
    doc_version: int = Field(ge=1)


class SnapshotData(BaseModel):
    """一次 5.1 编译使用的不可变快照输入。"""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    manifest_hash: str
    root: str
    documents: tuple[DocumentVersionIdentity, ...]
    blocks: tuple[SourceBlock, ...]

    @model_validator(mode="after")
    def validate_unique_block_ids(self) -> SnapshotData:
        """拒绝重复原文块，并校验块没有混入其他快照或文档版本。"""

        block_ids = [block.block_id for block in self.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("snapshot contains duplicate block IDs")
        doc_ids = [document.doc_id for document in self.documents]
        if len(doc_ids) != len(set(doc_ids)):
            raise ValueError("snapshot contains duplicate document versions")
        versions_by_declared_doc = {
            document.doc_id: document.doc_version for document in self.documents
        }
        if any(block.snapshot_id != self.snapshot_id for block in self.blocks):
            raise ValueError("snapshot contains blocks from another snapshot")
        versions_by_doc: dict[str, int] = {}
        for block in self.blocks:
            previous_version = versions_by_doc.setdefault(block.doc_id, block.doc_version)
            if previous_version != block.doc_version:
                raise ValueError("snapshot contains multiple versions of one document")
            if versions_by_declared_doc.get(block.doc_id) != block.doc_version:
                raise ValueError("snapshot block document is not declared by manifest")
        return self


class ScanContract(BaseModel):
    """候选扫描规则和运行条件冻结后的不可变契约。

    发布身份只使用 ``contract_hash``。其余字段只用于解释总哈希由哪些运行条件参与计算。
    """

    model_config = ConfigDict(frozen=True)

    contract_version: str
    implementation_version: str
    jieba_version: str
    nltk_version: str
    chinese_dictionary_sha256: str
    chinese_pos_mapping_version: str
    chinese_boundary_version: str
    english_boundary_version: str
    english_model_filter_version: str
    compound_identifier_version: str
    noise_pattern_version: str
    sentence_boundary_version: str
    normalization_version: str
    contract_hash: str


class CandidateOccurrence(BaseModel):
    """一次位于原文块内的临时候选出现，不直接发布为地图对象。"""

    model_config = ConfigDict(frozen=True)

    occurrence_id: str
    snapshot_id: str
    normalized_key: str
    display_text: str
    doc_id: str
    block_id: str
    block_index: int
    block_ordinal: int
    block_content_hash: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    scan_contract_hash: str
    model_judgement_required: bool = True
    lexical_exclusion_reason: str | None = None

    @model_validator(mode="after")
    def validate_character_range(self) -> CandidateOccurrence:
        """保证候选范围是非空半开区间，并校验词法排除状态一致。"""

        if self.end <= self.start:
            raise ValueError("candidate character range must be non-empty")
        if self.model_judgement_required and self.lexical_exclusion_reason is not None:
            raise ValueError("model-required candidate cannot carry lexical exclusion")
        if not self.model_judgement_required and not self.lexical_exclusion_reason:
            raise ValueError("lexically excluded candidate requires a reason")
        return self


class CandidateGroup(BaseModel):
    """同一原文块内同一规范化词面的模型判断单元。

    候选出现继续保存全部字符范围，用于证据定位和审计；模型只判断这里的一个候选组。
    原文块是局部上下文边界，因此身份同时绑定块内容、规范化词面和扫描契约，不跨块合并。
    """

    model_config = ConfigDict(frozen=True)

    candidate_group_id: str
    snapshot_id: str
    doc_id: str
    block_id: str
    block_index: int
    block_ordinal: int
    block_content_hash: str = Field(min_length=64, max_length=64)
    normalized_key: str
    display_text: str
    occurrence_ids: tuple[str, ...] = Field(min_length=1)
    first_start: int = Field(ge=0)
    scan_contract_hash: str
    model_judgement_required: bool = True
    lexical_exclusion_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_group_state(self) -> CandidateGroup:
        """保证组内身份唯一，并保持模型判断与确定性排除互斥。"""

        if len(self.occurrence_ids) != len(set(self.occurrence_ids)):
            raise ValueError("candidate group occurrence IDs must be unique")
        if self.model_judgement_required and self.lexical_exclusion_reasons:
            raise ValueError("model-required candidate group cannot carry lexical exclusions")
        if not self.model_judgement_required and not self.lexical_exclusion_reasons:
            raise ValueError("lexically excluded candidate group requires a reason")
        return self


class ScanBlockStatus(BaseModel):
    """单个原文块的候选扫描完成状态。"""

    model_config = ConfigDict(frozen=True)

    block_id: str
    content_hash: str
    candidate_count: int = Field(ge=0)
    status: Literal["succeeded", "failed"]
    error_code: str | None = None


class ScanResult(BaseModel):
    """候选扫描的可复现结果与诊断。"""

    model_config = ConfigDict(frozen=True)

    contract: ScanContract
    occurrences: tuple[CandidateOccurrence, ...]
    candidate_groups: tuple[CandidateGroup, ...]
    block_statuses: tuple[ScanBlockStatus, ...]
    excluded_noise_counts: dict[str, int]
    lexical_exclusion_counts: dict[str, int] = Field(default_factory=dict)
    candidate_set_hash: str


class CandidateGroupJudgement(BaseModel):
    """模型对一个原文块内规范化词面候选组的术语和义项判断。

    ``resolved`` 且没有 ``existing_sense_id`` 时，只表示这个候选可以进入新建概念义项
    的归并候选，不表示当前阶段已经生成可发布摘要。摘要统一在义项归并完成后生成。
    """

    model_config = ConfigDict(frozen=True)

    candidate_group_id: str
    term_decision: TermDecision
    sense_decision: SenseDecision
    existing_sense_id: str | None = None
    reason: str

    @model_validator(mode="after")
    def validate_decision_shape(self) -> CandidateGroupJudgement:
        """校验术语判断和义项引用之间的组合关系。"""

        if not self.reason.strip():
            raise ValueError("judgement reason must not be blank")
        if self.term_decision is not TermDecision.ACCEPTED and self.sense_decision is not (
            SenseDecision.UNCERTAIN
        ):
            raise ValueError("non-accepted terms must use uncertain sense decision")
        if self.sense_decision is not SenseDecision.RESOLVED and self.existing_sense_id:
            raise ValueError("unresolved sense cannot carry an existing sense reference")
        return self


class TermRecord(BaseModel):
    """发布产物中的词面检索锚点。"""

    model_config = ConfigDict(frozen=True)

    normalized_key: str
    display_text: str
    support_block_ids: tuple[str, ...]
    status: ObjectStatus = ObjectStatus.ACTIVE


class ConceptSense(BaseModel):
    """发布产物中的独立概念义项。"""

    model_config = ConfigDict(frozen=True)

    concept_sense_id: str
    summary: str
    summary_basis: SummaryBasis
    support_block_ids: tuple[str, ...]
    induction_contract_version: str
    status: ObjectStatus = ObjectStatus.ACTIVE


class TermSenseLink(BaseModel):
    """术语与概念义项之间带原文块依据的多对多关联。"""

    model_config = ConfigDict(frozen=True)

    normalized_key: str
    concept_sense_id: str
    support_block_ids: tuple[str, ...]
    basis_type: LinkBasis
    status: ObjectStatus = ObjectStatus.ACTIVE


class SourceBlockIdentity(BaseModel):
    """用于增量编译判断原文块是否变化的最小身份记录。"""

    model_config = ConfigDict(frozen=True)

    block_id: str
    content_hash: str


class CompileContract(BaseModel):
    """5.1 地图的可复现输入契约。

    ``contract_hash`` 绑定目标快照和完整输入清单；``compatibility_hash`` 只绑定跨快照复用时
    必须一致的规则、模型和上下文装配条件。
    """

    model_config = ConfigDict(frozen=True)

    contract_id: str
    contract_hash: str
    compatibility_hash: str
    contract_version: str
    compiler_version: str
    snapshot_id: str
    snapshot_manifest_hash: str
    document_versions: tuple[DocumentVersionIdentity, ...]
    scan_contract_hash: str
    induction_contract_version: str
    induction_contract_hash: str
    model_id: str
    actual_model_ids: tuple[str, ...] = ()
    term_sense_prompt_version: str
    consolidation_prompt_version: str
    summary_prompt_version: str
    context_assembly_version: str
    max_candidate_groups_per_judgement_request: int = Field(gt=0)
    max_input_chars: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    consolidation_output_tokens_per_draft: int = Field(gt=0)
    max_retries: int = Field(ge=0)

    def compatibility_payload(self) -> dict[str, object]:
        """返回跨快照增量必须一致的规则字段。"""

        return {
            "contract_version": self.contract_version,
            "compiler_version": self.compiler_version,
            "scan_contract_hash": self.scan_contract_hash,
            "induction_contract_version": self.induction_contract_version,
            "induction_contract_hash": self.induction_contract_hash,
            "model_id": self.model_id,
            "term_sense_prompt_version": self.term_sense_prompt_version,
            "consolidation_prompt_version": self.consolidation_prompt_version,
            "summary_prompt_version": self.summary_prompt_version,
            "context_assembly_version": self.context_assembly_version,
            "max_candidate_groups_per_judgement_request": (
                self.max_candidate_groups_per_judgement_request
            ),
            "max_input_chars": self.max_input_chars,
            "max_output_tokens": self.max_output_tokens,
            "consolidation_output_tokens_per_draft": (self.consolidation_output_tokens_per_draft),
            "max_retries": self.max_retries,
        }

    def calculate_compatibility_hash(self) -> str:
        """计算由兼容字段派生的稳定哈希。"""

        return canonical_hash(self.compatibility_payload())

    def calculate_contract_hash(self) -> str:
        """计算包含目标快照和文档版本的完整契约哈希。"""

        return canonical_hash(
            self.model_dump(
                mode="json",
                exclude={"contract_id", "contract_hash"},
            )
        )


class ArtifactDiagnostic(BaseModel):
    """发布产物中可长期保留的结构化诊断摘要。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    normalized_key: str | None = None
    block_id: str | None = None
    occurrence_ids: tuple[str, ...] = ()


class Stage51Artifact(BaseModel):
    """5.1 独立编译生成的不可变概念索引地图。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["dual-entry-stage51-v1"] = "dual-entry-stage51-v1"
    map_id: str
    map_hash: str = ""
    snapshot_id: str
    snapshot_manifest_hash: str
    base_map_id: str | None = None
    compilation_mode: Literal["full", "incremental"]
    compile_contract: CompileContract
    source_blocks: tuple[SourceBlockIdentity, ...]
    terms: tuple[TermRecord, ...]
    term_sense_links: tuple[TermSenseLink, ...]
    concept_senses: tuple[ConceptSense, ...]
    diagnostics: tuple[ArtifactDiagnostic, ...] = ()
    stats: dict[str, int | float | str]

    def calculate_map_hash(self) -> str:
        """计算地图语义、证据闭包和可查询诊断的完整哈希。"""

        return canonical_hash(
            self.model_dump(
                mode="json",
                exclude={"map_id", "map_hash", "stats"},
            )
        )


class MapPublicationRecord(BaseModel):
    """唯一活动地图的原子发布记录。"""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["dual-entry-stage51-publication-v1"] = (
        "dual-entry-stage51-publication-v1"
    )
    map_id: str
    map_hash: str
    compile_contract_id: str
    compile_contract_hash: str
    base_map_id: str | None
    compilation_mode: Literal["full", "incremental"]
    status: Literal["active"]
    published_at: datetime


class DiagnosticRecord(BaseModel):
    """不进入地图身份、供人工检查的详细编译诊断。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class StageMetric(BaseModel):
    """一次编译阶段的耗时、模型调用和缓存用量。"""

    model_config = ConfigDict(frozen=True)

    stage: Literal[
        "scan",
        "term_judgement",
        "consolidation",
        "materialization",
        "total_compile",
    ]
    wall_seconds: float = Field(ge=0)
    requests: int = Field(ge=0)
    attempts: int = Field(ge=0)
    failed_attempts: int = Field(ge=0)
    retry_attempts: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_hit_tokens: int = Field(ge=0)
    cache_miss_tokens: int = Field(ge=0)
    actual_model_ids: tuple[str, ...] = ()
    failure_details: tuple[str, ...] = ()


class CompilationDiagnostics(BaseModel):
    """一次 5.1 编译的候选、模型调用和人工检查数据。"""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    map_id: str
    candidate_occurrence_count: int
    candidate_group_count: int
    block_count: int
    added_block_ids: tuple[str, ...]
    deleted_block_ids: tuple[str, ...]
    rescanned_block_ids: tuple[str, ...]
    skipped_block_ids: tuple[str, ...]
    candidate_group_judgements: tuple[CandidateGroupJudgement, ...]
    llm_requests: int
    llm_attempts: int
    failed_llm_attempts: int
    retry_attempts: int
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    actual_model_ids: tuple[str, ...] = ()
    stage_metrics: tuple[StageMetric, ...]
    records: tuple[DiagnosticRecord, ...]


class TermSenseDraft(BaseModel):
    """全局收敛前或中的临时概念义项分组。

    新候选草稿在全局义项归并前没有摘要；``refresh_summary_required`` 用于表达有效支持集合
    变化后必须重写摘要，即使该草稿没有可与其他草稿归并的成员，也不能跳过模型。
    """

    model_config = ConfigDict(frozen=True)

    draft_id: str
    normalized_keys: tuple[str, ...]
    occurrence_ids: tuple[str, ...]
    support_block_ids: tuple[str, ...]
    summary: str | None = None
    summary_basis: SummaryBasis | None = None
    existing_sense_id: str | None = None
    previous_sense_ids: tuple[str, ...] = ()
    refresh_summary_required: bool = False
    source: Literal["model", "base_map", "consolidation"]

    @model_validator(mode="after")
    def validate_summary_pair(self) -> TermSenseDraft:
        """摘要正文和依据必须同时存在，或者都等待归并阶段生成。"""

        if bool(self.summary) != bool(self.summary_basis):
            raise ValueError("sense draft summary and basis must be present together")
        return self


class ConsolidatedGroup(BaseModel):
    """归并模型给出的一个完整分组。"""

    model_config = ConfigDict(frozen=True)

    draft_ids: tuple[str, ...] = Field(min_length=1)
    summary: str
    summary_basis: SummaryBasis
    reason: str

    @model_validator(mode="after")
    def validate_text_fields(self) -> ConsolidatedGroup:
        """拒绝空白摘要或理由，避免无法审计的归并结果进入物化。"""

        if not self.summary.strip() or not self.reason.strip():
            raise ValueError("consolidated group summary and reason must not be blank")
        return self


class ConsolidationOutput(BaseModel):
    """一组临时义项分组的归并结果。"""

    model_config = ConfigDict(frozen=True)

    groups: tuple[ConsolidatedGroup, ...]
