from __future__ import annotations

from app.evaluation.aggregation import build_aggregates
from app.evaluation.models import (
    AttemptView,
    CaseRevisionView,
    CheckResultView,
    CheckReviewView,
    CriterionView,
    EffectiveJudgmentBindingView,
    PlanCaseView,
    RunView,
)


def _criterion(rule_type: str, suffix: str) -> CriterionView:
    return CriterionView(
        criterion_id=f"c1-001-{suffix}",
        revision_id=f"ccr_c1_001_{suffix}",
        rule_type=rule_type,  # type: ignore[arg-type]
        ordinal=1 if rule_type == "R" else 2,
        title=suffix,
        rule_text=f"{suffix} rule",
        evaluator="llm_judge",
        evaluator_ref="judge-rubric-v2",
    )


def _binding(
    attempt_id: str,
    criterion: CriterionView,
    result: str,
) -> EffectiveJudgmentBindingView:
    verdict = {
        ("R", "pass"): "satisfied",
        ("R", "fail"): "unsatisfied",
        ("R", "pending"): "unknown",
        ("X", "pass"): "not_violated",
        ("X", "fail"): "violated",
        ("X", "pending"): "unknown",
    }[(criterion.rule_type, result)]
    return EffectiveJudgmentBindingView(
        binding_id=f"binding_{attempt_id}_{criterion.revision_id}",
        attempt_id=attempt_id,
        criterion_revision_id=criterion.revision_id,
        judgment_id=f"judgment_{attempt_id}_{criterion.revision_id}",
        executor="llm_judge",
        verdict=verdict,
        normalized_result=result,  # type: ignore[arg-type]
        is_effective=True,
        bound_at="2026-10-23T00:00:00+00:00",
    )


def _attempt(attempt_id: str, case_revision_id: str = "cr_case") -> AttemptView:
    return AttemptView(
        attempt_id=attempt_id,
        case_revision_id=case_revision_id,
        replacement_for_attempt_id=None,
        session_id=f"session_{attempt_id}",
        trace_id="a" * 32,
        trace_status="complete",
        execution_status="succeeded",
        outcome_class="product",
        termination_reason="finish",
        error_code=None,
        answer="answer",
        input_tokens=1,
        output_tokens=1,
        model_calls=1,
        tool_calls=0,
        started_at="2026-10-23T00:00:00+00:00",
        first_token_at="2026-10-23T00:00:01+00:00",
        finished_at="2026-10-23T00:00:02+00:00",
    )


def _case(criteria: list[CriterionView]) -> CaseRevisionView:
    return CaseRevisionView(
        case_id="c1-001",
        revision_id="cr_case",
        revision_number=1,
        category="1",
        question="question",
        target_bindings=[],
        distractor_metadata={},
        n=10,
        k=9,
        execution_policy={},
        review_status="active",
        reviewer="reviewer",
        source_ref="test",
        content_hash="a" * 64,
        change_note="",
        criteria=criteria,
    )


def _run(attempts: list[AttemptView]) -> RunView:
    return RunView(
        run_id="run",
        plan_id="plan",
        plan_mode="formal",
        suite_id="suite",
        suite_name="suite",
        suite_revision_id="suite-revision",
        suite_revision_number=1,
        status="sealed_complete",
        pause_reason=None,
        created_at="2026-10-23T00:00:00+00:00",
        started_at="2026-10-23T00:00:00+00:00",
        sealed_at="2026-10-23T00:01:00+00:00",
        updated_at="2026-10-23T00:01:00+00:00",
        totals={},
        attempts=attempts,
    )


def _plan(case: CaseRevisionView, n: int, k: int) -> PlanCaseView:
    return PlanCaseView(
        case_revision_id=case.revision_id,
        case_id=case.case_id,
        question=case.question,
        n=n,
        k=k,
        source_n=case.n,
        source_k=case.k,
        resolved_targets=[],
        execution_policy={},
    )


def _integrity_check(attempt_id: str, result: str = "pass") -> CheckResultView:
    return CheckResultView(
        check_result_id=f"check_integrity_{attempt_id}",
        attempt_id=attempt_id,
        checker_id="citation_integrity",
        checker_version="1",
        result=result,  # type: ignore[arg-type]
        payload={},
        error_code=None,
        created_at="2026-10-23T00:00:00+00:00",
    )


def test_attempt_aggregation_uses_fail_priority_and_human_check_review() -> None:
    r = _criterion("R", "r1")
    x = _criterion("X", "x1")
    case = _case([r, x])
    run = _run([_attempt("attempt_1")])
    support = CheckResultView(
        check_result_id="check_support",
        attempt_id="attempt_1",
        checker_id="citation_support",
        checker_version="1",
        result="pass",
        payload={},
        error_code=None,
        created_at="2026-10-23T00:00:00+00:00",
    )
    review = CheckReviewView(
        review_id="review_1",
        attempt_id="attempt_1",
        checker_id="citation_support",
        source_check_result_id="check_support",
        result="fail",
        payload={},
        error_code="citation_not_supported",
        reason="人工确认不支持",
        reviewer="reviewer",
        created_at="2026-10-23T00:00:01+00:00",
    )

    attempts, _cases = build_aggregates(
        run=run,
        plan_cases=[_plan(case, n=1, k=1)],
        cases={case.revision_id: case},
        bindings=[
            _binding("attempt_1", r, "pass"),
            _binding("attempt_1", x, "pass"),
        ],
        checks=[_integrity_check("attempt_1"), support],
        reviews=[review],
    )

    assert attempts[0].result == "fail"
    assert attempts[0].check_failures == ["citation_support"]


def test_case_aggregation_applies_k_threshold_exploratory_and_redline() -> None:
    r = _criterion("R", "r1")
    x = _criterion("X", "x1")
    case = _case([r, x])
    attempts = [_attempt(f"attempt_{index}") for index in range(3)]
    run = _run(attempts)
    bindings = []
    for index, attempt in enumerate(attempts):
        bindings.extend(
            [
                _binding(
                    attempt.attempt_id,
                    r,
                    "pending" if index == 2 else "pass",
                ),
                _binding(attempt.attempt_id, x, "pass"),
            ]
        )
    checks = [_integrity_check(attempt.attempt_id) for attempt in attempts]

    _attempt_aggregates, cases = build_aggregates(
        run=run,
        plan_cases=[_plan(case, n=3, k=3)],
        cases={case.revision_id: case},
        bindings=bindings,
        checks=checks,
        reviews=[],
    )
    assert cases[0].result == "pending"

    exploratory = build_aggregates(
        run=run,
        plan_cases=[_plan(case, n=2, k=3)],
        cases={case.revision_id: case},
        bindings=bindings,
        checks=checks,
        reviews=[],
    )[1][0]
    assert exploratory.result == "exploratory"

    redline_bindings = [
        binding for binding in bindings if binding.criterion_revision_id == r.revision_id
    ] + [
        _binding(
            attempt.attempt_id,
            x,
            "fail" if index == 0 else "pass",
        )
        for index, attempt in enumerate(attempts)
    ]
    redline = build_aggregates(
        run=run,
        plan_cases=[_plan(case, n=3, k=1)],
        cases={case.revision_id: case},
        bindings=redline_bindings,
        checks=checks,
        reviews=[],
    )[1][0]
    assert redline.result == "fail"
    assert redline.redline_violation_count == 1
