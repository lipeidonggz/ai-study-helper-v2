from __future__ import annotations

from collections import Counter
from datetime import datetime

from app.evaluation.models import (
    AttemptAggregateView,
    AttemptView,
    CaseAggregateView,
    CaseRevisionView,
    CheckResultView,
    CheckReviewView,
    EffectiveJudgmentBindingView,
    EvaluationResultMatrixView,
    PlanCaseView,
    ResultMatrixAttemptView,
    ResultMatrixCaseView,
    ResultMatrixCategoryView,
    ResultMatrixTotalsView,
    RunView,
)


def build_aggregates(
    *,
    run: RunView,
    plan_cases: list[PlanCaseView],
    cases: dict[str, CaseRevisionView],
    bindings: list[EffectiveJudgmentBindingView],
    checks: list[CheckResultView],
    reviews: list[CheckReviewView],
) -> tuple[list[AttemptAggregateView], list[CaseAggregateView]]:
    """从生效绑定、有效 CheckResult 和冻结 N/K 构建两级聚合。"""

    binding_by_key = {(item.attempt_id, item.criterion_revision_id): item for item in bindings}
    checks_by_attempt: dict[str, list[CheckResultView]] = {}
    for check in checks:
        checks_by_attempt.setdefault(check.attempt_id, []).append(check)
    effective_reviews = {(item.attempt_id, item.checker_id): item for item in reviews}

    attempt_aggregates: list[AttemptAggregateView] = []
    for attempt in run.attempts:
        case = cases[attempt.case_revision_id]
        if attempt.outcome_class != "product":
            attempt_aggregates.append(
                AttemptAggregateView(
                    attempt_id=attempt.attempt_id,
                    case_id=case.case_id,
                    case_revision_id=case.revision_id,
                    valid_sample=False,
                    result="excluded",
                    r_pass=0,
                    r_fail=0,
                    r_pending=0,
                    x_not_violated=0,
                    x_violated=0,
                    x_pending=0,
                    missing_binding_count=len(case.criteria),
                    reasons=["基础设施失败或结果未知，不计入有效 N。"],
                )
            )
            continue

        criterion_counts: Counter[tuple[str, str]] = Counter()
        missing_binding_count = 0
        for criterion in case.criteria:
            binding = binding_by_key.get((attempt.attempt_id, criterion.revision_id))
            if binding is None:
                missing_binding_count += 1
                continue
            criterion_counts[(criterion.rule_type, binding.normalized_result)] += 1

        r_pass = criterion_counts[("R", "pass")]
        r_fail = criterion_counts[("R", "fail")]
        r_pending = criterion_counts[("R", "pending")]
        x_not_violated = criterion_counts[("X", "pass")]
        x_violated = criterion_counts[("X", "fail")]
        x_pending = criterion_counts[("X", "pending")]

        check_failures: list[str] = []
        check_quality_failures: list[str] = []
        check_pending: list[str] = []
        review_pending = False
        attempt_checks = {
            check.checker_id: check for check in checks_by_attempt.get(attempt.attempt_id, [])
        }
        for checker_id, check in attempt_checks.items():
            review = effective_reviews.get((attempt.attempt_id, checker_id))
            result = review.result if review is not None else check.result
            if result == "fail":
                check_failures.append(checker_id)
            elif result == "pending":
                check_pending.append(checker_id)
                if review is None and check.payload.get("observed_result") == "fail":
                    check_quality_failures.append(checker_id)
                    review_pending = True
        if "citation_integrity" not in attempt_checks:
            check_pending.append("citation_integrity")

        reasons: list[str] = []
        if r_fail:
            reasons.append(f"{r_fail} 条必选 R 判定失败")
        if x_violated:
            reasons.append(f"{x_violated} 条边界 X 判定违规")
        if check_failures:
            reasons.append("跨项检查失败：" + "、".join(check_failures))
        if check_quality_failures:
            reasons.append("跨项未确认失败：" + "、".join(check_quality_failures))
        if missing_binding_count:
            reasons.append(f"{missing_binding_count} 条 criterion 缺少生效绑定")
        if r_pending:
            reasons.append(f"{r_pending} 条必选 R 判定待定")
        if x_pending:
            reasons.append(f"{x_pending} 条边界 X 判定待定")
        if check_pending:
            reasons.append("跨项检查待定：" + "、".join(check_pending))

        if r_fail or x_violated or check_failures or check_quality_failures:
            result = "fail"
        elif missing_binding_count or r_pending or x_pending or check_pending:
            result = "pending"
        else:
            result = "pass"
        attempt_aggregates.append(
            AttemptAggregateView(
                attempt_id=attempt.attempt_id,
                case_id=case.case_id,
                case_revision_id=case.revision_id,
                valid_sample=True,
                result=result,
                r_pass=r_pass,
                r_fail=r_fail,
                r_pending=r_pending,
                x_not_violated=x_not_violated,
                x_violated=x_violated,
                x_pending=x_pending,
                missing_binding_count=missing_binding_count,
                check_failures=check_failures,
                check_quality_failures=check_quality_failures,
                check_pending=check_pending,
                review_pending=review_pending,
                reasons=reasons,
            )
        )

    attempt_by_case: dict[str, list[AttemptAggregateView]] = {}
    for aggregate in attempt_aggregates:
        attempt_by_case.setdefault(aggregate.case_revision_id, []).append(aggregate)
    case_aggregates = [
        _case_aggregate(
            plan_case=plan_case,
            attempts=attempt_by_case.get(plan_case.case_revision_id, []),
        )
        for plan_case in plan_cases
    ]
    return attempt_aggregates, case_aggregates


