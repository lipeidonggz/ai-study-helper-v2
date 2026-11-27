from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.profile_extraction.util import normalize_name

ProfileStatus = Literal["draft", "frozen", "conflicted"]
PreviewRunStatus = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
ChecklistPriority = Literal["must", "should", "nice_to_have", "noise"]
ComparisonOutcome = Literal["a_better", "b_better", "equivalent", "both_insufficient"]
CandidateAuditVerdict = Literal["clean", "suspicious", "uncertain"]
CandidateAuditStatus = Literal["not_requested", "clean", "suspicious", "uncertain"]
CandidateNameScopeVerdict = Literal["clear", "ambiguous", "uncertain"]
CandidateNameMeaningVerdict = Literal["aligned", "misaligned", "uncertain"]
CandidateTitleKind = Literal[
    "proper_name",
    "acronym",
    "common_concept",
    "relation",
    "principle",
    "method",
    "other",
]
CandidateTitleAnchorType = Literal["actor", "system", "product", "scope"]
DocumentAnchorStatus = Literal[
    "not_needed",
    "completed",
    "over_budget",
    "candidate_limit_exceeded",
    "truncated_output",
    "invalid_json",
    "invalid_schema",
    "failed",
]
CandidateSemanticAuditStatus = Literal[
    "disabled",
    "pending",
    "running",
    "completed",
    "partial",
    "failed",
    "interrupted",
]
OMISSION_RETRY_FIELD_ALIASES = AliasChoices(
    "omission_retry_enabled",
    "coverage_audit_enabled",
)


class ExtractionChannel(BaseModel):
    """一个职责单一的候选 discovery 通道配置。"""

    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(pattern=r"^ec_[0-9a-f]{16}$")
    name: str = Field(default="", max_length=120)
    purpose: str = Field(default="", max_length=1_000)
    activation: str = Field(default="", max_length=1_000)
    include_criteria: list[str] = Field(default_factory=list, max_length=20)
    exclude_criteria: list[str] = Field(default_factory=list, max_length=20)
    granularity_rule: str = Field(default="", max_length=1_000)
    omission_retry_enabled: bool = Field(
        default=False,
        validation_alias=OMISSION_RETRY_FIELD_ALIASES,
    )


class ProfileLanguagePolicy(BaseModel):
    """Profile 固定的候选名称与摘要语言策略。"""

    model_config = ConfigDict(extra="forbid")

    name: Literal["preserve_source_term"] = "preserve_source_term"
    summary: Literal["zh-CN"] = "zh-CN"


