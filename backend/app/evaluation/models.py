from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

ReviewStatus = Literal["draft", "reviewed", "active", "retired"]
RunType = Literal["quality", "latency"]
RunStatus = Literal[
    "planned",
    "running",
    "paused",
    "canceled",
    "sealed_complete",
    "sealed_partial",
    "invalid_plan",
]


class CriterionSeed(BaseModel):
    """从设计 03 解析出的一条 R／X 判定项。"""

    criterion_id: str
    revision_id: str
    rule_type: Literal["R", "X"]
    ordinal: int
    title: str
    rule_text: str
    evaluator: Literal["checker", "llm_judge", "human"] = "human"


class CaseSeed(BaseModel):
    """一条可幂等导入的已审核用例种子。"""

    case_id: str
    revision_id: str
    category: str
    question: str
    target_bindings: list[dict[str, Any]]
    distractor_metadata: dict[str, Any] = Field(default_factory=dict)
    n: int = 10
    k: int = 9
    execution_policy: dict[str, Any] = Field(default_factory=lambda: {"latency_class": "complex"})
    source_ref: str
    content_hash: str
    criteria: list[CriterionSeed]
    review_status: ReviewStatus = "active"
    reviewer: str = "design-03-seed"


class TargetedDocumentSelector(BaseModel):
    """case revision 中的逻辑目标，计划阶段再解析为具体 doc version。"""

    title_contains: str
    section_path: list[str] = Field(default_factory=list)


class ResolvedTarget(BaseModel):
    """ExecutionPlan 内冻结的文档版本和可选章节。"""

    selector: TargetedDocumentSelector
    doc_id: str
    doc_version: int
    title: str
    section_path: list[str] = Field(default_factory=list)


class CriterionView(BaseModel):
    """用例 revision 内的一条有序判定项。"""

    criterion_id: str
    revision_id: str
    rule_type: Literal["R", "X"]
    ordinal: int
    title: str
    rule_text: str
    evaluator: str
    evaluator_ref: str | None = None
    change_note: str = ""


class CaseRevisionView(BaseModel):
    """用例 revision 及其审核状态。"""

    case_id: str
    revision_id: str
    revision_number: int
    category: str
    question: str
    target_bindings: list[dict[str, Any]]
    distractor_metadata: dict[str, Any]
    n: int
    k: int
    execution_policy: dict[str, Any]
    review_status: ReviewStatus
    reviewer: str | None
    source_ref: str
    content_hash: str
    change_note: str
    criteria: list[CriterionView]


class CaseDraftCriterionView(BaseModel):
    """草稿中的一条可编辑判定项。"""

    criterion_id: str
    base_revision_id: str | None
    rule_type: Literal["R", "X"]
    ordinal: int = Field(ge=1)
    title: str = Field(min_length=1)
    rule_text: str = Field(min_length=1)
    evaluator: Literal["checker", "llm_judge", "human"] = "human"
    evaluator_ref: str | None = None


class StartDraftRequest(BaseModel):
    """创建或读取工作副本时记录当前操作人。"""

    updated_by: str = Field(min_length=1, max_length=128)
    base_revision_id: str | None = None


class CreateCaseDraftRequest(BaseModel):
    """创建一个新的稳定用例及其初始草稿。"""

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,31}$")
    category: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1)
    updated_by: str = Field(min_length=1, max_length=128)


class CaseDraftView(BaseModel):
    """一个可反复编辑的用例草稿。"""

    case_id: str
    base_revision_id: str | None
    next_revision_number: int
    category: str
    question: str
    target_bindings: list[dict[str, Any]]
    distractor_metadata: dict[str, Any]
    n: int
    k: int
    execution_policy: dict[str, Any]
    change_note: str
    criteria: list[CaseDraftCriterionView]
    updated_by: str
    updated_at: str


class SaveCaseDraftRequest(BaseModel):
    """覆盖保存一个可变用例草稿。"""

    category: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1)
    target_bindings: list[dict[str, Any]] = Field(default_factory=list)
    distractor_metadata: dict[str, Any] = Field(default_factory=dict)
    n: int = Field(default=10, ge=1, le=100)
    k: int = Field(default=9, ge=1, le=100)
    execution_policy: dict[str, Any] = Field(default_factory=dict)
    change_note: str = Field(default="", max_length=4_000)
    criteria: list[CaseDraftCriterionView] = Field(min_length=1)
    updated_by: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_draft(self) -> SaveCaseDraftRequest:
        """校验 N/K、规则类型和 criterion 身份唯一性。"""

        if self.k > self.n:
            raise ValueError("k must not exceed n")
        criterion_ids = [item.criterion_id for item in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("criterion ids must be unique")
        return self


class PublishCaseDraftRequest(BaseModel):
    """把当前草稿原子发布成 reviewed case revision。"""

    reviewer: str = Field(min_length=1, max_length=128)


class ReviewRevisionRequest(BaseModel):
    """推进 reviewed／active／retired 审核状态。"""

    review_status: ReviewStatus
    reviewer: str = Field(min_length=1, max_length=128)


class CaseCatalogItem(BaseModel):
    """用例库中的稳定身份、草稿与历史 revisions。"""

    case_id: str
    active_revision_id: str | None
    latest_revision_id: str | None
    has_draft: bool
    revisions: list[CaseRevisionView]


class SuiteRevisionView(BaseModel):
    """可被运行台选择的 suite revision。"""

    suite_id: str
    revision_id: str
    revision_number: int
    name: str
    description: str
    scope_id: str
    review_status: ReviewStatus
    change_note: str
    stale: bool
    stale_reasons: list[str]
    case_count: int
    cases: list[CaseRevisionView]


class SuiteDraftView(BaseModel):
    """可编辑的 suite 成员与元数据。"""

    suite_id: str
    base_revision_id: str | None
    next_revision_number: int
    name: str
    description: str
    scope_id: str
    change_note: str
    cases: list[CaseRevisionView]
    updated_by: str
    updated_at: str


class SaveSuiteDraftRequest(BaseModel):
    """覆盖保存 suite draft。"""

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4_000)
    scope_id: str = Field(min_length=1, max_length=64)
    case_revision_ids: list[str] = Field(min_length=1)
    change_note: str = Field(default="", max_length=4_000)
    updated_by: str = Field(min_length=1, max_length=128)


