from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.storage.models import utc_now


class EvaluationBase(DeclarativeBase):
    """评测数据库的独立声明基类，不能混入产品数据库 metadata。"""


class EvalCaseRow(EvaluationBase):
    """评测用例的稳定身份。"""

    __tablename__ = "eval_cases"

    case_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalCaseRevisionRow(EvaluationBase):
    """不可变用例内容版本；只有审核生命周期字段允许变化。"""

    __tablename__ = "eval_case_revisions"

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    target_bindings_json: Mapped[str] = mapped_column(Text, nullable=False)
    distractor_metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalCriterionRow(EvaluationBase):
    """用例判定项的稳定身份。"""

    __tablename__ = "eval_criteria"

    criterion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalCriterionRevisionRow(EvaluationBase):
    """不可变判定项内容版本。"""

    __tablename__ = "eval_criterion_revisions"

    revision_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    criterion_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_criteria.criterion_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_type: Mapped[str] = mapped_column(String(1), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    evaluator: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluator_ref: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalCaseRevisionCriterionRow(EvaluationBase):
    """固定 case revision 内 criterion revision 的顺序。"""

    __tablename__ = "eval_case_revision_criteria"

    case_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    criterion_revision_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("eval_criterion_revisions.revision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class EvalCaseDraftRow(EvaluationBase):
    """可反复编辑的用例工作副本。"""

    __tablename__ = "eval_case_drafts"

    case_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    base_revision_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    target_bindings_json: Mapped[str] = mapped_column(Text, nullable=False)
    distractor_metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
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


class EvalCaseDraftCriterionRow(EvaluationBase):
    """用例草稿中的一条可编辑 R／X 判定项。"""

    __tablename__ = "eval_case_draft_criteria"

    criterion_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("eval_cases.case_id", ondelete="RESTRICT"),
        nullable=False,
    )
    base_revision_id: Mapped[str | None] = mapped_column(
        String(96),
        ForeignKey("eval_criterion_revisions.revision_id", ondelete="RESTRICT"),
    )
    rule_type: Mapped[str] = mapped_column(String(1), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    evaluator: Mapped[str] = mapped_column(String(16), nullable=False)
    evaluator_ref: Mapped[str | None] = mapped_column(Text)


class EvalSuiteRow(EvaluationBase):
    """评测 suite 的稳定身份。"""

    __tablename__ = "eval_suites"

    suite_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalSuiteRevisionRow(EvaluationBase):
    """不可变 suite 内容版本。"""

    __tablename__ = "eval_suite_revisions"

    revision_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    suite_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_suites.suite_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewer: Mapped[str | None] = mapped_column(String(128))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalSuiteRevisionCaseRow(EvaluationBase):
    """suite revision 固定引用的 active case revisions。"""

    __tablename__ = "eval_suite_revision_cases"

    suite_revision_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("eval_suite_revisions.revision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    case_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class EvalSuiteDraftRow(EvaluationBase):
    """可反复编辑的用例集工作副本。"""

    __tablename__ = "eval_suite_drafts"

    suite_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_suites.suite_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    base_revision_id: Mapped[str | None] = mapped_column(
        String(96),
        ForeignKey("eval_suite_revisions.revision_id", ondelete="RESTRICT"),
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope_id: Mapped[str] = mapped_column(String(64), nullable=False)
    change_note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False)
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


class EvalSuiteDraftCaseRow(EvaluationBase):
    """用例集草稿中的精确 case revisions。"""

    __tablename__ = "eval_suite_draft_cases"

    suite_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_suite_drafts.suite_id", ondelete="CASCADE"),
        primary_key=True,
    )
    case_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class EvalExecutionPlanRow(EvaluationBase):
    """预检通过后冻结的一次执行计划。"""

    __tablename__ = "eval_execution_plans"

    plan_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    suite_revision_id: Mapped[str | None] = mapped_column(
        String(96),
        ForeignKey("eval_suite_revisions.revision_id", ondelete="RESTRICT"),
    )
    collection_snapshot_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False)
    agent_version_set_json: Mapped[str] = mapped_column(Text, nullable=False)
    model_config_json: Mapped[str] = mapped_column(Text, nullable=False)
    run_policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    experiment_variable_json: Mapped[str] = mapped_column(Text, nullable=False)
    preflight_result_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalPlanCaseRow(EvaluationBase):
    """计划内一次用例快照与目标解析结果。"""

    __tablename__ = "eval_plan_cases"

    plan_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_execution_plans.plan_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    case_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    n: Mapped[int] = mapped_column(Integer, nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    source_n: Mapped[int] = mapped_column(Integer, nullable=False)
    source_k: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_targets_json: Mapped[str] = mapped_column(Text, nullable=False)
    execution_policy_json: Mapped[str] = mapped_column(Text, nullable=False)


class EvalExecutionRunRow(EvaluationBase):
    """一次可恢复的评测执行现场。"""

    __tablename__ = "eval_execution_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_execution_plans.plan_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(Text)
    resume_of_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("eval_execution_runs.run_id", ondelete="RESTRICT"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )


class EvalAttemptRow(EvaluationBase):
    """一个全新 Agent 会话的可审计执行结果。"""

    __tablename__ = "eval_attempts"

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_execution_runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    case_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_case_revisions.revision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    replacement_for_attempt_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    trace_status: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome_class: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unknown",
    )
    termination_reason: Mapped[str | None] = mapped_column(String(96))
    error_code: Mapped[str | None] = mapped_column(String(96))
    answer: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    usage_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class EvalEvaluationRevisionRow(EvaluationBase):
    """一次逐 criterion 判定任务的不可变配置与可更新进度。"""

    __tablename__ = "eval_evaluation_revisions"

    revision_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_execution_runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    checker_version_set_json: Mapped[str] = mapped_column(Text, nullable=False)
    judge_version_set_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(96))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvalCheckResultRow(EvaluationBase):
    """一次通用跨判定项确定性检查结果，只追加。"""

    __tablename__ = "eval_check_results"

    check_result_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "eval_evaluation_revisions.revision_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    attempt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
        nullable=False,
    )
    checker_id: Mapped[str] = mapped_column(String(96), nullable=False)
    checker_version: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalJudgmentRow(EvaluationBase):
    """一个 attempt 对一条 criterion 的最终判定事实。"""

    __tablename__ = "eval_judgments"

    judgment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    evaluation_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "eval_evaluation_revisions.revision_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    attempt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
        nullable=False,
    )
    criterion_revision_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey(
            "eval_criterion_revisions.revision_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    executor: Mapped[str] = mapped_column(String(16), nullable=False)
    executor_ref: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str] = mapped_column(String(24), nullable=False)
    normalized_result: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(96))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalJudgeInvocationRow(EvaluationBase):
    """一次 Judge provider 调用及其原始输入输出。"""

    __tablename__ = "eval_judge_invocations"

    invocation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    judgment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_judgments.judgment_id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(96), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_hash: Mapped[str | None] = mapped_column(String(64))
    input_json: Mapped[str] = mapped_column(Text, nullable=False)
    raw_output: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(96))
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvalJudgmentBindingRow(EvaluationBase):
    """把一条 Judgment 显式绑定为当前生效判定。"""

    __tablename__ = "eval_judgment_bindings"

    binding_order: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    binding_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    evaluation_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_evaluation_revisions.revision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
        nullable=False,
    )
    criterion_revision_id: Mapped[str] = mapped_column(
        String(96),
        ForeignKey("eval_criterion_revisions.revision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    judgment_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_judgments.judgment_id", ondelete="RESTRICT"),
        nullable=False,
    )
    bound_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalCheckReviewRow(EvaluationBase):
    """人工对 LLM 型 CheckResult 的可追加复核结果。"""

    __tablename__ = "eval_check_reviews"

    review_order: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    review_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    evaluation_revision_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_evaluation_revisions.revision_id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_attempts.attempt_id", ondelete="RESTRICT"),
        nullable=False,
    )
    checker_id: Mapped[str] = mapped_column(String(96), nullable=False)
    source_check_result_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("eval_check_results.check_result_id", ondelete="RESTRICT"),
        nullable=False,
    )
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(96))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )


class EvalSchemaVersionRow(EvaluationBase):
    """评测数据库 schema 的幂等初始化标记。"""

    __tablename__ = "eval_schema_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )
