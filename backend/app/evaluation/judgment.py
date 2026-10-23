from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select

from app.evaluation.aggregation import build_aggregates
from app.evaluation.citation_prompt import (
    CITATION_SUPPORT_PROMPT_HASH,
    CITATION_SUPPORT_PROMPT_VERSION,
    CitationEvidenceInput,
    CitationSupportInput,
    render_citation_messages,
)
from app.evaluation.database import EvaluationDatabase
from app.evaluation.evidence import (
    MAX_JUDGE_EVIDENCE_CHARS,
    BuiltJudgeInput,
    JudgeCriterionInput,
    JudgeInputError,
    JudgeInputPacket,
    build_judge_input,
)
from app.evaluation.judge_prompt import (
    JUDGE_PROMPT_HASH,
    JUDGE_PROMPT_VERSION,
    render_judge_messages,
)
from app.evaluation.models import (
    AttemptView,
    CaseRevisionView,
    CheckResultView,
    CheckReviewRequest,
    CheckReviewView,
    CriterionView,
    EffectiveJudgmentBindingView,
    EvaluationRevisionView,
    JudgeInvocationView,
    JudgeOverrideRequest,
    JudgmentView,
    PlanCaseView,
    RunView,
    StartJudgmentRequest,
)
from app.evaluation.outcomes import is_product_sample
from app.evaluation.repository import EvaluationRepository
from app.evaluation.storage_models import (
    EvalAttemptRow,
    EvalCaseRevisionCriterionRow,
    EvalCheckResultRow,
    EvalCheckReviewRow,
    EvalCriterionRevisionRow,
    EvalEvaluationRevisionRow,
    EvalJudgeInvocationRow,
    EvalJudgmentBindingRow,
    EvalJudgmentRow,
)
from app.providers.chat import (
    ChatCompletionResult,
    ChatProvider,
    ChatProviderError,
    DeepSeekChatProvider,
)
from app.storage.credentials import CHAT_API_KEY_USERNAME, CredentialStore
from app.storage.models import utc_iso, utc_now
from app.tracing.catalog import TraceCatalog

CHECKER_VERSIONS = {
    "citation_integrity": "1",
    "citation_support": "1",
    "truncation_declared": "1",
}
JUDGE_MAX_OUTPUT_TOKENS = 2_048


class JudgeOutput(BaseModel):
    """LLM Judge 必须返回的严格结构化结论。"""

    model_config = ConfigDict(extra="forbid")

    verdict: str
    reason: str = Field(min_length=1)


class CitationResultOutput(BaseModel):
    """引用判官对单个 block 的结构化结论。"""

    model_config = ConfigDict(extra="forbid")

    block_id: str
    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = Field(min_length=1)
    evidence_quotes: list[str]


class CitationSupportOutput(BaseModel):
    """引用支持检查的严格结构化结论。"""

    model_config = ConfigDict(extra="forbid")

    citations: list[CitationResultOutput]


@dataclass(frozen=True, slots=True)
class _InvocationDraft:
    """尚未持久化的单次 provider 调用。"""

    attempt_number: int
    status: str
    input_json: str
    raw_output: str | None
    error_code: str | None
    input_tokens: int
    output_tokens: int
    request_id: str | None
    started_at: datetime
    finished_at: datetime


@dataclass(frozen=True, slots=True)
class _TaskOutcome:
    """一个 criterion 任务的最终事实。"""

    attempt_id: str
    criterion: CriterionView
    executor: str
    executor_ref: str
    verdict: str
    normalized_result: str
    reason: str | None
    evidence: dict[str, Any]
    error_code: str | None
    invocations: tuple[_InvocationDraft, ...]