class PublishSuiteDraftRequest(BaseModel):
    """把 suite draft 原子发布并启用。"""

    reviewer: str = Field(min_length=1, max_length=128)


class PreflightRequest(BaseModel):
    """生成 ExecutionPlan 所需的显式现场和运行策略。"""

    suite_revision_id: str
    collection_snapshot_id: str = Field(pattern=r"^s_[0-9a-f]{16}$")
    run_type: RunType = "quality"
    concurrency: int = Field(default=8, ge=1, le=32)
    total_token_budget: int = Field(default=5_000_000, ge=1)
    model_request_budget: int = Field(default=2_000, ge=1)
    replacement_attempt_budget: int = Field(default=20, ge=0, le=500)
    attempt_timeout_seconds: int = Field(default=600, ge=30, le=3_600)
    experiment_variable_name: str | None = Field(default=None, max_length=128)
    experiment_variable_value: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def normalize_run_policy(self) -> PreflightRequest:
        """延迟评测固定串行，避免并发污染首字延迟。"""

        if self.run_type == "latency":
            self.concurrency = 1
        return self


class QuickPlanRequest(BaseModel):
    """从 reviewed／active case revisions 创建探索性计划。"""

    case_revision_ids: list[str] = Field(min_length=1)
    collection_snapshot_id: str = Field(pattern=r"^s_[0-9a-f]{16}$")
    run_type: RunType = "quality"
    concurrency: int = Field(default=1, ge=1, le=32)
    n_override: int = Field(default=1, ge=1, le=100)
    total_token_budget: int = Field(default=1_000_000, ge=1)
    model_request_budget: int = Field(default=500, ge=1)
    replacement_attempt_budget: int = Field(default=4, ge=0, le=100)
    attempt_timeout_seconds: int = Field(default=600, ge=30, le=3_600)

    @model_validator(mode="after")
    def validate_quick_plan(self) -> QuickPlanRequest:
        """快速试跑中延迟固定串行，且 case revision 不得重复。"""

        if self.run_type == "latency":
            self.concurrency = 1
        if len(self.case_revision_ids) != len(set(self.case_revision_ids)):
            raise ValueError("case revision ids must be unique")
        return self


class PlanCaseView(BaseModel):
    """计划内用例的目标解析结果。"""

    case_revision_id: str
    case_id: str
    question: str
    n: int
    k: int
    source_n: int
    source_k: int
    resolved_targets: list[ResolvedTarget]
    execution_policy: dict[str, Any]


class PreflightCheck(BaseModel):
    """预检中一个可操作的通过或失败项。"""

    code: str
    status: Literal["passed", "failed"]
    message: str


class ExecutionPlanView(BaseModel):
    """预检结果和所有进入执行的冻结信息。"""

    plan_id: str
    status: Literal["planned", "invalid_plan"]
    suite_revision_id: str | None
    collection_snapshot_id: str
    run_type: RunType
    mode: Literal["formal", "exploratory"]
    concurrency: int
    agent_version_set: dict[str, Any]
    model_configuration: dict[str, Any]
    run_policy: dict[str, Any]
    experiment_variable: dict[str, Any]
    checks: list[PreflightCheck]
    cases: list[PlanCaseView]
    created_at: str


class StartRunRequest(BaseModel):
    """启动一个已预检通过的 ExecutionPlan。"""

    plan_id: str


class AttemptView(BaseModel):
    """运行台显示的 attempt 现场。"""

    attempt_id: str
    case_revision_id: str
    replacement_for_attempt_id: str | None
    session_id: str
    trace_id: str | None
    trace_status: Literal["complete", "partial", "missing"]
    execution_status: str
    termination_reason: str | None
    error_code: str | None
    answer: str | None
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    started_at: str
    first_token_at: str | None
    finished_at: str | None


class RunView(BaseModel):
    """ExecutionRun 的当前状态和有效样本进度。"""

    run_id: str
    plan_id: str
    plan_mode: Literal["formal", "exploratory"]
    suite_id: str | None
    suite_name: str | None
    suite_revision_id: str | None
    suite_revision_number: int | None
    status: RunStatus
    pause_reason: str | None
    created_at: str
    started_at: str | None
    sealed_at: str | None
    totals: dict[str, int]
    attempts: list[AttemptView]


class EvaluationHealthResponse(BaseModel):
    """评测 sidecar 的版本和数据库状态。"""

    status: Literal["ok"]
    service: str
    schema_version: int
