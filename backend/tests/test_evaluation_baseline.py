from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import pytest
from sqlalchemy.exc import IntegrityError

from app.evaluation.aggregation import build_result_matrix
from app.evaluation.baseline import EvaluationBaselineService
from app.evaluation.database import EvaluationDatabase
from app.evaluation.models import (
    CaseAggregateView,
    EvaluationRevisionView,
    PlanCaseView,
    PreflightCheck,
    RunStatus,
    SetBaselineRequest,
)
from app.evaluation.repository import EvaluationRepository
from app.evaluation.storage_models import EvalBaselineRow, EvalEvaluationRevisionRow


def _create_complete_run(
    database: EvaluationDatabase,
    repository: EvaluationRepository,
    *,
    statuses: dict[str, str] | None = None,
    mode: str = "formal",
    snapshot_id: str = "s_0123456789abcdef",
    runtime_updated_at: str = "2026-10-23T00:00:00+00:00",
) -> tuple[str, EvaluationRevisionView]:
    active_suite = next(
        suite
        for suite in repository.list_suite_revisions()
        if suite.suite_id == "design03" and suite.review_status == "active" and not suite.stale
    )
    statuses = statuses or {}
    cases = list(active_suite.cases)
    plan_cases = [
        PlanCaseView(
            case_revision_id=case.revision_id,
            case_id=case.case_id,
            question=case.question,
            n=1,
            k=1,
            source_n=case.n,
            source_k=case.k,
            resolved_targets=[],
            execution_policy=case.execution_policy,
        )
        for case in cases
    ]
    plan = repository.create_plan(
        suite_revision_id=active_suite.revision_id,
        collection_snapshot_id=snapshot_id,
        run_type="quality",
        mode=mode,
        agent_version_set={"agent_loop_version": "test"},
        model_configuration={"model_id": "test"},
        run_policy={
            "concurrency": 1,
            "trace_mode": "full",
            "runtime_settings": {
                "max_context_tokens": 100_000,
                "updated_at": runtime_updated_at,
            },
        },
        experiment_variable={},
        checks=[PreflightCheck(code="test", status="passed", message="ok")],
        plan_cases=plan_cases,
    )
    run = repository.create_run(plan.plan_id)
    run = repository.set_run_status(
        run.run_id,
        cast(RunStatus, "sealed_complete"),
        started=True,
        sealed=True,
    )
    case_aggregates = []
    for case in cases:
        result = statuses.get(case.case_id, "pass")
        case_aggregates.append(
            CaseAggregateView(
                case_id=case.case_id,
                case_revision_id=case.revision_id,
                n=1,
                k=1,
                valid_samples=1,
                pass_count=1 if result == "pass" else 0,
                fail_count=1 if result == "fail" else 0,
                pending_count=1 if result == "pending" else 0,
                redline_violation_count=0,
                result=cast(Literal["pass", "fail", "pending", "exploratory"], result),
                reasons=[],
            )
        )
    matrix = build_result_matrix(
        run=run,
        plan_cases=plan_cases,
        cases={case.revision_id: case for case in cases},
        attempt_aggregates=[],
        case_aggregates=case_aggregates,
    )
    revision_id = f"evalrev_{run.run_id.removeprefix('run_')}"
    with database.session() as session, session.begin():
        session.add(
            EvalEvaluationRevisionRow(
                revision_id=revision_id,
                execution_run_id=run.run_id,
                status="complete",
                checker_version_set_json="{}",
                judge_version_set_json="{}",
                config_json="{}",
                summary_json='{"total":18,"pass":18,"fail":0,"pending":0}',
                error_code=None,
                error_message=None,
            )
        )
    revision = EvaluationRevisionView(
        revision_id=revision_id,
        execution_run_id=run.run_id,
        status="complete",
        checker_version_set={},
        judge_version_set={},
        config={},
        summary={"total": 18, "pass": 18, "fail": 0, "pending": 0},
        error_code=None,
        error_message=None,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.sealed_at,
        judgments=[],
        checks=[],
        result_matrix=matrix,
    )
    return run.run_id, revision