class ExtractionProfileConfig(BaseModel):
    """可由用户编辑、并通过确定性规则编译为 Prompt 的 Profile 配置。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1_000)
    domain_context: str = Field(default="", max_length=1_000)
    primary_uses: list[str] = Field(default_factory=list, max_length=10)
    representative_questions: list[str] = Field(default_factory=list, max_length=20)
    extraction_channels: list[ExtractionChannel] = Field(default_factory=list, max_length=8)
    exclude_criteria: list[str] = Field(default_factory=list, max_length=20)
    granularity: Literal["standard", "fine", "coarse"] = "standard"
    additional_guidance: str = Field(default="", max_length=1_000)
    language_policy: ProfileLanguagePolicy = Field(default_factory=ProfileLanguagePolicy)

    @model_validator(mode="after")
    def validate_channel_ids(self) -> ExtractionProfileConfig:
        """拒绝同一 Profile 内重复的通道 ID。"""

        channel_ids = [channel.channel_id for channel in self.extraction_channels]
        if len(channel_ids) != len(set(channel_ids)):
            raise ValueError("extraction channel IDs must be unique")
        return self


class ExtractionProfile(BaseModel):
    """Profile API 的完整持久化视图。"""

    profile_id: str
    config: ExtractionProfileConfig
    status: ProfileStatus
    is_system: bool
    created_from_profile_id: str | None = None
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    updated_at: str
    frozen_at: str | None = None


class ProfileListResponse(BaseModel):
    """Profile 列表和当前选择。"""

    profiles: list[ExtractionProfile]
    selected_profile_id: str | None = None


class ProfileCreateRequest(BaseModel):
    """创建空白 Profile 或从已有 Profile 克隆 draft。"""

    name: str = Field(default="", max_length=120)
    clone_from_profile_id: str | None = None


class ProfileSaveRequest(BaseModel):
    """全量替换一个 draft Profile 的可编辑配置。"""

    config: ExtractionProfileConfig


class ProfileCloneRequest(BaseModel):
    """从已有 Profile 克隆一个新 draft。"""

    name: str | None = Field(default=None, max_length=120)


class ProfileValidationIssue(BaseModel):
    """一个可用于定位 Profile 冲突或未完成字段的问题。"""

    code: str
    message: str
    channel_id: str | None = None
    field: str | None = None


class ProfileValidationResponse(BaseModel):
    """Profile 的确定性校验结果；语义冲突由 LLM 建议进一步补充。"""

    valid: bool
    issues: list[ProfileValidationIssue]


class ProfileSelectionResponse(BaseModel):
    """当前知识库选择的冻结 Profile。"""

    selected_profile_id: str | None = None
    selected_profile_hash: str | None = None
    selected_at: str | None = None


class StrategySuggestionRequest(BaseModel):
    """根据非 Profile 专属语义信息生成通道草案。"""

    profile_name: str = Field(min_length=1, max_length=120)
    primary_uses: list[str] = Field(min_length=1, max_length=10)
    representative_questions: list[str] = Field(default_factory=list, max_length=20)
    domain_context: str = Field(default="", max_length=1_000)


class ChannelAttributeSuggestionRequest(BaseModel):
    """根据 Profile 上下文和单个通道的名称、目的补全其余通道属性。"""

    profile_name: str = Field(default="", max_length=120)
    primary_uses: list[str] = Field(default_factory=list, max_length=10)
    representative_questions: list[str] = Field(default_factory=list, max_length=20)
    domain_context: str = Field(default="", max_length=1_000)
    channel_name: str = Field(min_length=1, max_length=120)
    channel_purpose: str = Field(min_length=1, max_length=1_000)


class SuggestedChannelAttributes(BaseModel):
    """模型返回的单通道属性草案，不包含名称、目的或通道 ID。"""

    model_config = ConfigDict(extra="forbid")

    activation: str = Field(min_length=1, max_length=1_000)
    include_criteria: list[str] = Field(default_factory=list, max_length=20)
    exclude_criteria: list[str] = Field(default_factory=list, max_length=20)
    granularity_rule: str = Field(min_length=1, max_length=1_000)
    omission_retry_enabled: bool = Field(
        default=False,
        validation_alias=OMISSION_RETRY_FIELD_ALIASES,
    )


class ChannelAttributeSuggestionResponse(SuggestedChannelAttributes):
    """单通道属性建议；不会自动保存或覆盖当前草稿。"""

    request_id: str


class SuggestedChannel(BaseModel):
    """模型返回的通道草案，不包含服务端生成的稳定 ID。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    purpose: str = Field(min_length=1, max_length=1_000)
    activation: str = Field(min_length=1, max_length=1_000)
    include_criteria: list[str] = Field(default_factory=list, max_length=20)
    exclude_criteria: list[str] = Field(default_factory=list, max_length=20)
    granularity_rule: str = Field(min_length=1, max_length=1_000)
    omission_retry_enabled: bool = Field(
        default=False,
        validation_alias=OMISSION_RETRY_FIELD_ALIASES,
    )


class SuggestedStrategyPayload(BaseModel):
    """模型返回的可编辑 Profile 策略草案。"""

    model_config = ConfigDict(extra="forbid")

    channels: list[SuggestedChannel] = Field(min_length=1, max_length=8)
    exclude_criteria: list[str] = Field(default_factory=list, max_length=20)


class StrategySuggestionResponse(BaseModel):
    """模型生成的通道草案；不会自动保存。"""

    channels: list[ExtractionChannel]
    exclude_criteria: list[str]
    request_id: str


def _normalize_summary_values(values: list[str]) -> list[str]:
    """Remove empty and normalized-duplicate summaries while preserving order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(value.split())
        if not text:
            continue
        key = normalize_name(text)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(text)
    if not normalized:
        raise ValueError("candidate must contain at least one non-empty summary")
    return normalized


class CandidateDraft(BaseModel):
    """模型返回并由当前 snapshot evidence 校验的候选。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    summaries: list[str] = Field(min_length=1)
    selection_reason: str = Field(default="", max_length=1_000)
    source_block_ids: list[str] = Field(min_length=1)
    existing_candidate_ref: str | None = Field(
        default=None,
        pattern=r"^ca_[0-9a-f]{16}$",
    )

    @field_validator("summaries")
    @classmethod
    def normalize_summaries(cls, values: list[str]) -> list[str]:
        """Require a non-empty, normalized-deduplicated sibling summary set."""

        return _normalize_summary_values(values)


