from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    coverage_audit_enabled: bool = False


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
    coverage_audit_enabled: bool = False


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
    coverage_audit_enabled: bool = False


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


class CandidateDraft(BaseModel):
    """模型返回并由当前 snapshot evidence 校验的候选。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)


class CandidateExtractionResponse(BaseModel):
    """一个通道在一个窗口内允许返回的唯一结构。"""

    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateDraft]


class PreviewRunRequest(BaseModel):
    """启动或显式复用一次 Profile Preview。"""

    profile_id: str
    snapshot_id: str | None = None
    document_ids: list[str] = Field(min_length=1)
    execution_mode: Literal["reuse_if_exact", "force_new"] = "reuse_if_exact"


class PreviewRunPlanRequest(BaseModel):
    """启动 Preview 前计算文档范围、调用量和复用候选。"""

    profile_id: str
    snapshot_id: str | None = None
    document_ids: list[str] = Field(min_length=1)
    execution_mode: Literal["reuse_if_exact", "force_new"] = "reuse_if_exact"


class PreviewRunPlan(BaseModel):
    """Preview 启动前的确定性计划和粗略成本提示。"""

    snapshot_id: str
    profile_id: str
    profile_hash: str
    document_ids: list[str]
    block_count: int = Field(ge=0)
    window_count: int = Field(ge=0)
    estimated_model_calls: int = Field(ge=0)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    estimated_total_tokens: int = Field(ge=0)
    reusable_run_id: str | None = None
    provider_ready: bool


class PreviewCandidate(BaseModel):
    """一个文档级 Profile 比较候选。"""

    candidate_id: str
    doc_id: str
    doc_version: int = Field(ge=1)
    ordinal: int = Field(ge=0)
    name: str
    normalized_name: str
    aliases: list[str]
    summary: str
    summary_variants: list[str]
    source_block_ids: list[str]
    evidence_blocks: list[dict[str, object]]
    channel_ids: list[str]
    content_hash: str


class PreviewRunSummary(BaseModel):
    """Preview Run 的状态与可追溯信息。"""

    run_id: str
    snapshot_id: str
    snapshot_manifest_hash: str
    profile_id: str
    profile_hash: str
    profile_name: str
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
