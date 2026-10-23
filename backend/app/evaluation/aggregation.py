from __future__ import annotations

from collections import Counter

from app.evaluation.models import (
    AttemptAggregateView,
    CaseAggregateView,
    CaseRevisionView,
    CheckResultView,
    CheckReviewView,
    EffectiveJudgmentBindingView,
    PlanCaseView,
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
        check_pending: list[str] = []
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
        if "citation_integrity" not in attempt_checks:
            check_pending.append("citation_integrity")

        reasons: list[str] = []
        if r_fail:
            reasons.append(f"{r_fail} 条必选 R 判定失败")
        if x_violated:
            reasons.append(f"{x_violated} 条边界 X 判定违规")
        if check_failures:
            reasons.append("跨项检查失败：" + "、".join(check_failures))
        if missing_binding_count:
            reasons.append(f"{missing_binding_count} 条 criterion 缺少生效绑定")
        if r_pending:
            reasons.append(f"{r_pending} 条必选 R 判定待定")
        if x_pending:
            reasons.append(f"{x_pending} 条边界 X 判定待定")
        if check_pending:
            reasons.append("跨项检查待定：" + "、".join(check_pending))

        if r_fail or x_violated or check_failures:
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
                check_pending=check_pending,
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
        redline_violation_count=redline_violation_count,
        result=result,
        reasons=reasons,
    )