class EvaluationJudgmentService:
    """执行 checker／LLM Judge 并把结果持久化为 append-only Judgment。"""

    def __init__(
        self,
        *,
        database: EvaluationDatabase,
        repository: EvaluationRepository,
        trace_catalog: TraceCatalog,
        credentials: CredentialStore,
        chat_provider_factory: Callable[[str], ChatProvider] | None,
    ) -> None:
        self._database = database
        self._repository = repository
        self._trace_catalog = trace_catalog
        self._credentials = credentials
        self._chat_provider_factory = chat_provider_factory
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._recover_interrupted_revisions()

    async def start(
        self,
        run_id: str,
        request: StartJudgmentRequest,
    ) -> EvaluationRevisionView:
        """验证 run 现场并启动后台判定。"""

        if not request.judge_review_confirmed:
            raise ValueError("judge prompt review must be confirmed before judging")
        run = self._repository.get_run(run_id)
        if run.status not in {"sealed_complete", "sealed_partial", "canceled"}:
            raise ValueError("only terminal or canceled runs can be judged")
        case_revision_ids = list(
            dict.fromkeys(attempt.case_revision_id for attempt in run.attempts)
        )
        for case in self._repository.get_case_revisions(case_revision_ids):
            if any(
                criterion.evaluator == "llm_judge"
                and criterion.evaluator_ref != JUDGE_PROMPT_VERSION
                for criterion in case.criteria
            ):
                raise ValueError(
                    "run criteria use a different judge prompt revision; create a new run"
                )
        with self._database.session() as session, session.begin():
            active = session.scalar(
                select(EvalEvaluationRevisionRow).where(
                    EvalEvaluationRevisionRow.execution_run_id == run_id,
                    EvalEvaluationRevisionRow.status == "running",
                )
            )
            if active is not None:
                raise ValueError("this run already has a running judgment revision")
            attempts = session.scalars(
                select(EvalAttemptRow)
                .where(
                    EvalAttemptRow.run_id == run_id,
                    EvalAttemptRow.outcome_class == "product",
                )
                .order_by(EvalAttemptRow.started_at, EvalAttemptRow.attempt_id)
            ).all()
            if not attempts:
                raise ValueError("run has no eligible attempts for judging")
            revision_id = f"evalrev_{uuid4().hex}"
            model_profile = self._chat_provider().profile
            config = {
                "concurrency": request.concurrency,
                "token_budget": request.token_budget,
                "request_budget": request.request_budget,
                "attempt_timeout_seconds": request.attempt_timeout_seconds,
            }
            session.add(
                EvalEvaluationRevisionRow(
                    revision_id=revision_id,
                    execution_run_id=run_id,
                    status="running",
                    checker_version_set_json=_json(CHECKER_VERSIONS),
                    judge_version_set_json=_json(
                        {
                            "prompt_version": JUDGE_PROMPT_VERSION,
                            "prompt_hash": JUDGE_PROMPT_HASH,
                            "citation_prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                            "citation_prompt_hash": CITATION_SUPPORT_PROMPT_HASH,
                            "model_id": model_profile.model_id,
                            "temperature": model_profile.temperature,
                            "thinking_enabled": model_profile.thinking_enabled,
                        }
                    ),
                    config_json=_json(config),
                    summary_json=_json({"total": 0, "pass": 0, "fail": 0, "pending": 0}),
                    started_at=utc_now(),
                )
            )
        task = asyncio.create_task(
            self._execute(revision_id, run, request),
            name=f"evaluation-judgment-{revision_id}",
        )
        self._tasks[revision_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(revision_id, None))
        return self.get_revision(revision_id)

    async def shutdown(self) -> None:
        """关闭判定后台任务；重启恢复按 incomplete 处理。"""

        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def get_revision(self, revision_id: str) -> EvaluationRevisionView:
        """读取 EvaluationRevision、Judgments 和 checker 结果。"""

        with self._database.session() as session:
            row = session.get(EvalEvaluationRevisionRow, revision_id)
            if row is None:
                raise KeyError(revision_id)
            judgments = session.scalars(
                select(EvalJudgmentRow)
                .where(EvalJudgmentRow.evaluation_revision_id == revision_id)
                .order_by(EvalJudgmentRow.created_at, EvalJudgmentRow.judgment_id)
            ).all()
            checks = session.scalars(
                select(EvalCheckResultRow)
                .where(EvalCheckResultRow.evaluation_revision_id == revision_id)
                .order_by(EvalCheckResultRow.created_at, EvalCheckResultRow.check_result_id)
            ).all()
            binding_rows = session.scalars(
                select(EvalJudgmentBindingRow)
                .where(EvalJudgmentBindingRow.evaluation_revision_id == revision_id)
                .order_by(EvalJudgmentBindingRow.binding_order)
            ).all()
            review_rows = session.scalars(
                select(EvalCheckReviewRow)
                .where(EvalCheckReviewRow.evaluation_revision_id == revision_id)
                .order_by(EvalCheckReviewRow.review_order)
            ).all()
            invocation_rows = session.scalars(
                select(EvalJudgeInvocationRow)
                .where(
                    EvalJudgeInvocationRow.judgment_id.in_(
                        [judgment.judgment_id for judgment in judgments]
                    )
                )
                .order_by(
                    EvalJudgeInvocationRow.started_at,
                    EvalJudgeInvocationRow.attempt_number,
                )
            ).all()
            invocations_by_judgment: dict[str, list[JudgeInvocationView]] = {}
            for invocation in invocation_rows:
                invocations_by_judgment.setdefault(
                    invocation.judgment_id,
                    [],
                ).append(
                    JudgeInvocationView(
                        invocation_id=invocation.invocation_id,
                        attempt_number=invocation.attempt_number,
                        prompt_version=invocation.prompt_version,
                        model_id=invocation.model_id,
                        status=invocation.status,
                        error_code=invocation.error_code,
                        input_tokens=invocation.input_tokens,
                        output_tokens=invocation.output_tokens,
                        request_id=invocation.request_id,
                        input_json=invocation.input_json,
                        raw_output=invocation.raw_output,
                        started_at=utc_iso(invocation.started_at),
                        finished_at=(
                            None
                            if invocation.finished_at is None
                            else utc_iso(invocation.finished_at)
                        ),
                    )
                )
            judgment_views = [
                JudgmentView(
                    judgment_id=judgment.judgment_id,
                    attempt_id=judgment.attempt_id,
                    criterion_revision_id=judgment.criterion_revision_id,
                    executor=judgment.executor,
                    executor_ref=judgment.executor_ref,
                    verdict=judgment.verdict,
                    normalized_result=judgment.normalized_result,
                    reason=judgment.reason,
                    evidence=json.loads(judgment.evidence_json),
                    error_code=judgment.error_code,
                    created_at=utc_iso(judgment.created_at),
                    invocations=invocations_by_judgment.get(
                        judgment.judgment_id,
                        [],
                    ),
                )
                for judgment in judgments
            ]
            judgments_by_id = {judgment.judgment_id: judgment for judgment in judgment_views}
            latest_bindings = {
                (binding.attempt_id, binding.criterion_revision_id): binding
                for binding in binding_rows
            }
            latest_binding_ids = {binding.binding_id for binding in latest_bindings.values()}
            binding_history_views = []
            for binding in binding_rows:
                judgment = judgments_by_id.get(binding.judgment_id)
                if judgment is None:
                    raise RuntimeError(
                        f"binding references missing judgment: {binding.judgment_id}"
                    )
                binding_history_views.append(
                    EffectiveJudgmentBindingView(
                        binding_id=binding.binding_id,
                        attempt_id=binding.attempt_id,
                        criterion_revision_id=binding.criterion_revision_id,
                        judgment_id=binding.judgment_id,
                        executor=judgment.executor,
                        verdict=judgment.verdict,
                        normalized_result=cast(Any, judgment.normalized_result),
                        is_effective=binding.binding_id in latest_binding_ids,
                        bound_at=utc_iso(binding.bound_at),
                    )
                )
            binding_views = [binding for binding in binding_history_views if binding.is_effective]
            check_views = [
                CheckResultView(
                    check_result_id=check.check_result_id,
                    attempt_id=check.attempt_id,
                    checker_id=check.checker_id,
                    checker_version=check.checker_version,
                    result=_expect_check_result(check.result),
                    payload=json.loads(check.payload_json),
                    error_code=check.error_code,
                    created_at=utc_iso(check.created_at),
                )
                for check in checks
            ]
            review_views = [
                CheckReviewView(
                    review_id=review.review_id,
                    attempt_id=review.attempt_id,
                    checker_id=review.checker_id,
                    source_check_result_id=review.source_check_result_id,
                    result=_expect_check_result(review.result),
                    payload=json.loads(review.payload_json),
                    error_code=review.error_code,
                    reason=review.reason,
                    reviewer=review.reviewer,
                    created_at=utc_iso(review.created_at),
                )
                for review in review_rows
            ]
            run = self._repository.get_run(row.execution_run_id)
            case_revision_ids = list(
                dict.fromkeys(attempt.case_revision_id for attempt in run.attempts)
            )
            case_views = {
                case.revision_id: case
                for case in self._repository.get_case_revisions(case_revision_ids)
            }
            try:
                plan_cases = self._repository.get_plan(run.plan_id).cases
            except (KeyError, ValueError):
                plan_cases = []
            planned_revision_ids = {plan_case.case_revision_id for plan_case in plan_cases}
            for case in case_views.values():
                if case.revision_id in planned_revision_ids:
                    continue
                plan_cases.append(
                    PlanCaseView(
                        case_revision_id=case.revision_id,
                        case_id=case.case_id,
                        question=case.question,
                        n=case.n,
                        k=case.k,
                        source_n=case.n,
                        source_k=case.k,
                        resolved_targets=[],
                        execution_policy=case.execution_policy,
                    )
                )
            attempt_aggregates, case_aggregates = build_aggregates(
                run=run,
                plan_cases=plan_cases,
                cases=case_views,
                bindings=binding_views,
                checks=check_views,
                reviews=review_views,
            )
            return EvaluationRevisionView(
                revision_id=row.revision_id,
                execution_run_id=row.execution_run_id,
                status=_expect_status(row.status),
                checker_version_set=json.loads(row.checker_version_set_json),
                judge_version_set=json.loads(row.judge_version_set_json),
                config=json.loads(row.config_json),
                summary=json.loads(row.summary_json),
                error_code=row.error_code,
                error_message=row.error_message,
                created_at=utc_iso(row.created_at),
                started_at=None if row.started_at is None else utc_iso(row.started_at),
                finished_at=None if row.finished_at is None else utc_iso(row.finished_at),
                judgments=judgment_views,
                judgment_bindings=binding_history_views,
                effective_bindings=binding_views,
                check_reviews=review_views,
                checks=check_views,
                attempt_aggregates=attempt_aggregates,
                case_aggregates=case_aggregates,
            )

    def override_judgment(
        self,
        revision_id: str,
        request: JudgeOverrideRequest,
    ) -> EvaluationRevisionView:
        """追加人工语义 Judgment，并绑定为当前生效判定。"""

        with self._database.session() as session, session.begin():
            revision = session.get(EvalEvaluationRevisionRow, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.status == "running":
                raise ValueError("cannot override a running evaluation revision")
            attempt = session.get(EvalAttemptRow, request.attempt_id)
            if attempt is None or attempt.run_id != revision.execution_run_id:
                raise ValueError("attempt does not belong to this evaluation run")
            criterion = session.get(
                EvalCriterionRevisionRow,
                request.criterion_revision_id,
            )
            link = session.get(
                EvalCaseRevisionCriterionRow,
                (attempt.case_revision_id, request.criterion_revision_id),
            )
            if criterion is None or link is None:
                raise ValueError("criterion does not belong to this attempt")
            if criterion.evaluator != "llm_judge":
                raise ValueError("only llm_judge criteria can be manually overridden")
            normalized_result = _normalize_human_verdict(
                criterion.rule_type,
                request.verdict,
            )
            judgment_id = f"judgment_{uuid4().hex}"
            session.add(
                EvalJudgmentRow(
                    judgment_id=judgment_id,
                    evaluation_revision_id=revision_id,
                    attempt_id=attempt.attempt_id,
                    criterion_revision_id=criterion.revision_id,
                    executor="human",
                    executor_ref=request.reviewer,
                    verdict=request.verdict,
                    normalized_result=normalized_result,
                    reason=request.reason,
                    evidence_json=_json({"reviewer": request.reviewer}),
                    error_code=None,
                )
            )
            session.flush()
            session.add(
                EvalJudgmentBindingRow(
                    binding_id=f"binding_{uuid4().hex}",
                    evaluation_revision_id=revision_id,
                    attempt_id=attempt.attempt_id,
                    criterion_revision_id=criterion.revision_id,
                    judgment_id=judgment_id,
                    bound_at=utc_now(),
                )
            )
        self._refresh_effective_revision_state(revision_id)
        return self.get_revision(revision_id)

    def review_llm_check(
        self,
        revision_id: str,
        request: CheckReviewRequest,
    ) -> EvaluationRevisionView:
        """追加人工逐 block 引用支持复核，并重新计算有效检查结果。"""

        if request.checker_id != "citation_support":
            raise ValueError("only citation_support check results can be reviewed")
        with self._database.session() as session, session.begin():
            revision = session.get(EvalEvaluationRevisionRow, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            if revision.status == "running":
                raise ValueError("cannot review a running evaluation revision")
            attempt = session.get(EvalAttemptRow, request.attempt_id)
            if attempt is None or attempt.run_id != revision.execution_run_id:
                raise ValueError("attempt does not belong to this evaluation run")
            source = session.scalar(
                select(EvalCheckResultRow)
                .where(
                    EvalCheckResultRow.evaluation_revision_id == revision_id,
                    EvalCheckResultRow.attempt_id == request.attempt_id,
                    EvalCheckResultRow.checker_id == request.checker_id,
                )
                .order_by(EvalCheckResultRow.created_at.desc())
            )
            integrity = session.scalar(
                select(EvalCheckResultRow).where(
                    EvalCheckResultRow.evaluation_revision_id == revision_id,
                    EvalCheckResultRow.attempt_id == request.attempt_id,
                    EvalCheckResultRow.checker_id == "citation_integrity",
                )
            )
            if source is None or integrity is None:
                raise ValueError("citation support check facts are incomplete")
            if integrity.result != "pass":
                raise ValueError("citation integrity must pass before support review")
            expected_ids = set(json.loads(integrity.payload_json).get("cited_block_ids", []))
            submitted_ids = [item.block_id for item in request.citations]
            if len(submitted_ids) != len(set(submitted_ids)):
                raise ValueError("citation review contains duplicate block ids")
            if set(submitted_ids) != expected_ids:
                raise ValueError("citation review block set does not match the answer")
            if "unsupported" in {item.verdict for item in request.citations}:
                result = "fail"
                error_code = "citation_not_supported"
            elif "unknown" in {item.verdict for item in request.citations}:
                result = "pending"
                error_code = "citation_support_unknown"
            else:
                result = "pass"
                error_code = None
            session.add(
                EvalCheckReviewRow(
                    review_id=f"review_{uuid4().hex}",
                    evaluation_revision_id=revision_id,
                    attempt_id=request.attempt_id,
                    checker_id=request.checker_id,
                    source_check_result_id=source.check_result_id,
                    result=result,
                    payload_json=_json(
                        {
                            "citations": [
                                item.model_dump(mode="json") for item in request.citations
                            ],
                            "reviewer": request.reviewer,
                            "reason": request.reason,
                        }
                    ),
                    error_code=error_code,
                    reason=request.reason,
                    reviewer=request.reviewer,
                    created_at=utc_now(),
                )
            )
        self._refresh_effective_revision_state(revision_id)
        return self.get_revision(revision_id)

    def _refresh_effective_revision_state(self, revision_id: str) -> None:
        """按当前绑定与人工复核重算进度，不修改底层历史记录。"""

        revision = self.get_revision(revision_id)
        counts = Counter(binding.normalized_result for binding in revision.effective_bindings)
        is_incomplete = counts["pending"] > 0 or any(
            item.result == "pending" for item in revision.attempt_aggregates
        )
        with self._database.session() as session, session.begin():
            row = session.get(EvalEvaluationRevisionRow, revision_id)
            if row is None:
                raise KeyError(revision_id)
            row.status = "incomplete" if is_incomplete else "complete"
            row.summary_json = _json(
                {
                    "total": len(revision.effective_bindings),
                    "pass": counts["pass"],
                    "fail": counts["fail"],
                    "pending": counts["pending"],
                }
            )
            if not is_incomplete:
                row.error_code = None
                row.error_message = None

    def list_run_revisions(self, run_id: str) -> list[EvaluationRevisionView]:
        """返回一个 run 的全部判定修订版，按创建时间倒序。"""

        with self._database.session() as session:
            revision_ids = session.scalars(
                select(EvalEvaluationRevisionRow.revision_id)
                .where(EvalEvaluationRevisionRow.execution_run_id == run_id)
                .order_by(EvalEvaluationRevisionRow.created_at.desc())
            ).all()
        return [self.get_revision(revision_id) for revision_id in revision_ids]

    async def _execute(
        self,
        revision_id: str,
        run: RunView,
        request: StartJudgmentRequest,
    ) -> None:
        try:
            tasks, citation_tasks = await asyncio.to_thread(
                self._prepare_tasks,
                revision_id,
                run,
            )
            self._update_revision(
                revision_id,
                total=len(tasks),
            )
            semaphore = asyncio.Semaphore(request.concurrency)
            persistence_lock = asyncio.Lock()
            budget = _JudgeBudget(
                token_budget=request.token_budget,
                request_budget=request.request_budget,
            )

            async def run_task(task: _PreparedTask) -> _TaskOutcome:
                async with semaphore:
                    outcome = await self._judge_task(
                        task,
                        budget=budget,
                        timeout_seconds=request.attempt_timeout_seconds,
                    )
                async with persistence_lock:
                    self._persist_outcome(revision_id, outcome)
                    self._refresh_revision_progress(revision_id)
                return outcome

            async def run_citation_task(task: _PreparedCitationTask) -> None:
                async with semaphore:
                    outcome = await self._judge_citation_task(
                        task,
                        budget=budget,
                        timeout_seconds=request.attempt_timeout_seconds,
                    )
                async with persistence_lock:
                    self._persist_citation_support(revision_id, outcome)

            await asyncio.gather(
                *(run_task(task) for task in tasks),
                *(run_citation_task(task) for task in citation_tasks),
            )
            self._finish_revision_after_tasks(revision_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._finish_revision(
                revision_id,
                status="incomplete",
                error_code="judgment_runner_failure",
                error_message=str(error),
            )

    def _prepare_tasks(
        self,
        revision_id: str,
        run: RunView,
    ) -> tuple[list[_PreparedTask], list[_PreparedCitationTask]]:
        case_revision_ids = list(
            dict.fromkeys(attempt.case_revision_id for attempt in run.attempts)
        )
        case_views = {
            item.revision_id: item
            for item in self._repository.get_case_revisions(case_revision_ids)
        }
        tasks: list[_PreparedTask] = []
        citation_tasks: list[_PreparedCitationTask] = []
        for attempt in run.attempts:
            if not is_product_sample(attempt.outcome_class):
                continue
            case = case_views.get(attempt.case_revision_id)
            if case is None:
                continue
            built: BuiltJudgeInput | None = None
            build_error: JudgeInputError | None = None
            try:
                built = build_judge_input(
                    catalog=self._trace_catalog,
                    trace_id=attempt.trace_id,
                    trace_status=attempt.trace_status,
                    answer=attempt.answer,
                )
                self._persist_citation_check(
                    revision_id=revision_id,
                    attempt_id=attempt.attempt_id,
                    built=built,
                )
                cited_ids = set(built.checker_payload.cited_block_ids)
                if (
                    attempt.answer is not None
                    and cited_ids
                    and not built.checker_payload.missing_block_ids
                ):
                    cited_evidence = [
                        CitationEvidenceInput(
                            block_id=item.block_id,
                            doc_title=item.doc_title,
                            section_path=item.section_path,
                            text=item.text,
                        )
                        for item in built.all_evidence
                        if item.block_id in cited_ids
                    ]
                    _add_citation_task_or_pending(
                        self,
                        revision_id=revision_id,
                        attempt_id=attempt.attempt_id,
                        answer=attempt.answer,
                        citations=cited_evidence,
                        citation_tasks=citation_tasks,
                    )
            except JudgeInputError as error:
                build_error = error
                self._persist_pending_check(
                    revision_id=revision_id,
                    attempt_id=attempt.attempt_id,
                    code=error.code,
                )
            for criterion in case.criteria:
                task_built = built if criterion.evaluator == "checker" else None
                task_build_error = build_error if criterion.evaluator == "checker" else None
                tasks.append(
                    _PreparedTask(
                        attempt=attempt,
                        case=case,
                        criterion=criterion,
                        built=task_built,
                        build_error=task_build_error,
                    )
                )
        return tasks, citation_tasks

    async def _judge_task(
        self,
        task: _PreparedTask,
        *,
        budget: _JudgeBudget,
        timeout_seconds: int,
    ) -> _TaskOutcome:
        if task.build_error is not None:
            return _pending_judgment(
                task,
                task.build_error.code,
                reason=str(task.build_error),
            )
        if task.criterion.evaluator == "human":
            return _pending_judgment(
                task,
                "judge_manual_required",
                reason="criterion requires human judgment",
                executor="human",
                executor_ref="manual-review",
            )
        if task.criterion.evaluator == "checker":
            return _checker_judgment(task)
        if task.criterion.evaluator_ref != JUDGE_PROMPT_VERSION:
            return _pending_judgment(
                task,
                "judge_prompt_version_mismatch",
                reason=(
                    f"criterion requires {task.criterion.evaluator_ref}; "
                    f"current judge prompt is {JUDGE_PROMPT_VERSION}"
                ),
            )
        return await self._llm_judgment(
            task,
            budget=budget,
            timeout_seconds=timeout_seconds,
        )

    async def _llm_judgment(
        self,
        task: _PreparedTask,
        *,
        budget: _JudgeBudget,
        timeout_seconds: int,
    ) -> _TaskOutcome:
        messages = render_judge_messages(
            JudgeInputPacket(
                answer=task.attempt.answer or "",
                criterion=JudgeCriterionInput(
                    rule_type=task.criterion.rule_type,
                    rule_text=task.criterion.rule_text,
                ),
            )
        )
        input_json = _json(
            {
                "prompt_version": JUDGE_PROMPT_VERSION,
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
            }
        )
        reserved_tokens = 2 * (_estimate_tokens(input_json) + JUDGE_MAX_OUTPUT_TOKENS)
        if not budget.reserve(
            tokens=reserved_tokens,
            requests=2,
        ):
            return _pending_judgment(
                task,
                "judge_budget_exhausted",
                reason="judge budget cannot cover another criterion",
            )
        provider = self._chat_provider()
        invocations: list[_InvocationDraft] = []
        last_error: str | None = None
        parsed: JudgeOutput | None = None
        for attempt_number in range(1, 3):
            started_at = utc_now()
            try:
                result = await asyncio.wait_for(
                    provider.complete(
                        messages,
                        max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                invocations.append(
                    _InvocationDraft(
                        attempt_number=attempt_number,
                        status="timeout",
                        input_json=input_json,
                        raw_output=None,
                        error_code="judge_timeout",
                        input_tokens=0,
                        output_tokens=0,
                        request_id=None,
                        started_at=started_at,
                        finished_at=utc_now(),
                    )
                )
                last_error = "judge_upstream_unavailable"
                continue
            except ChatProviderError as error:
                invocations.append(
                    _InvocationDraft(
                        attempt_number=attempt_number,
                        status="provider_error",
                        input_json=input_json,
                        raw_output=None,
                        error_code=error.code,
                        input_tokens=0,
                        output_tokens=0,
                        request_id=None,
                        started_at=started_at,
                        finished_at=utc_now(),
                    )
                )
                last_error = "judge_upstream_unavailable"
                if error.retryable and attempt_number < 2:
                    continue
                break
            except Exception:
                invocations.append(
                    _InvocationDraft(
                        attempt_number=attempt_number,
                        status="provider_error",
                        input_json=input_json,
                        raw_output=None,
                        error_code="judge_provider_exception",
                        input_tokens=0,
                        output_tokens=0,
                        request_id=None,
                        started_at=started_at,
                        finished_at=utc_now(),
                    )
                )
                last_error = "judge_provider_exception"
                break
            finished_at = utc_now()
            invocations.append(
                _InvocationDraft(
                    attempt_number=attempt_number,
                    status="succeeded",
                    input_json=input_json,
                    raw_output=result.content,
                    error_code=None,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    request_id=result.request_id,
                    started_at=started_at,
                    finished_at=finished_at,
                )
            )
            try:
                parsed = _parse_judge_output(result, task.criterion)
            except ValueError as error:
                last_error = str(error)
                invocations[-1] = replace_invocation(
                    invocations[-1],
                    status="output_invalid",
                    error_code=last_error,
                )
                break
            break
        if parsed is None:
            return _pending_judgment(
                task,
                last_error or "judge_failed",
                reason="judge did not produce a valid criterion result",
                invocations=invocations,
            )
        budget.settle(
            reserved_tokens=reserved_tokens,
            actual_tokens=sum(
                invocation.input_tokens + invocation.output_tokens for invocation in invocations
            ),
            actual_requests=len(invocations),
        )
        return _TaskOutcome(
            attempt_id=task.attempt.attempt_id,
            criterion=task.criterion,
            executor="llm_judge",
            executor_ref=f"{JUDGE_PROMPT_VERSION}:{provider.profile.model_id}",
            verdict=parsed.verdict,
            normalized_result=_normalize_verdict(task.criterion.rule_type, parsed.verdict),
            reason=parsed.reason,
            evidence={},
            error_code=None,
            invocations=tuple(invocations),
        )

    async def _judge_citation_task(
        self,
        task: _PreparedCitationTask,
        *,
        budget: _JudgeBudget,
        timeout_seconds: int,
    ) -> _CitationOutcome:
        """执行一次最小输入的引用支持检查。"""

        messages = render_citation_messages(task.packet)
        input_json = _json(
            {
                "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
            }
        )
        reserved_tokens = 2 * (_estimate_tokens(input_json) + JUDGE_MAX_OUTPUT_TOKENS)
        if not budget.reserve(tokens=reserved_tokens, requests=2):
            return _CitationOutcome(
                attempt_id=task.attempt_id,
                result="pending",
                payload={
                    "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                    "input": task.packet.model_dump(mode="json"),
                    "error": "judge_budget_exhausted",
                },
                error_code="judge_budget_exhausted",
            )

        provider = self._chat_provider()
        invocations: list[_InvocationDraft] = []
        parsed: CitationSupportOutput | None = None
        last_error: str | None = None
        for attempt_number in range(1, 3):
            started_at = utc_now()
            try:
                result = await asyncio.wait_for(
                    provider.complete(
                        messages,
                        max_output_tokens=JUDGE_MAX_OUTPUT_TOKENS,
                    ),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                invocations.append(
                    _InvocationDraft(
                        attempt_number=attempt_number,
                        status="timeout",
                        input_json=input_json,
                        raw_output=None,
                        error_code="judge_timeout",
                        input_tokens=0,
                        output_tokens=0,
                        request_id=None,
                        started_at=started_at,
                        finished_at=utc_now(),
                    )
                )
                last_error = "judge_upstream_unavailable"
                continue
            except ChatProviderError as error:
                invocations.append(
                    _InvocationDraft(
                        attempt_number=attempt_number,
                        status="provider_error",
                        input_json=input_json,
                        raw_output=None,
                        error_code=error.code,
                        input_tokens=0,
                        output_tokens=0,
                        request_id=None,
                        started_at=started_at,
                        finished_at=utc_now(),
                    )
                )
                last_error = "judge_upstream_unavailable"
                if error.retryable and attempt_number < 2:
                    continue
                break
            except Exception:
                invocations.append(
                    _InvocationDraft(
                        attempt_number=attempt_number,
                        status="provider_error",
                        input_json=input_json,
                        raw_output=None,
                        error_code="judge_provider_exception",
                        input_tokens=0,
                        output_tokens=0,
                        request_id=None,
                        started_at=started_at,
                        finished_at=utc_now(),
                    )
                )
                last_error = "judge_provider_exception"
                break
            invocations.append(
                _InvocationDraft(
                    attempt_number=attempt_number,
                    status="succeeded",
                    input_json=input_json,
                    raw_output=result.content,
                    error_code=None,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    request_id=result.request_id,
                    started_at=started_at,
                    finished_at=utc_now(),
                )
            )
            try:
                parsed = _parse_citation_output(result, task.packet)
            except ValueError as error:
                last_error = str(error)
                invocations[-1] = replace_invocation(
                    invocations[-1],
                    status="output_invalid",
                    error_code=last_error,
                )
                break
            break

        budget.settle(
            reserved_tokens=reserved_tokens,
            actual_tokens=sum(
                invocation.input_tokens + invocation.output_tokens for invocation in invocations
            ),
            actual_requests=len(invocations),
        )
        invocation_payload = [
            {
                "attempt_number": invocation.attempt_number,
                "status": invocation.status,
                "error_code": invocation.error_code,
                "input_tokens": invocation.input_tokens,
                "output_tokens": invocation.output_tokens,
                "request_id": invocation.request_id,
                "raw_output": invocation.raw_output,
            }
            for invocation in invocations
        ]
        if parsed is None:
            return _CitationOutcome(
                attempt_id=task.attempt_id,
                result="pending",
                payload={
                    "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                    "prompt_hash": CITATION_SUPPORT_PROMPT_HASH,
                    "model_id": provider.profile.model_id,
                    "input": task.packet.model_dump(mode="json"),
                    "error": last_error or "citation_support_failed",
                    "invocations": invocation_payload,
                },
                error_code=last_error or "citation_support_failed",
            )

        verdicts = {item.verdict for item in parsed.citations}
        normalized: Literal["pass", "fail", "pending"] = (
            "fail" if "unsupported" in verdicts else "pending" if "unknown" in verdicts else "pass"
        )
        error_code = {
            "pass": None,
            "fail": "citation_not_supported",
            "pending": "citation_support_unknown",
        }[normalized]
        return _CitationOutcome(
            attempt_id=task.attempt_id,
            result=normalized,
            payload={
                "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                "prompt_hash": CITATION_SUPPORT_PROMPT_HASH,
                "model_id": provider.profile.model_id,
                "input": task.packet.model_dump(mode="json"),
                "citations": [citation.model_dump(mode="json") for citation in parsed.citations],
                "invocations": invocation_payload,
            },
            error_code=error_code,
        )

    def _persist_citation_check(
        self,
        *,
        revision_id: str,
        attempt_id: str,
        built: BuiltJudgeInput,
    ) -> None:
        payload = built.checker_payload
        result = "fail" if payload.missing_block_ids else "pass"
        with self._database.session() as session, session.begin():
            session.add(
                EvalCheckResultRow(
                    check_result_id=f"check_{uuid4().hex}",
                    evaluation_revision_id=revision_id,
                    attempt_id=attempt_id,
                    checker_id="citation_integrity",
                    checker_version=CHECKER_VERSIONS["citation_integrity"],
                    result=result,
                    payload_json=_json(payload.model_dump(mode="json")),
                    error_code=("citation_block_missing" if payload.missing_block_ids else None),
                )
            )

    def _persist_citation_support(
        self,
        revision_id: str,
        outcome: _CitationOutcome,
    ) -> None:
        """持久化引用支持检查及最小调用审计。"""

        with self._database.session() as session, session.begin():
            session.add(
                EvalCheckResultRow(
                    check_result_id=f"check_{uuid4().hex}",
                    evaluation_revision_id=revision_id,
                    attempt_id=outcome.attempt_id,
                    checker_id="citation_support",
                    checker_version=CHECKER_VERSIONS["citation_support"],
                    result=outcome.result,
                    payload_json=_json(outcome.payload),
                    error_code=outcome.error_code,
                )
            )

    def _persist_pending_check(
        self,
        *,
        revision_id: str,
        attempt_id: str,
        code: str,
    ) -> None:
        with self._database.session() as session, session.begin():
            session.add(
                EvalCheckResultRow(
                    check_result_id=f"check_{uuid4().hex}",
                    evaluation_revision_id=revision_id,
                    attempt_id=attempt_id,
                    checker_id="citation_integrity",
                    checker_version=CHECKER_VERSIONS["citation_integrity"],
                    result="pending",
                    payload_json="{}",
                    error_code=code,
                )
            )

    def _persist_outcome(
        self,
        revision_id: str,
        outcome: _TaskOutcome,
    ) -> None:
        """追加一个 Judgment 及其 invocation 历史。"""

        with self._database.session() as session, session.begin():
            judgment_id = f"judgment_{uuid4().hex}"
            session.add(
                EvalJudgmentRow(
                    judgment_id=judgment_id,
                    evaluation_revision_id=revision_id,
                    attempt_id=outcome.attempt_id,
                    criterion_revision_id=outcome.criterion.revision_id,
                    executor=outcome.executor,
                    executor_ref=outcome.executor_ref,
                    verdict=outcome.verdict,
                    normalized_result=outcome.normalized_result,
                    reason=outcome.reason,
                    evidence_json=_json(outcome.evidence),
                    error_code=outcome.error_code,
                )
            )
            session.flush()
            session.add(
                EvalJudgmentBindingRow(
                    binding_id=f"binding_{uuid4().hex}",
                    evaluation_revision_id=revision_id,
                    attempt_id=outcome.attempt_id,
                    criterion_revision_id=outcome.criterion.revision_id,
                    judgment_id=judgment_id,
                    bound_at=utc_now(),
                )
            )
            for invocation in outcome.invocations:
                session.add(
                    EvalJudgeInvocationRow(
                        invocation_id=f"invocation_{uuid4().hex}",
                        judgment_id=judgment_id,
                        attempt_number=invocation.attempt_number,
                        prompt_version=JUDGE_PROMPT_VERSION,
                        model_id=self._chat_provider().profile.model_id,
                        input_hash=hashlib.sha256(invocation.input_json.encode()).hexdigest(),
                        output_hash=(
                            None
                            if invocation.raw_output is None
                            else hashlib.sha256(invocation.raw_output.encode()).hexdigest()
                        ),
                        input_json=invocation.input_json,
                        raw_output=invocation.raw_output,
                        status=invocation.status,
                        error_code=invocation.error_code,
                        input_tokens=invocation.input_tokens,
                        output_tokens=invocation.output_tokens,
                        request_id=invocation.request_id,
                        started_at=invocation.started_at,
                        finished_at=invocation.finished_at,
                    )
                )

    def _refresh_revision_progress(self, revision_id: str) -> None:
        """按已落盘 Judgment 更新判定进度摘要。"""

        with self._database.session() as session, session.begin():
            rows = session.execute(
                select(
                    EvalJudgmentRow.normalized_result,
                    func.count(),
                )
                .where(EvalJudgmentRow.evaluation_revision_id == revision_id)
                .group_by(EvalJudgmentRow.normalized_result)
            ).all()
            counts = dict(rows)
            revision = session.get(EvalEvaluationRevisionRow, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            current = json.loads(revision.summary_json)
            current.update(
                {
                    "pass": int(counts.get("pass", 0)),
                    "fail": int(counts.get("fail", 0)),
                    "pending": int(counts.get("pending", 0)),
                }
            )
            revision.summary_json = _json(current)

    def _finish_revision_after_tasks(self, revision_id: str) -> None:
        """根据 pending 数量设置判定最终状态。"""

        with self._database.session() as session, session.begin():
            revision = session.get(EvalEvaluationRevisionRow, revision_id)
            if revision is None:
                raise KeyError(revision_id)
            summary = json.loads(revision.summary_json)
            pending_checks = session.scalar(
                select(func.count())
                .select_from(EvalCheckResultRow)
                .where(
                    EvalCheckResultRow.evaluation_revision_id == revision_id,
                    EvalCheckResultRow.result == "pending",
                )
            )
            revision.status = (
                "complete"
                if int(summary.get("pending", 0)) == 0 and not pending_checks
                else "incomplete"
            )
            revision.finished_at = utc_now()

    def _update_revision(self, revision_id: str, *, total: int) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(EvalEvaluationRevisionRow, revision_id)
            if row is None:
                raise KeyError(revision_id)
            row.summary_json = _json({"total": total, "pass": 0, "fail": 0, "pending": 0})

    def _finish_revision(
        self,
        revision_id: str,
        *,
        status: Literal["incomplete", "canceled"],
        error_code: str,
        error_message: str,
    ) -> None:
        with self._database.session() as session, session.begin():
            row = session.get(EvalEvaluationRevisionRow, revision_id)
            if row is None:
                return
            row.status = status
            row.error_code = error_code
            row.error_message = error_message
            row.finished_at = utc_now()

    def _recover_interrupted_revisions(self) -> None:
        """进程重启后把遗留 running 判定标为不完整。"""

        with self._database.session() as session, session.begin():
            rows = session.scalars(
                select(EvalEvaluationRevisionRow).where(
                    EvalEvaluationRevisionRow.status == "running"
                )
            ).all()
            for row in rows:
                row.status = "incomplete"
                row.error_code = "judgment_process_restarted"
                row.error_message = "judgment process restarted before completion"
                row.finished_at = utc_now()

    def _chat_provider(self) -> ChatProvider:
        api_key = self._credentials.get(CHAT_API_KEY_USERNAME)
        if not api_key:
            from app.ingestion.errors import IngestFailure

            raise IngestFailure(
                "chat_api_key_missing",
                "configure a chat API key before running the judge",
                409,
            )
        return (
            self._chat_provider_factory(api_key)
            if self._chat_provider_factory is not None
            else DeepSeekChatProvider(api_key)
        )


@dataclass(frozen=True, slots=True)
class _PreparedTask:
    """一个已解析现场、等待判定的 criterion 任务。"""

    attempt: AttemptView
    case: CaseRevisionView
    criterion: CriterionView
    built: BuiltJudgeInput | None
    build_error: JudgeInputError | None


@dataclass(frozen=True, slots=True)
class _PreparedCitationTask:
    """一次 attempt 的显式引用支持检查任务。"""

    attempt_id: str
    packet: CitationSupportInput


@dataclass(frozen=True, slots=True)
class _CitationOutcome:
    """尚未持久化的引用支持检查结果与调用审计。"""

    attempt_id: str
    result: Literal["pass", "fail", "pending"]
    payload: dict[str, Any]
    error_code: str | None


class _JudgeBudget:
    """保护 Judge provider 调用的 token／请求硬预算。"""

    def __init__(self, *, token_budget: int, request_budget: int) -> None:
        self._tokens = token_budget
        self._requests = request_budget

    def reserve(self, *, tokens: int, requests: int) -> bool:
        if self._tokens < tokens or self._requests < requests:
            return False
        self._tokens -= tokens
        self._requests -= requests
        return True

    def settle(
        self,
        *,
        reserved_tokens: int,
        actual_tokens: int,
        actual_requests: int,
    ) -> None:
        self._tokens += max(0, reserved_tokens - actual_tokens)
        self._requests += max(0, 2 - actual_requests)


def _add_citation_task_or_pending(
    service: EvaluationJudgmentService,
    *,
    revision_id: str,
    attempt_id: str,
    answer: str,
    citations: list[CitationEvidenceInput],
    citation_tasks: list[_PreparedCitationTask],
) -> None:
    """加入引用语义检查；超限时只把该共享检查记为 pending。"""

    evidence_chars = sum(len(item.text) for item in citations)
    if evidence_chars > MAX_JUDGE_EVIDENCE_CHARS:
        service._persist_citation_support(
            revision_id,
            _CitationOutcome(
                attempt_id=attempt_id,
                result="pending",
                payload={
                    "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                    "evidence_char_count": evidence_chars,
                    "error": "citation_support_input_too_large",
                },
                error_code="citation_support_input_too_large",
            ),
        )
        return
    citation_tasks.append(
        _PreparedCitationTask(
            attempt_id=attempt_id,
            packet=CitationSupportInput(answer=answer, citations=citations),
        )
    )


def _checker_judgment(task: _PreparedTask) -> _TaskOutcome:
    assert task.built is not None
    if task.criterion.evaluator_ref == "truncation_declared":
        satisfied = task.built.checker_payload.generation_state.answer_completeness == "partial"
        verdict = "satisfied" if satisfied else "unsatisfied"
        return _TaskOutcome(
            attempt_id=task.attempt.attempt_id,
            criterion=task.criterion,
            executor="checker",
            executor_ref="truncation_declared:1",
            verdict=verdict,
            normalized_result="pass" if satisfied else "fail",
            reason=(
                "answer_completeness is partial"
                if satisfied
                else "answer_completeness is not partial"
            ),
            evidence=task.built.checker_payload.model_dump(mode="json"),
            error_code=None,
            invocations=(),
        )
    return _pending_judgment(
        task,
        "checker_unavailable",
        reason=f"unknown checker: {task.criterion.evaluator_ref}",
    )


def _pending_judgment(
    task: _PreparedTask,
    error_code: str,
    *,
    reason: str,
    executor: str = "llm_judge",
    executor_ref: str = JUDGE_PROMPT_VERSION,
    invocations: Sequence[_InvocationDraft] = (),
) -> _TaskOutcome:
    return _TaskOutcome(
        attempt_id=task.attempt.attempt_id,
        criterion=task.criterion,
        executor=executor,
        executor_ref=executor_ref,
        verdict="unknown",
        normalized_result="pending",
        reason=reason,
        evidence={},
        error_code=error_code,
        invocations=tuple(invocations),
    )


def _parse_judge_output(
    result: ChatCompletionResult,
    criterion: CriterionView,
) -> JudgeOutput:
    try:
        parsed = JudgeOutput.model_validate_json(result.content)
    except ValidationError as error:
        raise ValueError("judge_output_invalid") from error
    allowed = (
        {"satisfied", "unsatisfied", "unknown"}
        if criterion.rule_type == "R"
        else {"violated", "not_violated", "unknown"}
    )
    if parsed.verdict not in allowed:
        raise ValueError("judge_verdict_invalid")
    return parsed


def _parse_citation_output(
    result: ChatCompletionResult,
    packet: CitationSupportInput,
) -> CitationSupportOutput:
    """校验引用支持结论、block 归属和 quote 原文。"""

    try:
        parsed = CitationSupportOutput.model_validate_json(result.content)
    except ValidationError as error:
        raise ValueError("citation_output_invalid") from error
    evidence_by_id = {item.block_id: item.text for item in packet.citations}
    result_ids = [item.block_id for item in parsed.citations]
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != set(evidence_by_id):
        raise ValueError("citation_result_block_set_invalid")
    for item in parsed.citations:
        for quote in item.evidence_quotes:
            if quote not in evidence_by_id[item.block_id]:
                raise ValueError("citation_evidence_quote_invalid")
    return parsed


def _normalize_verdict(rule_type: str, verdict: str) -> str:
    if verdict in {"satisfied", "not_violated"}:
        return "pass"
    if verdict in {"unsatisfied", "violated"}:
        return "fail"
    if verdict == "unknown":
        return "pending"
    raise ValueError(f"unsupported judge verdict: {rule_type}/{verdict}")


def _normalize_human_verdict(rule_type: str, verdict: str) -> str:
    """校验人工 verdict 必须与 R／X 方向一致。"""

    allowed = (
        {"satisfied", "unsatisfied", "unknown"}
        if rule_type == "R"
        else {"violated", "not_violated", "unknown"}
    )
    if verdict not in allowed:
        raise ValueError(f"unsupported judge verdict: {rule_type}/{verdict}")
    return _normalize_verdict(rule_type, verdict)


def replace_invocation(
    invocation: _InvocationDraft,
    *,
    status: str,
    error_code: str,
) -> _InvocationDraft:
    """替换 invocation 的最终状态字段。"""

    return _InvocationDraft(
        attempt_number=invocation.attempt_number,
        status=status,
        input_json=invocation.input_json,
        raw_output=invocation.raw_output,
        error_code=error_code,
        input_tokens=invocation.input_tokens,
        output_tokens=invocation.output_tokens,
        request_id=invocation.request_id,
        started_at=invocation.started_at,
        finished_at=invocation.finished_at,
    )


def _expect_status(
    value: str,
) -> Literal["running", "complete", "incomplete", "canceled"]:
    if value not in {"running", "complete", "incomplete", "canceled"}:
        raise ValueError(f"invalid evaluation revision status: {value}")
    return value  # type: ignore[return-value]


def _expect_check_result(value: str) -> Literal["pass", "fail", "pending"]:
    if value not in {"pass", "fail", "pending"}:
        raise ValueError(f"invalid checker result: {value}")
    return value  # type: ignore[return-value]


def _estimate_tokens(value: str) -> int:
    return max(1, len(value) // 3)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