class CandidateExtractionResponse(BaseModel):
    """一个通道在一个窗口内允许返回的唯一结构。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateDraft]


class ExistingCandidateContext(BaseModel):
    """后续通道可见的已积累候选账本条目。"""

    model_config = ConfigDict(extra="forbid")

    candidate_ref: str = Field(pattern=r"^ca_[0-9a-f]{16}$")
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    summaries: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(default_factory=list)
    channel_ids: list[str] = Field(default_factory=list)


class CandidateNameScopeAuditDecision(BaseModel):
    """一个主标题在当前知识库命名空间中的独立可定位性结论。"""

    model_config = ConfigDict(extra="forbid")

    candidate_ref: str = Field(pattern=r"^ca_[0-9a-f]{16}$")
    verdict: CandidateNameScopeVerdict
    scope_anchor: str | None = Field(default=None, max_length=200)
    suggested_name: str | None = Field(default=None, max_length=300)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_suggested_name(self) -> CandidateNameScopeAuditDecision:
        """要求问题结论提供建议名称，并禁止 clear 携带建议。"""

        if self.verdict == "ambiguous" and not self.suggested_name:
            raise ValueError("ambiguous name audit requires suggested_name")
        if self.verdict != "ambiguous" and self.suggested_name is not None:
            raise ValueError("only ambiguous name audit can include suggested_name")
        return self


class CandidateNameScopeAuditPayload(BaseModel):
    """一次知识库级标题审查必须覆盖全部输入主标题。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CandidateNameScopeAuditDecision]


class CandidateNameMeaningAuditDecision(BaseModel):
    """一个主标题及其别名是否锚定本地 summary 中心对象的结论。"""

    model_config = ConfigDict(extra="forbid")

    candidate_ref: str = Field(pattern=r"^ca_[0-9a-f]{16}$")
    verdict: CandidateNameMeaningVerdict
    suggested_name: str | None = Field(default=None, max_length=300)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_suggested_name(self) -> CandidateNameMeaningAuditDecision:
        """要求含义不一致时提供建议名称，并禁止一致结论携带建议。"""

        if self.verdict == "misaligned" and not self.suggested_name:
            raise ValueError("misaligned name meaning audit requires suggested_name")
        if self.verdict != "misaligned" and self.suggested_name is not None:
            raise ValueError("only misaligned name meaning audit can include suggested_name")
        return self


class CandidateNameMeaningAuditPayload(BaseModel):
    """一次知识库级名称含义审查必须覆盖全部输入候选。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CandidateNameMeaningAuditDecision]


class CandidateDocumentAnchorDecision(BaseModel):
    """整篇文档上下文对候选标题执行消歧后的结构化结果。"""

    model_config = ConfigDict(extra="forbid")

    candidate_ref: str = Field(pattern=r"^ca_[0-9a-f]{16}$")
    provisional_name: str = Field(min_length=1, max_length=300)
    title_kind: CandidateTitleKind
    standalone_clear: bool
    missing_anchor_types: list[CandidateTitleAnchorType] = Field(
        default_factory=list,
        max_length=4,
    )
    final_name: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    anchor_source_block_ids: list[str] = Field(min_length=1, max_length=10)
    reason: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_disambiguation_decision(self) -> CandidateDocumentAnchorDecision:
        """禁止“不清楚但名称不变”和“已经清楚却仍需改名”的矛盾输出。"""

        provisional_normalized = " ".join(self.provisional_name.casefold().split())
        final_normalized = " ".join(self.final_name.casefold().split())
        if self.standalone_clear:
            if final_normalized != provisional_normalized:
                raise ValueError("clear provisional title cannot be renamed")
            if self.missing_anchor_types:
                raise ValueError("clear provisional title cannot require anchors")
        else:
            if final_normalized == provisional_normalized:
                raise ValueError("unclear provisional title must be renamed")
            if not self.missing_anchor_types:
                raise ValueError("unclear provisional title must identify missing anchor types")
        return self


class CandidateDocumentAnchorPayload(BaseModel):
    """一次文档级标题消歧必须覆盖请求中的全部候选。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CandidateDocumentAnchorDecision]


