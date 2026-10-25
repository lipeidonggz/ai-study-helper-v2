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
    trace_status: Literal["complete", "partial", "dropped", "missing"]
    execution_status: str
    outcome_class: Literal["product", "infrastructure", "canceled", "unknown"]
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
    run_type: RunType = "quality"
    suite_id: str | None
    suite_name: str | None
    suite_revision_id: str | None
    suite_revision_number: int | None
    status: RunStatus
    pause_reason: str | None
    created_at: str
    started_at: str | None
    sealed_at: str | None
    updated_at: str
    totals: dict[str, int]
    attempts: list[AttemptView]


class StartJudgmentRequest(BaseModel):
    """启动一次 EvaluationRevision 判定的运行策略。"""

    concurrency: int = Field(default=2, ge=1, le=8)
    token_budget: int = Field(default=500_000, ge=1)
    request_budget: int = Field(default=500, ge=1)
    attempt_timeout_seconds: int = Field(default=60, ge=10, le=300)
    judge_review_confirmed: bool = False


class JudgeInvocationView(BaseModel):
    """一次 Judge provider 调用及 usage。"""

    invocation_id: str
    attempt_number: int
    prompt_version: str
    model_id: str
    status: str
    error_code: str | None
    input_tokens: int
    output_tokens: int
    request_id: str | None
    input_json: str | None
    raw_output: str | None
    started_at: str
    finished_at: str | None


class JudgmentView(BaseModel):
    """一条 attempt×criterion Judgment 及 invocation 历史。"""

    judgment_id: str
    attempt_id: str
    criterion_revision_id: str
    executor: str
    executor_ref: str | None
    verdict: str
    normalized_result: str
    reason: str | None
    evidence: dict[str, Any]
    error_code: str | None
    created_at: str
    invocations: list[JudgeInvocationView]


class EffectiveJudgmentBindingView(BaseModel):
    """一条当前生效的 Judgment 绑定。"""

    binding_id: str
    attempt_id: str
    criterion_revision_id: str
    judgment_id: str
    executor: str
    verdict: str
    normalized_result: Literal["pass", "fail", "pending"]
    is_effective: bool
    bound_at: str


class JudgeOverrideRequest(BaseModel):
    """人工修正一条 LLM 语义 Judgment。"""

    attempt_id: str
    criterion_revision_id: str
    verdict: str = Field(min_length=1, max_length=24)
    reason: str = Field(min_length=1)
    reviewer: str = Field(min_length=1, max_length=128)


class CitationReviewItem(BaseModel):
    """人工对一个引用 block 的语义支持结论。"""

    block_id: str = Field(min_length=1)
    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = Field(min_length=1)
    evidence_quotes: list[str] = Field(default_factory=list)


class CheckReviewRequest(BaseModel):
    """人工修正一个 LLM 型 CheckResult。"""

    attempt_id: str
    checker_id: str = Field(min_length=1, max_length=96)
    citations: list[CitationReviewItem] = Field(min_length=1)
    reason: str = Field(min_length=1)
    reviewer: str = Field(min_length=1, max_length=128)


class CheckReviewView(BaseModel):
    """一条 append-only human check review。"""

    review_id: str
    attempt_id: str
    checker_id: str
    source_check_result_id: str
    result: Literal["pass", "fail", "pending"]
    payload: dict[str, Any]
    error_code: str | None
    reason: str
    reviewer: str
    created_at: str


class CheckResultView(BaseModel):
    """一条通用跨判定项确定性检查结果。"""

    check_result_id: str
    attempt_id: str
    checker_id: str
    checker_version: str
    result: Literal["pass", "fail", "pending"]
    payload: dict[str, Any]
    error_code: str | None
    created_at: str


class AttemptAggregateView(BaseModel):
    """一个 attempt 的生效 criterion 与全局检查聚合。"""

    attempt_id: str
    case_id: str
    case_revision_id: str
    valid_sample: bool
    result: Literal["pass", "fail", "pending", "excluded"]
    r_pass: int
    r_fail: int
    r_pending: int
    x_not_violated: int
    x_violated: int
    x_pending: int
    missing_binding_count: int
    check_failures: list[str] = Field(default_factory=list)
    check_pending: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class CaseAggregateView(BaseModel):
    """一个 case 在冻结 N/K 下的聚合结果。"""

    case_id: str
    case_revision_id: str
    n: int
    k: int
    valid_samples: int
    pass_count: int
    fail_count: int
    pending_count: int
    x_violation_count: int = 0
    check_failure_count: int = 0
    redline_violation_count: int
    result: Literal["pass", "fail", "pending", "exploratory"]
    reasons: list[str] = Field(default_factory=list)


class ResultMatrixAttemptView(BaseModel):
    """结果矩阵中的一次执行样本及其成本和证据入口。"""

    attempt_id: str
    sample_ordinal: int = Field(ge=1)
    execution_status: str
    outcome_class: Literal["product", "infrastructure", "canceled", "unknown"]
    valid_sample: bool
    result: Literal["pass", "fail", "pending", "excluded"]
    redline_violated: bool
    x_violated: int = 0
    check_failures: list[str] = Field(default_factory=list)
    trace_id: str | None
    trace_status: Literal["complete", "partial", "dropped", "missing"]
    started_at: str
    finished_at: str | None
    duration_ms: int | None = Field(default=None, ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list)


