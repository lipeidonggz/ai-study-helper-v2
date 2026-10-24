from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import select

from app.evaluation.database import EvaluationDatabase
from app.evaluation.models import (
    BaselineCaseDeltaView,
    BaselineComparisonSummaryView,
    BaselineComparisonView,
    BaselineView,
    EvaluationResultMatrixView,
    EvaluationRevisionView,
    ExecutionPlanView,
    ResultMatrixCaseView,
    ResultMatrixTotalsView,
    RunType,
    RunView,
    SetBaselineRequest,
    SuiteRevisionView,
)
from app.evaluation.repository import EvaluationRepository
from app.evaluation.storage_models import (
    EvalBaselinePointerRow,
    EvalBaselineRow,
)
from app.storage.models import utc_iso, utc_now

AGGREGATE_RULE_VERSION = "023b-nk-v1"


@dataclass(frozen=True, slots=True)
class _BaselineCandidate:
    run: RunView
    plan: ExecutionPlanView
    suite: SuiteRevisionView
    revision: EvaluationRevisionView
    matrix: EvaluationResultMatrixView
    case_revision_ids: tuple[str, ...]


class EvaluationBaselineService:
    """人工管理不可变评测基线，并投影目标 run 相对基线的差异。"""

    def __init__(
        self,
        *,
        database: EvaluationDatabase,
        repository: EvaluationRepository,
        revision_loader: Callable[[str], EvaluationRevisionView],
    ) -> None:
        self._database = database
        self._repository = repository
        self._revision_loader = revision_loader

    def set_baseline(self, run_id: str, request: SetBaselineRequest) -> BaselineView:
        """校验资格后追加 Baseline，并把 scope 当前指针切到该基线。"""

        candidate = self._candidate(run_id, request.evaluation_revision_id)
        matrix_json = _json(candidate.matrix.model_dump(mode="json"))
        matrix_hash = hashlib.sha256(matrix_json.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._database.session() as session, session.begin():
            existing = session.scalar(
                select(EvalBaselineRow).where(
                    EvalBaselineRow.suite_id == candidate.run.suite_id,
                    EvalBaselineRow.run_type == candidate.plan.run_type,
                    EvalBaselineRow.execution_run_id == run_id,
                    EvalBaselineRow.evaluation_revision_id == request.evaluation_revision_id,
                    EvalBaselineRow.result_matrix_hash == matrix_hash,
                )
            )
            if existing is None:
                existing = EvalBaselineRow(
                    baseline_id=f"baseline_{uuid4().hex}",
                    suite_id=cast(str, candidate.run.suite_id),
                    scope_id=candidate.suite.scope_id,
                    run_type=candidate.plan.run_type,
                    suite_revision_id=candidate.suite.revision_id,
                    collection_snapshot_id=candidate.plan.collection_snapshot_id,
                    plan_id=candidate.plan.plan_id,
                    execution_run_id=run_id,
                    evaluation_revision_id=request.evaluation_revision_id,
                    case_revision_ids_json=_json(list(candidate.case_revision_ids)),
                    run_policy_json=_json(candidate.plan.run_policy),
                    aggregate_rule_version=AGGREGATE_RULE_VERSION,
                    result_matrix_hash=matrix_hash,
                    result_matrix_json=matrix_json,
                    run_started_at=_parse_datetime(candidate.run.started_at),
                    run_sealed_at=_parse_datetime(candidate.run.sealed_at),
                    reviewer=request.reviewer,
                    reason=request.reason,
                    created_at=now,
                )
                session.add(existing)
                session.flush()
            pointer = session.get(
                EvalBaselinePointerRow,
                (cast(str, candidate.run.suite_id), candidate.plan.run_type),
            )
            if pointer is None:
                session.add(
                    EvalBaselinePointerRow(
                        suite_id=cast(str, candidate.run.suite_id),
                        run_type=candidate.plan.run_type,
                        baseline_id=existing.baseline_id,
                        updated_by=request.reviewer,
                        updated_at=now,
                    )
                )
            else:
                pointer.baseline_id = existing.baseline_id
                pointer.updated_by = request.reviewer
                pointer.updated_at = now
            session.flush()
            return self._baseline_view(
                existing,
                suite_name=candidate.run.suite_name or candidate.suite.name,
                is_current=True,
            )

    def current_baseline(
        self,
        suite_id: str,
        run_type: str,
    ) -> BaselineView | None:
        """返回指定 suite 与 run type 的当前基线。"""

        with self._database.session() as session:
            pointer = session.get(EvalBaselinePointerRow, (suite_id, run_type))
            if pointer is None:
                return None
            row = session.get(EvalBaselineRow, pointer.baseline_id)
            if row is None:
                raise RuntimeError("baseline pointer references a missing baseline")
            return self._baseline_view(row, suite_name=self._suite_name(suite_id), is_current=True)

    def list_baselines(
        self,
        suite_id: str,
        run_type: str,
        *,
        limit: int = 20,
    ) -> list[BaselineView]:
        """按最新优先返回指定范围的基线历史。"""

        with self._database.session() as session:
            pointer = session.get(EvalBaselinePointerRow, (suite_id, run_type))
            rows = session.scalars(
                select(EvalBaselineRow)
                .where(
                    EvalBaselineRow.suite_id == suite_id,
                    EvalBaselineRow.run_type == run_type,
                )
                .order_by(EvalBaselineRow.created_at.desc())
                .limit(limit)
            ).all()
            suite_name = self._suite_name(suite_id)
            return [
                self._baseline_view(
                    row,
                    suite_name=suite_name,
                    is_current=pointer is not None and pointer.baseline_id == row.baseline_id,
                )
                for row in rows
            ]

    def compare(
        self,
        run_id: str,
        evaluation_revision_id: str,
    ) -> BaselineComparisonView | None:
        """比较目标 run 与同 suite／run type 的当前基线。"""

        target = self._candidate(run_id, evaluation_revision_id)
        baseline = self.current_baseline(cast(str, target.run.suite_id), target.plan.run_type)
        if baseline is None:
            return None
        baseline_matrix = self._baseline_matrix(baseline.baseline_id)
        baseline_row = self._baseline_row(baseline.baseline_id)
        baseline_case_ids = tuple(json.loads(baseline_row.case_revision_ids_json))
        incompatibilities: list[str] = []
        if baseline.suite_revision_id != target.suite.revision_id:
            incompatibilities.append("suite_revision_changed")
        if baseline_case_ids != target.case_revision_ids:
            incompatibilities.append("case_revision_set_changed")
        if baseline.collection_snapshot_id != target.plan.collection_snapshot_id:
            incompatibilities.append("collection_snapshot_changed")
        if _normalized_run_policy(json.loads(baseline_row.run_policy_json)) != (
            _normalized_run_policy(target.plan.run_policy)
        ):
            incompatibilities.append("run_policy_changed")
        if baseline.aggregate_rule_version != AGGREGATE_RULE_VERSION:
            incompatibilities.append("aggregate_rule_changed")

        baseline_cases = _matrix_cases(baseline_matrix)
        target_cases = _matrix_cases(target.matrix)
        case_ids = list(dict.fromkeys([*baseline_cases.keys(), *target_cases.keys()]))
        case_deltas = [
            _case_delta(
                case_id,
                baseline_cases.get(case_id),
                target_cases.get(case_id),
            )
            for case_id in case_ids
        ]
        summary = _comparison_summary(baseline_matrix.totals, target.matrix.totals, case_deltas)
        return BaselineComparisonView(
            baseline=baseline,
            target_run_id=target.run.run_id,
            target_evaluation_revision_id=target.revision.revision_id,
            target_suite_revision_id=target.suite.revision_id,
            target_collection_snapshot_id=target.plan.collection_snapshot_id,
            target_totals=target.matrix.totals,
            comparable=not incompatibilities,
            incompatibility_reasons=incompatibilities,
            summary=summary,
            cases=case_deltas,
        )

    def _candidate(self, run_id: str, revision_id: str) -> _BaselineCandidate:
        run = self._repository.get_run(run_id)
        if run.plan_mode != "formal":
            raise ValueError("exploratory run cannot become a baseline or be compared")
        if run.status != "sealed_complete":
            raise ValueError("only sealed_complete runs can become baselines or be compared")
        if run.suite_id is None or run.suite_revision_id is None:
            raise ValueError("run has no suite identity")
        plan = self._repository.get_plan(run.plan_id)
        if plan.mode != "formal":
            raise ValueError("only formal plans can become baselines")
        suite = self._repository.get_suite_revision(run.suite_revision_id)
        revision = self._revision_loader(revision_id)
        if revision.execution_run_id != run.run_id:
            raise ValueError("evaluation revision does not belong to run")
        if revision.status != "complete" or revision.result_matrix is None:
            raise ValueError("evaluation revision is incomplete")
        matrix = revision.result_matrix
        if not matrix.categories or not matrix.totals.case_count:
            raise ValueError("evaluation run has no cases")
        for case in _matrix_cases(matrix).values():
            if case.result == "pending" or case.result == "exploratory":
                raise ValueError(f"case is not final: {case.case_id}")
            if case.valid_samples != case.n:
                raise ValueError(f"case has incomplete valid samples: {case.case_id}")
            if case.pending_count:
                raise ValueError(f"case still has pending samples: {case.case_id}")
        case_revision_ids = tuple(case.case_revision_id for case in plan.cases)
        suite_case_revision_ids = tuple(case.revision_id for case in suite.cases)
        if case_revision_ids != suite_case_revision_ids:
            raise ValueError("run case revisions do not match its suite revision")
        return _BaselineCandidate(
            run=run,
            plan=plan,
            suite=suite,
            revision=revision,
            matrix=matrix,
            case_revision_ids=case_revision_ids,
        )

    def _baseline_row(self, baseline_id: str) -> EvalBaselineRow:
        with self._database.session() as session:
            row = session.get(EvalBaselineRow, baseline_id)
            if row is None:
                raise KeyError(baseline_id)
            return row

    def _baseline_matrix(self, baseline_id: str) -> EvaluationResultMatrixView:
        row = self._baseline_row(baseline_id)
        return EvaluationResultMatrixView.model_validate(json.loads(row.result_matrix_json))

    def _baseline_view(
        self,
        row: EvalBaselineRow,
        *,
        suite_name: str,
        is_current: bool,
    ) -> BaselineView:
        matrix = EvaluationResultMatrixView.model_validate(json.loads(row.result_matrix_json))
        return BaselineView(
            baseline_id=row.baseline_id,
            suite_id=row.suite_id,
            suite_name=suite_name,
            scope_id=row.scope_id,
            run_type=cast(RunType, row.run_type),
            suite_revision_id=row.suite_revision_id,
            collection_snapshot_id=row.collection_snapshot_id,
            execution_run_id=row.execution_run_id,
            evaluation_revision_id=row.evaluation_revision_id,
            case_revision_ids=json.loads(row.case_revision_ids_json),
            aggregate_rule_version=row.aggregate_rule_version,
            totals=matrix.totals,
            run_started_at=None if row.run_started_at is None else utc_iso(row.run_started_at),
            run_sealed_at=None if row.run_sealed_at is None else utc_iso(row.run_sealed_at),
            reviewer=row.reviewer,
            reason=row.reason,
            created_at=utc_iso(row.created_at),
            is_current=is_current,
        )

    def _suite_name(self, suite_id: str) -> str:
        suites = [
            suite for suite in self._repository.list_suite_revisions() if suite.suite_id == suite_id
        ]
        if not suites:
            raise KeyError(suite_id)
        return suites[0].name or suite_id


def _matrix_cases(matrix: EvaluationResultMatrixView) -> dict[str, ResultMatrixCaseView]:
    cases: dict[str, ResultMatrixCaseView] = {}
    for category in matrix.categories:
        for case in category.cases:
            cases[case.case_id] = case
    return cases


def _case_delta(
    case_id: str,
    baseline: ResultMatrixCaseView | None,
    target: ResultMatrixCaseView | None,
) -> BaselineCaseDeltaView:
    baseline_result = None if baseline is None else baseline.result
    target_result = None if target is None else target.result
    if baseline is None:
        change = "new"
    elif target is None:
        change = "removed"
    elif baseline.result == target.result:
        change = "unchanged"
    elif baseline.result == "fail" and target.result == "pass":
        change = "improved"
    elif baseline.result == "pass" and target.result == "fail":
        change = "regressed"
    else:
        change = "changed"
    source = target or baseline
    if source is None:
        raise AssertionError("case delta requires baseline or target")
    return BaselineCaseDeltaView(
        case_id=case_id,
        category=source.category,
        baseline_case_revision_id=None if baseline is None else baseline.case_revision_id,
        target_case_revision_id=None if target is None else target.case_revision_id,
        baseline_result=baseline_result,
        target_result=target_result,
        change=cast(
            Literal["new", "removed", "improved", "regressed", "changed", "unchanged"],
            change,
        ),
        revision_changed=(
            baseline is not None
            and target is not None
            and baseline.case_revision_id != target.case_revision_id
        ),
        valid_samples_delta=_delta(baseline, target, "valid_samples"),
        pass_count_delta=_delta(baseline, target, "pass_count"),
        fail_count_delta=_delta(baseline, target, "fail_count"),
        pending_count_delta=_delta(baseline, target, "pending_count"),
        redline_violation_count_delta=_delta(
            baseline,
            target,
            "redline_violation_count",
        ),
        input_tokens_delta=_delta(baseline, target, "input_tokens"),
        output_tokens_delta=_delta(baseline, target, "output_tokens"),
        model_calls_delta=_delta(baseline, target, "model_calls"),
        tool_calls_delta=_delta(baseline, target, "tool_calls"),
        duration_ms_delta=_delta(baseline, target, "duration_ms"),
    )


def _comparison_summary(
    baseline: ResultMatrixTotalsView,
    target: ResultMatrixTotalsView,
    cases: list[BaselineCaseDeltaView],
) -> BaselineComparisonSummaryView:
    return BaselineComparisonSummaryView(
        case_pass_count_delta=target.case_pass_count - baseline.case_pass_count,
        case_fail_count_delta=target.case_fail_count - baseline.case_fail_count,
        case_pending_count_delta=target.case_pending_count - baseline.case_pending_count,
        case_exploratory_count_delta=(
            target.case_exploratory_count - baseline.case_exploratory_count
        ),
        valid_samples_delta=target.valid_samples - baseline.valid_samples,
        redline_violation_count_delta=(
            target.redline_violation_count - baseline.redline_violation_count
        ),
        input_tokens_delta=target.input_tokens - baseline.input_tokens,
        output_tokens_delta=target.output_tokens - baseline.output_tokens,
        model_calls_delta=target.model_calls - baseline.model_calls,
        tool_calls_delta=target.tool_calls - baseline.tool_calls,
        duration_ms_delta=target.duration_ms - baseline.duration_ms,
        newly_passing_count=sum(case.change == "improved" for case in cases),
        newly_failing_count=sum(case.change == "regressed" for case in cases),
    )


def _delta(
    baseline: ResultMatrixCaseView | None,
    target: ResultMatrixCaseView | None,
    field: str,
) -> int:
    baseline_value = 0 if baseline is None else int(getattr(baseline, field))
    target_value = 0 if target is None else int(getattr(target, field))
    return target_value - baseline_value


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_run_policy(value: dict[str, object]) -> dict[str, object]:
    """移除不影响运行语义的配置更新时间，保留所有行为参数。"""

    normalized = dict(value)
    runtime_settings = normalized.get("runtime_settings")
    if isinstance(runtime_settings, dict):
        normalized_settings = dict(runtime_settings)
        normalized_settings.pop("updated_at", None)
        normalized["runtime_settings"] = normalized_settings
    return normalized


def _parse_datetime(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