class CandidateAuditIssue(BaseModel):
    """LLM 候选审查允许返回的受控问题类型。"""

    code: Literal[
        "member_or_component_as_alias",
        "alias_not_same_object",
        "unsupported_summary",
        "not_independent_object",
        "granularity_mismatch",
        "reason_not_grounded",
        "other",
    ]
    message: str = Field(min_length=1, max_length=1_000)
    alias: str | None = Field(default=None, max_length=200)


class CandidateSemanticAuditDecision(BaseModel):
    """LLM 对一个窗口候选给出的审查结论。"""

    model_config = ConfigDict(extra="forbid")

    candidate_ref: str = Field(pattern=r"^ca_[0-9a-f]{16}$")
    verdict: CandidateAuditVerdict
    issues: list[CandidateAuditIssue] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=1_000)
    evidence_block_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_clean_has_no_issues(self) -> CandidateSemanticAuditDecision:
        """防止 clean 结论同时携带问题项。"""

        if self.verdict == "clean" and self.issues:
            raise ValueError("clean candidate audit decision cannot include issues")
        return self


class CandidateSemanticAuditPayload(BaseModel):
    """一次批量候选审查必须覆盖请求中的全部候选。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CandidateSemanticAuditDecision]


class CandidateAuditFinding(BaseModel):
    """供候选界面展示的确定性或 LLM 审查疑点。"""

    code: str
    message: str
    alias: str | None = None
    conflicting_candidate_id: str | None = None
    evidence_block_ids: list[str] = Field(default_factory=list)


class PreviewRunRequest(BaseModel):
    """启动或显式复用一次 Profile Preview。"""

    profile_id: str
    snapshot_id: str | None = None
    document_ids: list[str] = Field(min_length=1)
    execution_mode: Literal["reuse_if_exact", "force_new"] = "reuse_if_exact"
    semantic_audit_enabled: bool = False


class PreviewRunPlanRequest(BaseModel):
    """启动 Preview 前计算文档范围、调用量和复用候选。"""

    profile_id: str
    snapshot_id: str | None = None
    document_ids: list[str] = Field(min_length=1)
    execution_mode: Literal["reuse_if_exact", "force_new"] = "reuse_if_exact"
    semantic_audit_enabled: bool = False


class PreviewRunPlan(BaseModel):
    """Preview 启动前的确定性计划和粗略成本提示。"""

    snapshot_id: str
    profile_id: str
    profile_hash: str
    document_ids: list[str]
    block_count: int = Field(ge=0)
    window_count: int = Field(ge=0)
    estimated_extraction_calls: int = Field(ge=0)
    estimated_document_anchor_calls: int = Field(ge=0)
    estimated_document_anchor_input_tokens: int = Field(ge=0)
    estimated_document_anchor_output_tokens: int = Field(ge=0)
    estimated_semantic_audit_calls: int = Field(ge=0)
    estimated_name_scope_audit_calls: int = Field(ge=0)
    estimated_name_meaning_audit_calls: int = Field(ge=0)
    estimated_model_calls: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    estimated_total_tokens: int = Field(ge=0)
    document_anchor_soft_input_tokens: int = Field(ge=1)
    document_anchor_hard_input_tokens: int = Field(ge=1)
    document_anchor_max_candidates: int = Field(ge=1)
    reusable_run_id: str | None = None
    provider_ready: bool


class CandidateSelectionReason(BaseModel):
    """一个通道窗口对候选为何满足当前规则的简短、可追溯说明。"""

    channel_id: str
    operation: Literal["discovery", "omission_retry"]
    window_index: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1_000)
    source_block_ids: list[str] = Field(min_length=1)

    @field_validator("operation", mode="before")
    @classmethod
    def normalize_legacy_operation(cls, value: object) -> object:
        """把历史持久化值转换为当前的补抽阶段名称。"""

        return "omission_retry" if value == "coverage_audit" else value


class PreviewCandidate(BaseModel):
    """一个文档级 Profile 比较候选。"""

    candidate_id: str
    doc_id: str
    doc_version: int = Field(ge=1)
    ordinal: int = Field(ge=0)
    name: str
    normalized_name: str
    provisional_name: str | None = None
    title_kind: CandidateTitleKind | None = None
    title_standalone_clear: bool | None = None
    missing_anchor_types: list[CandidateTitleAnchorType] = Field(default_factory=list)
    name_changed: bool = False
    aliases: list[str]
    summaries: list[str]
    selection_reasons: list[CandidateSelectionReason]
    name_anchor_source_block_ids: list[str] = Field(default_factory=list)
    name_anchor_reason: str | None = None
    audit_status: CandidateAuditStatus = "not_requested"
    audit_reason: str | None = None
    audit_findings: list[CandidateAuditFinding] = Field(default_factory=list)
    source_block_ids: list[str]
    evidence_blocks: list[dict[str, object]]
    channel_ids: list[str]
    content_hash: str

    @field_validator("summaries")
    @classmethod
    def normalize_summaries(cls, values: list[str]) -> list[str]:
        """Require a non-empty, normalized-deduplicated sibling summary set."""

        return _normalize_summary_values(values)


class PreviewChannelDefinition(BaseModel):
    """Run 创建时冻结、用于解释候选来源的通道判断标准。"""

    name: str
    activation: str
    include_criteria: list[str]
    exclude_criteria: list[str]


class PreviewDocumentAnchorResult(BaseModel):
    """一个文档的标题消歧预算、覆盖和失败状态。"""

    status: DocumentAnchorStatus
    candidate_count: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    output_token_budget: int = Field(ge=0)
    soft_input_tokens: int = Field(ge=1)
    hard_input_tokens: int = Field(ge=1)
    error: str | None = None
    request_id: str | None = None
    actual_input_tokens: int = Field(default=0, ge=0)
    actual_output_tokens: int = Field(default=0, ge=0)
    finish_reason: str | None = None
    returned_chars: int = Field(default=0, ge=0)


class PreviewRunSummary(BaseModel):
    """Preview Run 的状态与可追溯信息。"""

    run_id: str
    snapshot_id: str
    snapshot_manifest_hash: str
    profile_id: str
    profile_hash: str
    profile_name: str
    channel_names: dict[str, str] = Field(default_factory=dict)
    channel_definitions: dict[str, PreviewChannelDefinition] = Field(default_factory=dict)
    semantic_audit_enabled: bool = False
    semantic_audit_prompt_version: str | None = None
    name_scope_audit_prompt_version: str | None = None
    name_meaning_audit_prompt_version: str | None = None
    document_anchor_prompt_version: str | None = None
    document_anchor_policy_version: str | None = None
    document_anchor_results: dict[str, PreviewDocumentAnchorResult] = Field(default_factory=dict)
    semantic_audit_status: CandidateSemanticAuditStatus = "disabled"
    semantic_audit_error: str | None = None
    document_scope: list[str]
    status: PreviewRunStatus
    preview_fingerprint: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    error_code: str | None
    error_message: str | None
    usage: list[dict[str, object]]
    diagnostics: list[dict[str, object]]


class PreviewRunDetail(PreviewRunSummary):
    """Preview Run 及其正式候选；非 completed run 的 candidates 为空。"""

    candidates: list[PreviewCandidate] = Field(default_factory=list)
    candidates_total: int = Field(default=0, ge=0)
    reused: bool = False


class PreviewCandidatePage(BaseModel):
    """按文档分页读取的 Preview 候选。"""

    candidates: list[PreviewCandidate]
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)


class EvidenceTranslationRequest(BaseModel):
    """请求翻译一次试抽取中的指定证据块。"""

    block_ids: list[str] = Field(min_length=1, max_length=500)


class EvidenceTranslationPayloadItem(BaseModel):
    """模型返回的单个证据块中文翻译。"""

    model_config = ConfigDict(extra="forbid")

    block_id: str = Field(min_length=1)
    translated_text: str = Field(min_length=1, max_length=20_000)


class EvidenceTranslationPayload(BaseModel):
    """模型返回的一批证据块中文翻译。"""

    model_config = ConfigDict(extra="forbid")

    translations: list[EvidenceTranslationPayloadItem] = Field(min_length=1)


class EvidenceTranslationItem(BaseModel):
    """一个证据块的可展示中文翻译及缓存复用状态。"""

    block_id: str
    translated_text: str
    reused: bool


class EvidenceTranslationResponse(BaseModel):
    """一次证据翻译请求的结果；原文不会被修改。"""

    items: list[EvidenceTranslationItem]
    translated_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    request_ids: list[str]


class DocumentChecklistItem(BaseModel):
    """文档清单中的期望项或噪声项。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(pattern=r"^dci_[0-9a-f]{16}$")
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    priority: ChecklistPriority
    source_block_ids: list[str] = Field(min_length=1)
    origin: Literal["system_suggested", "from_preview", "manual"]
    origin_profile_id: str | None = None
    origin_preview_run_id: str | None = None