def test_baseline_is_idempotent_frozen_and_rebaselines_changed_result_state(
    tmp_path: Path,
) -> None:
    database = EvaluationDatabase(tmp_path / "eval")
    repository = EvaluationRepository(database)
    revisions: dict[str, EvaluationRevisionView] = {}

    def load_revision(revision_id: str) -> EvaluationRevisionView:
        return revisions[revision_id]

    service = EvaluationBaselineService(
        database=database,
        repository=repository,
        revision_loader=load_revision,
    )
    first_run_id, first_revision = _create_complete_run(database, repository)
    revisions[first_revision.revision_id] = first_revision
    request = SetBaselineRequest(
        evaluation_revision_id=first_revision.revision_id,
        reviewer="reviewer-a",
        reason="首个正式基线",
    )

    baseline = service.set_baseline(first_run_id, request)
    repeated = service.set_baseline(first_run_id, request)

    assert baseline.baseline_id == repeated.baseline_id
    assert baseline.is_current is True
    assert baseline.suite_id == "design03"
    assert baseline.totals.case_count == 18
    assert baseline.totals.case_pass_count == 18
    assert service.current_baseline("design03", "quality") == baseline
    assert service.current_baseline("design03", "latency") is None
    with pytest.raises(IntegrityError), database.session() as session, session.begin():
        row = session.get(EvalBaselineRow, baseline.baseline_id)
        assert row is not None
        row.reviewer = "tampered"

    assert first_revision.result_matrix is not None
    changed_revision = first_revision.model_copy(
        update={
            "result_matrix": first_revision.result_matrix.model_copy(
                update={
                    "totals": first_revision.result_matrix.totals.model_copy(
                        update={"case_pass_count": 0, "case_fail_count": 18}
                    )
                }
            )
        }
    )
    revisions[first_revision.revision_id] = changed_revision
    frozen = service.current_baseline("design03", "quality")
    assert frozen is not None
    assert frozen.totals.case_pass_count == 18

    corrected = service.set_baseline(
        first_run_id,
        SetBaselineRequest(
            evaluation_revision_id=first_revision.revision_id,
            reviewer="reviewer-b",
            reason="人工改判后的新基线",
        ),
    )
    repeated_corrected = service.set_baseline(
        first_run_id,
        SetBaselineRequest(
            evaluation_revision_id=first_revision.revision_id,
            reviewer="reviewer-c",
            reason="重复设置不应再追加",
        ),
    )
    history = service.list_baselines("design03", "quality")

    assert corrected.baseline_id != baseline.baseline_id
    assert repeated_corrected.baseline_id == corrected.baseline_id
    assert corrected.totals.case_pass_count == 0
    assert service.current_baseline("design03", "quality") == corrected
    assert {item.baseline_id for item in history} == {
        baseline.baseline_id,
        corrected.baseline_id,
    }
    assert (
        next(
            item for item in history if item.baseline_id == baseline.baseline_id
        ).totals.case_pass_count
        == 18
    )


def test_baseline_switch_keeps_history_and_comparison_detects_regression(
    tmp_path: Path,
) -> None:
    database = EvaluationDatabase(tmp_path / "eval")
    repository = EvaluationRepository(database)
    revisions: dict[str, EvaluationRevisionView] = {}

    def load_revision(revision_id: str) -> EvaluationRevisionView:
        return revisions[revision_id]

    service = EvaluationBaselineService(
        database=database,
        repository=repository,
        revision_loader=load_revision,
    )
    first_run_id, first_revision = _create_complete_run(database, repository)
    second_run_id, second_revision = _create_complete_run(
        database,
        repository,
        statuses={"c1-001": "fail"},
        runtime_updated_at="2026-10-23T01:00:00+00:00",
    )
    revisions[first_revision.revision_id] = first_revision
    revisions[second_revision.revision_id] = second_revision
    first = service.set_baseline(
        first_run_id,
        SetBaselineRequest(
            evaluation_revision_id=first_revision.revision_id,
            reviewer="reviewer-a",
            reason="首个正式基线",
        ),
    )

    comparison = service.compare(second_run_id, second_revision.revision_id)

    assert comparison is not None
    assert comparison.comparable is True
    assert comparison.summary.case_pass_count_delta == -1
    assert comparison.summary.newly_failing_count == 1
    changed = next(item for item in comparison.cases if item.case_id == "c1-001")
    assert changed.change == "regressed"
    assert changed.baseline_result == "pass"
    assert changed.target_result == "fail"

    second = service.set_baseline(
        second_run_id,
        SetBaselineRequest(
            evaluation_revision_id=second_revision.revision_id,
            reviewer="reviewer-b",
            reason="性能与质量调整后的新基线",
        ),
    )
    current = service.current_baseline("design03", "quality")
    history = service.list_baselines("design03", "quality")

    assert second.baseline_id != first.baseline_id
    assert current is not None and current.baseline_id == second.baseline_id
    assert {item.baseline_id for item in history} == {
        first.baseline_id,
        second.baseline_id,
    }
    assert sum(item.is_current for item in history) == 1

    third_run_id, third_revision = _create_complete_run(
        database,
        repository,
        snapshot_id="s_fedcba9876543210",
    )
    revisions[third_revision.revision_id] = third_revision
    cross_scene = service.compare(third_run_id, third_revision.revision_id)
    assert cross_scene is not None
    assert cross_scene.comparable is False
    assert "collection_snapshot_changed" in cross_scene.incompatibility_reasons


def test_incomplete_or_exploratory_run_cannot_be_baseline(tmp_path: Path) -> None:
    database = EvaluationDatabase(tmp_path / "eval")
    repository = EvaluationRepository(database)
    revisions: dict[str, EvaluationRevisionView] = {}

    def load_revision(revision_id: str) -> EvaluationRevisionView:
        return revisions[revision_id]

    service = EvaluationBaselineService(
        database=database,
        repository=repository,
        revision_loader=load_revision,
    )
    pending_run_id, pending_revision = _create_complete_run(
        database,
        repository,
        statuses={"c1-001": "pending"},
    )
    revisions[pending_revision.revision_id] = pending_revision
    with pytest.raises(ValueError, match="case is not final"):
        service.set_baseline(
            pending_run_id,
            SetBaselineRequest(
                evaluation_revision_id=pending_revision.revision_id,
                reviewer="reviewer",
                reason="不应成功",
            ),
        )

    exploratory_run_id, exploratory_revision = _create_complete_run(
        database,
        repository,
        mode="exploratory",
    )
    revisions[exploratory_revision.revision_id] = exploratory_revision
    with pytest.raises(ValueError, match="exploratory"):
        service.set_baseline(
            exploratory_run_id,
            SetBaselineRequest(
                evaluation_revision_id=exploratory_revision.revision_id,
                reviewer="reviewer",
                reason="不应成功",
            ),
        )