class ResultMatrixCaseView(BaseModel):
    """一个 case 在结果矩阵中的 N/K、状态与资源汇总。"""

    case_id: str
    case_revision_id: str
    category: str
    question: str
    n: int
    k: int
    attempt_count: int
    valid_samples: int
    excluded_count: int
    pass_count: int
    fail_count: int
    pending_count: int
    x_violation_count: int = 0
    check_failure_count: int = 0
    redline_violation_count: int
    result: Literal["pass", "fail", "pending", "exploratory"]
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    duration_ms: int
    reasons: list[str] = Field(default_factory=list)
    attempts: list[ResultMatrixAttemptView] = Field(default_factory=list)


class ResultMatrixTotalsView(BaseModel):
    """一个类别或整个 run 的结果与资源总量。"""

    case_count: int
    case_pass_count: int
    case_fail_count: int
    case_pending_count: int
    case_exploratory_count: int
    attempt_count: int
    valid_samples: int
    excluded_attempts: int
    infrastructure_failures: int
    canceled_attempts: int
    unknown_attempts: int
    x_violation_count: int = 0
    check_failure_count: int = 0
    redline_violation_count: int
    input_tokens: int
    output_tokens: int
    model_calls: int
    tool_calls: int
    duration_ms: int


class ResultMatrixCategoryView(BaseModel):
    """按冻结 case category 分组的结果矩阵。"""

    category: str
    totals: ResultMatrixTotalsView
    cases: list[ResultMatrixCaseView]


class EvaluationResultMatrixView(BaseModel):
    """从执行与判定事实重建的四层只读结果矩阵。"""

    run_id: str
    run_status: RunStatus
    plan_mode: Literal["formal", "exploratory"]
    suite_name: str | None
    suite_revision_id: str | None
    suite_revision_number: int | None
    totals: ResultMatrixTotalsView
    categories: list[ResultMatrixCategoryView]


class EvaluationRevisionView(BaseModel):
    """一次判定任务的进度、版本和逐项结果。"""

    revision_id: str
    execution_run_id: str
    status: Literal["running", "complete", "incomplete", "canceled"]
    checker_version_set: dict[str, Any]
    judge_version_set: dict[str, Any]
    config: dict[str, Any]
    summary: dict[str, int]
    error_code: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    judgments: list[JudgmentView]
    judgment_bindings: list[EffectiveJudgmentBindingView] = Field(default_factory=list)
    effective_bindings: list[EffectiveJudgmentBindingView] = Field(default_factory=list)
    check_reviews: list[CheckReviewView] = Field(default_factory=list)
    checks: list[CheckResultView]
    attempt_aggregates: list[AttemptAggregateView] = Field(default_factory=list)
    case_aggregates: list[CaseAggregateView] = Field(default_factory=list)
    result_matrix: EvaluationResultMatrixView | None = None


class JudgePromptView(BaseModel):
    """当前判官 prompt 的原文与版本，供人工审核。"""

    version: str
    content_hash: str
    content: str


class EvaluationHealthResponse(BaseModel):
    """评测 sidecar 的版本和数据库状态。"""

    status: Literal["ok"]
    service: str
    schema_version: int


class SetBaselineRequest(BaseModel):
    """人工把一个完整正式评测设为当前基线。"""

    evaluation_revision_id: str = Field(min_length=1)
    reviewer: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=4_000)


class BaselineView(BaseModel):
    """一个不可变评测基线及其冻结的质量汇总。"""

    baseline_id: str
    suite_id: str
    suite_name: str
    scope_id: str
    run_type: RunType
    suite_revision_id: str
    collection_snapshot_id: str
    execution_run_id: str
    evaluation_revision_id: str
    case_revision_ids: list[str]
    aggregate_rule_version: str
    totals: ResultMatrixTotalsView
    run_started_at: str | None
    run_sealed_at: str | None
    reviewer: str
    reason: str
    created_at: str
    is_current: bool


class BaselineCaseDeltaView(BaseModel):
    """同一 case identity 在基线与目标 run 中的结果和资源差异。"""

    case_id: str
    category: str
    baseline_case_revision_id: str | None
    target_case_revision_id: str | None
    baseline_result: Literal["pass", "fail", "pending", "exploratory"] | None
    target_result: Literal["pass", "fail", "pending", "exploratory"] | None
    change: Literal["new", "removed", "improved", "regressed", "changed", "unchanged"]
    revision_changed: bool
    valid_samples_delta: int
    pass_count_delta: int
    fail_count_delta: int
    pending_count_delta: int
    x_violation_count_delta: int = 0
    check_failure_count_delta: int = 0
    redline_violation_count_delta: int
    input_tokens_delta: int
    output_tokens_delta: int
    model_calls_delta: int
    tool_calls_delta: int
    duration_ms_delta: int


class BaselineComparisonSummaryView(BaseModel):
    """目标 run 相对基线的 run 级差异。"""

    case_pass_count_delta: int
    case_fail_count_delta: int
    case_pending_count_delta: int
    case_exploratory_count_delta: int
    valid_samples_delta: int
    x_violation_count_delta: int = 0
    check_failure_count_delta: int = 0
    redline_violation_count_delta: int
    input_tokens_delta: int
    output_tokens_delta: int
    model_calls_delta: int
    tool_calls_delta: int
    duration_ms_delta: int
    newly_passing_count: int
    newly_failing_count: int


class BaselineComparisonView(BaseModel):
    """一个完整 run 与当前基线的只读比较结果。"""

    baseline: BaselineView
    target_run_id: str
    target_evaluation_revision_id: str
    target_suite_revision_id: str
    target_collection_snapshot_id: str
    target_totals: ResultMatrixTotalsView
    comparable: bool
    incompatibility_reasons: list[str]
    summary: BaselineComparisonSummaryView
    cases: list[BaselineCaseDeltaView]