def _case_aggregate(
    *,
    plan_case: PlanCaseView,
    attempts: list[AttemptAggregateView],
) -> CaseAggregateView:
    valid = [item for item in attempts if item.valid_sample]
    pass_count = sum(item.result == "pass" for item in valid)
    fail_count = sum(item.result == "fail" for item in valid)
    pending_count = sum(item.result == "pending" for item in valid)
    x_violation_count = sum(item.x_violated > 0 for item in valid)
    check_failure_count = sum(bool(item.check_failures) for item in valid)
    quality_failure_count = sum(bool(item.check_quality_failures) for item in valid)
    review_pending_count = sum(item.review_pending for item in valid)
    redline_violation_count = sum(
        item.x_violated > 0 or bool(item.check_failures) for item in valid
    )
    reasons: list[str] = []
    if plan_case.n < plan_case.k:
        result = "exploratory"
        reasons.append(f"N={plan_case.n} 小于 K={plan_case.k}，仅产生探索性分布。")
    elif redline_violation_count:
        result = "fail"
        reasons.append(f"{redline_violation_count} 个有效样本出现 X 违规或跨项检查失败。")
    elif review_pending_count:
        result = "pending"
        reasons.append(f"{review_pending_count} 个有效样本存在未确认语义失败候选，等待人工复核。")
    elif len(valid) < plan_case.n:
        result = "pending"
        reasons.append(f"有效样本 {len(valid)}／{plan_case.n}，基础设施替代样本不足。")
    elif pass_count >= plan_case.k:
        result = "pass"
        reasons.append(f"{pass_count}／{plan_case.n} 个样本通过，达到 K={plan_case.k}。")
    elif pass_count + pending_count >= plan_case.k:
        result = "pending"
        reasons.append(f"当前 {pass_count} pass、{pending_count} pending，人工复核后仍可能达到 K。")
    else:
        result = "fail"
        reasons.append(
            f"只有 {pass_count} pass、{pending_count} pending，全部改判也无法达到 K={plan_case.k}。"
        )
    return CaseAggregateView(
        case_id=plan_case.case_id,
        case_revision_id=plan_case.case_revision_id,
        n=plan_case.n,
        k=plan_case.k,
        valid_samples=len(valid),
        pass_count=pass_count,
        fail_count=fail_count,
        pending_count=pending_count,
        x_violation_count=x_violation_count,
        check_failure_count=check_failure_count,
        quality_failure_count=quality_failure_count,
        review_pending_count=review_pending_count,
        redline_violation_count=redline_violation_count,
        result=result,
        reasons=reasons,
    )