class DocumentChecklist(BaseModel):
    """绑定文档版本和 snapshot 的 draft 或 frozen 清单。"""

    checklist_id: str
    snapshot_id: str
    doc_id: str
    doc_version: int = Field(ge=1)
    status: Literal["draft", "frozen"]
    evaluation_note: str
    items: list[DocumentChecklistItem]
    checklist_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    updated_at: str
    frozen_at: str | None = None


class ChecklistCreateRequest(BaseModel):
    """从指定 snapshot 文档创建空清单 draft。"""

    snapshot_id: str | None = None
    doc_id: str
    evaluation_note: str = Field(default="", max_length=1_000)


class ChecklistSaveRequest(BaseModel):
    """全量保存一个 draft 清单。"""

    evaluation_note: str = Field(default="", max_length=1_000)
    items: list[DocumentChecklistItem]


class ChecklistSuggestRequest(BaseModel):
    """从文档本身生成非 Profile 专属清单建议。"""

    snapshot_id: str | None = None
    doc_id: str
    evaluation_note: str = Field(default="", max_length=1_000)


class ChecklistSuggestItem(BaseModel):
    """模型返回的清单建议项，可被用户编辑后保存。"""

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    priority: ChecklistPriority
    source_block_ids: list[str] = Field(min_length=1)


class ChecklistSuggestionPayload(BaseModel):
    """模型返回且尚未分配 item ID 的清单建议。"""

    model_config = ConfigDict(extra="forbid")

    items: list[ChecklistSuggestItem]


class ChecklistSuggestResponse(BaseModel):
    """系统生成的清单建议，不自动写入任何清单。"""

    items: list[ChecklistSuggestItem]
    request_ids: list[str]


class ProfileComparisonCreateRequest(BaseModel):
    """用两个 completed Preview Run 和冻结清单创建比较。"""

    run_a_id: str
    run_b_id: str
    checklist_ids: list[str] = Field(min_length=1)


class ComparisonItemDecision(BaseModel):
    """用户对 uncertain 清单项的人工决定。"""

    item_id: str
    profile_side: Literal["a", "b"]
    decision: Literal["matched", "missing"]
    candidate_id: str | None = None
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_candidate_for_match(self) -> ComparisonItemDecision:
        """matched 决定必须指出具体 candidate。"""

        if self.decision == "matched" and not self.candidate_id:
            raise ValueError("matched decision requires candidate_id")
        return self


class ProfileComparisonItemDecisionRequest(BaseModel):
    """批量提交一个比较中的 uncertain 项决定。"""

    decisions: list[ComparisonItemDecision] = Field(min_length=1)


class ProfileComparisonConcludeRequest(BaseModel):
    """提交最终 Profile 比较结论。"""

    outcome: ComparisonOutcome
    outcome_note: str = Field(default="", max_length=2_000)
    must_override_reasons: dict[str, str] = Field(default_factory=dict)
    document_outcomes: dict[str, ComparisonOutcome] = Field(default_factory=dict)


class ProfileComparison(BaseModel):
    """一个可追溯的双 Profile 比较多文档结果。"""

    comparison_id: str
    snapshot_id: str
    run_a_id: str
    run_b_id: str
    profile_a_id: str
    profile_b_id: str
    checklist_refs: list[dict[str, str]]
    status: Literal["draft", "concluded"]
    document_results: list[dict[str, object]]
    outcome: ComparisonOutcome | None = None
    outcome_note: str
    comparison_hash: str
    created_at: str
    completed_at: str | None = None