def build_result_matrix(
    *,
    run: RunView,
    plan_cases: list[PlanCaseView],
    cases: dict[str, CaseRevisionView],
    attempt_aggregates: list[AttemptAggregateView],
    case_aggregates: list[CaseAggregateView],
) -> EvaluationResultMatrixView:
    """把冻结用例、attempt 聚合与 N/K 结果投影成四层只读矩阵。"""

    aggregate_by_attempt = {aggregate.attempt_id: aggregate for aggregate in attempt_aggregates}
    aggregate_by_case = {aggregate.case_revision_id: aggregate for aggregate in case_aggregates}
    attempts_by_case: dict[str, list[AttemptView]] = {}
    for attempt in run.attempts:
        attempts_by_case.setdefault(attempt.case_revision_id, []).append(attempt)

    cases_by_category: dict[str, list[ResultMatrixCaseView]] = {}
    for plan_case in plan_cases:
        case = cases[plan_case.case_revision_id]
        attempts = attempts_by_case.get(case.revision_id, [])
        attempt_views = [
            _result_matrix_attempt(
                attempt=attempt,
                sample_ordinal=index,
                aggregate=aggregate_by_attempt.get(attempt.attempt_id),
            )
            for index, attempt in enumerate(attempts, start=1)
        ]
        aggregate = aggregate_by_case[case.revision_id]
        category = case.category or "未分类"
        cases_by_category.setdefault(category, []).append(
            ResultMatrixCaseView(
                case_id=case.case_id,
                case_revision_id=case.revision_id,
                category=category,
                question=case.question,
                n=aggregate.n,
                k=aggregate.k,
                attempt_count=len(attempt_views),
                valid_samples=aggregate.valid_samples,
                excluded_count=sum(item.result == "excluded" for item in attempt_views),
                pass_count=aggregate.pass_count,
                fail_count=aggregate.fail_count,
                pending_count=aggregate.pending_count,
                x_violation_count=aggregate.x_violation_count,
                check_failure_count=aggregate.check_failure_count,
                quality_failure_count=aggregate.quality_failure_count,
                review_pending_count=aggregate.review_pending_count,
                redline_violation_count=aggregate.redline_violation_count,
                result=aggregate.result,
                input_tokens=sum(item.input_tokens for item in attempt_views),
                output_tokens=sum(item.output_tokens for item in attempt_views),
                model_calls=sum(item.model_calls for item in attempt_views),
                tool_calls=sum(item.tool_calls for item in attempt_views),
                duration_ms=sum(item.duration_ms or 0 for item in attempt_views),
                reasons=aggregate.reasons,
                attempts=attempt_views,
            )
        )

    categories = [
        ResultMatrixCategoryView(
            category=category,
            totals=_result_matrix_totals(cases),
            cases=cases,
        )
        for category, cases in cases_by_category.items()
    ]
    all_cases = [case for category in categories for case in category.cases]
    return EvaluationResultMatrixView(
        run_id=run.run_id,
        run_status=run.status,
        plan_mode=run.plan_mode,
        suite_name=run.suite_name,
        suite_revision_id=run.suite_revision_id,
        suite_revision_number=run.suite_revision_number,
        totals=_result_matrix_totals(all_cases),
        categories=categories,
    )


def _result_matrix_attempt(
    *,
    attempt: AttemptView,
    sample_ordinal: int,
    aggregate: AttemptAggregateView | None,
) -> ResultMatrixAttemptView:
    return ResultMatrixAttemptView(
        attempt_id=attempt.attempt_id,
        sample_ordinal=sample_ordinal,
        execution_status=attempt.execution_status,
        outcome_class=attempt.outcome_class,
        valid_sample=aggregate.valid_sample if aggregate is not None else False,
        result=aggregate.result if aggregate is not None else "pending",
        redline_violated=(
            aggregate is not None and (aggregate.x_violated > 0 or bool(aggregate.check_failures))
        ),
        x_violated=0 if aggregate is None else aggregate.x_violated,
        check_failures=[] if aggregate is None else aggregate.check_failures,
        quality_failures=([] if aggregate is None else aggregate.check_quality_failures),
        review_pending=aggregate.review_pending if aggregate is not None else False,
        trace_id=attempt.trace_id,
        trace_status=attempt.trace_status,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        duration_ms=_duration_ms(attempt.started_at, attempt.finished_at),
        input_tokens=attempt.input_tokens,
        output_tokens=attempt.output_tokens,
        model_calls=attempt.model_calls,
        tool_calls=attempt.tool_calls,
        reasons=[] if aggregate is None else aggregate.reasons,
    )


def _result_matrix_totals(cases: list[ResultMatrixCaseView]) -> ResultMatrixTotalsView:
    attempts = [attempt for case in cases for attempt in case.attempts]
    return ResultMatrixTotalsView(
        case_count=len(cases),
        case_pass_count=sum(case.result == "pass" for case in cases),
        case_fail_count=sum(case.result == "fail" for case in cases),
        case_pending_count=sum(case.result == "pending" for case in cases),
        case_exploratory_count=sum(case.result == "exploratory" for case in cases),
        attempt_count=len(attempts),
        valid_samples=sum(attempt.valid_sample for attempt in attempts),
        excluded_attempts=sum(attempt.result == "excluded" for attempt in attempts),
        infrastructure_failures=sum(
            attempt.outcome_class == "infrastructure" for attempt in attempts
        ),
        canceled_attempts=sum(attempt.outcome_class == "canceled" for attempt in attempts),
        unknown_attempts=sum(attempt.outcome_class == "unknown" for attempt in attempts),
        x_violation_count=sum(case.x_violation_count for case in cases),
        check_failure_count=sum(case.check_failure_count for case in cases),
        redline_violation_count=sum(case.redline_violation_count for case in cases),
        input_tokens=sum(case.input_tokens for case in cases),
        output_tokens=sum(case.output_tokens for case in cases),
        model_calls=sum(case.model_calls for case in cases),
        tool_calls=sum(case.tool_calls for case in cases),
        duration_ms=sum(case.duration_ms for case in cases),
    )


def _duration_ms(started_at: str, finished_at: str | None) -> int | None:
    if finished_at is None:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    return max(0, int((finished - started).total_seconds() * 1_000))
