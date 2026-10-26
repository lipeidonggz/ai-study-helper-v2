from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select

from app.agent.citations import CitationAnnotation
from app.evaluation.aggregation import build_aggregates, build_result_matrix
from app.evaluation.citation_prompt import (
    CITATION_CLAIM_ROLES_PROMPT_HASH,
    CITATION_CLAIM_ROLES_PROMPT_VERSION,
    CITATION_EVIDENCE_ROLES_PROMPT_HASH,
    CITATION_EVIDENCE_ROLES_PROMPT_VERSION,
    CITATION_RELATION_PROMPT_HASH,
    CITATION_RELATION_PROMPT_VERSION,
    CITATION_SUPPORT_PROMPT_HASH,
    CITATION_SUPPORT_PROMPT_VERSION,
    CitationClaimRoleInput,
    CitationContextFragmentInput,
    CitationContextInput,
    CitationEvidenceInput,
    CitationEvidenceRoleInput,
    CitationOccurrenceInput,
    CitationRelationComparisonInput,
    CitationRoleAssertionInput,
    CitationSupportInput,
    render_citation_claim_role_messages,
    render_citation_evidence_role_messages,
    render_citation_messages,
    render_citation_relation_messages,
)
from app.evaluation.database import EvaluationDatabase
from app.evaluation.evidence import (
    MAX_CITATION_BATCH_INPUT_CHARS,
    MAX_CITATION_BATCH_OCCURRENCES,
    BuiltJudgeInput,
    JudgeCriterionInput,
    JudgeInputError,
    JudgeInputPacket,
    build_generation_judge_input,
    build_judge_input,
    cited_block_ids,
    split_citation_contexts,
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
    JudgeTokenUsageView,
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
    ChatMessage,
    ChatProvider,
    ChatProviderError,
    DeepSeekChatProvider,
)
from app.storage.credentials import CHAT_API_KEY_USERNAME, CredentialStore
from app.storage.models import utc_iso, utc_now
from app.tracing.catalog import TraceCatalog

CHECKER_VERSIONS = {
    "citation_integrity": "1",
    "citation_support": "11",
    "truncation_declared": "1",
}
JUDGE_MAX_OUTPUT_TOKENS = 2_048
CITATION_SUPPORT_MAX_OUTPUT_TOKENS = 4_096


def _build_judge_token_usage(
    judgments: Sequence[JudgmentView],
    checks: Sequence[CheckResultView],
) -> JudgeTokenUsageView:
    """汇总全部已记录判官调用；重试和引用批次各计一次，人工判定不计。"""

    criterion_invocations = [
        invocation for judgment in judgments for invocation in judgment.invocations
    ]
    input_tokens = sum(invocation.input_tokens for invocation in criterion_invocations)
    output_tokens = sum(invocation.output_tokens for invocation in criterion_invocations)
    invocation_count = len(criterion_invocations)

    for check in checks:
        if check.checker_id != "citation_support":
            continue
        batches = check.payload.get("batches", [])
        if not isinstance(batches, list):
            raise RuntimeError(f"citation judge batching is invalid: {check.check_result_id}")
        for batch in batches:
            if not isinstance(batch, dict):
                raise RuntimeError(f"citation judge batch is invalid: {check.check_result_id}")
            invocations = batch.get("invocations", [])
            if not isinstance(invocations, list):
                raise RuntimeError(
                    f"citation judge invocations are invalid: {check.check_result_id}"
                )
            for invocation in invocations:
                if not isinstance(invocation, dict):
                    raise RuntimeError(
                        f"citation judge invocation is invalid: {check.check_result_id}"
                    )
                input_tokens += _invocation_token_count(invocation, "input_tokens")
                output_tokens += _invocation_token_count(invocation, "output_tokens")
                invocation_count += 1

    return JudgeTokenUsageView(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        invocation_count=invocation_count,
    )


def _invocation_token_count(invocation: dict[str, Any], field: str) -> int:
    """读取 citation judge payload 中的非负 Token 数，损坏数据不得静默归零。"""

    value = invocation.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"citation judge invocation has invalid {field}")
    return value


class JudgeOutput(BaseModel):
    """LLM Judge 必须返回的严格结构化结论。"""

    model_config = ConfigDict(extra="forbid")

    verdict: str
    reason: str = Field(min_length=1)


class CitationRelationCheckOutput(BaseModel):
    """claim 与 evidence 各自角色，以及关系方向和目标框架的审计结果。"""

    model_config = ConfigDict(extra="forbid")

    relation_present: bool
    relation_type: (
        Literal[
            "benefit_cost",
            "cause_effect",
            "condition_result",
            "direction",
            "other",
        ]
        | None
    ) = None
    source_objective: str | None = None
    claim_objective: str | None = None
    evidence_roles: list[CitationRoleAssertionInput] = Field(default_factory=list)
    claim_roles: list[CitationRoleAssertionInput] = Field(default_factory=list)
    direction_preserved: bool | None = None
    explicit_conflict: bool | None = None

    @model_validator(mode="after")
    def validate_relation_fields(self) -> CitationRelationCheckOutput:
        """保证关系存在时角色字段完整，非关系时统一为空。"""

        if self.relation_present:
            if (
                self.relation_type is None
                or not self.evidence_roles
                or not self.claim_roles
                or self.explicit_conflict is None
            ):
                raise ValueError("citation_relation_fields_missing")
            return self
        if (
            self.relation_type is not None
            or self.source_objective is not None
            or self.claim_objective is not None
            or self.evidence_roles
            or self.claim_roles
            or self.direction_preserved is not None
            or self.explicit_conflict is not None
        ):
            raise ValueError("citation_relation_fields_unexpected")
        return self


class CitationClaimRolePacket(BaseModel):
    """关系判官第一阶段对一批 claim 的独立角色解析。"""

    model_config = ConfigDict(extra="forbid")

    claims: list[CitationClaimRoleInput]


class CitationEvidenceRolePacket(BaseModel):
    """关系判官对一批 evidence 的独立角色解析。"""

    model_config = ConfigDict(extra="forbid")

    evidence: list[CitationEvidenceRoleInput]


class CitationRelationReviewOutput(BaseModel):
    """最终比较器对单个 occurrence 的方向判断。"""

    model_config = ConfigDict(extra="forbid")

    occurrence_id: str
    direction_preserved: bool | None
    explicit_conflict: bool = False
    unmatched_claim_roles: list[str | CitationRoleAssertionInput] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class CitationRelationReviewPacket(BaseModel):
    """窄关系核验器的一次完整输出。"""

    model_config = ConfigDict(extra="forbid")

    relations: list[CitationRelationReviewOutput]


class CitationResultOutput(BaseModel):
    """引用判官对单个 occurrence 的结构化结论。"""

    model_config = ConfigDict(extra="forbid")

    occurrence_id: str
    verdict: Literal["supported", "unsupported", "unknown"]
    reason: str = Field(min_length=1)
    evidence_quotes: list[str]
    relation_check: CitationRelationCheckOutput | None = None


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
            judge_usage = _build_judge_token_usage(judgment_views, check_views)
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
            result_matrix = build_result_matrix(
                run=run,
                plan_cases=plan_cases,
                cases=case_views,
                attempt_aggregates=attempt_aggregates,
                case_aggregates=case_aggregates,
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
                result_matrix=result_matrix,
                judge_usage=judge_usage,
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
            generation_built: BuiltJudgeInput | None = None
            generation_error: JudgeInputError | None = None
            built: BuiltJudgeInput | None = None
            build_error: JudgeInputError | None = None
            try:
                generation_built = build_generation_judge_input(
                    catalog=self._trace_catalog,
                    trace_id=attempt.trace_id,
                    trace_status=attempt.trace_status,
                    answer=attempt.answer,
                )
            except JudgeInputError as error:
                generation_error = error
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
                    citation_task = _build_citation_task(
                        attempt_id=attempt.attempt_id,
                        answer=attempt.answer,
                        citations=cited_evidence,
                        annotations=built.citation_annotations,
                    )
                    if citation_task is not None:
                        citation_tasks.append(citation_task)
            except JudgeInputError as error:
                build_error = error
                self._persist_pending_check(
                    revision_id=revision_id,
                    attempt_id=attempt.attempt_id,
                    code=error.code,
                )
            for criterion in case.criteria:
                if criterion.evaluator == "checker":
                    if criterion.evaluator_ref == "truncation_declared":
                        task_built = generation_built
                        task_build_error = generation_error
                    else:
                        task_built = built
                        task_build_error = build_error
                else:
                    task_built = None
                    task_build_error = None
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
            reserved_requests=2,
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
    ) -> _CitationAttemptOutcome:
        """顺序执行一个 attempt 的全部引用支持批次并合并结果。"""

        outcomes = [
            await self._judge_citation_batch(
                task,
                batch_index=batch_index,
                packet=packet,
                budget=budget,
                timeout_seconds=timeout_seconds,
            )
            for batch_index, packet in enumerate(task.batches, start=1)
        ]
        return _merge_citation_outcomes(task, outcomes)

    async def _judge_citation_batch(
        self,
        task: _PreparedCitationTask,
        *,
        batch_index: int,
        packet: CitationSupportInput,
        budget: _JudgeBudget,
        timeout_seconds: int,
    ) -> _CitationBatchOutcome:
        """执行一个有界引用支持批次。"""

        messages = render_citation_messages(packet)
        reserved_input_json = _json(
            {
                "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
            }
        )
        reserved_tokens = 5 * (
            _estimate_tokens(reserved_input_json) + CITATION_SUPPORT_MAX_OUTPUT_TOKENS
        )
        if not budget.reserve(tokens=reserved_tokens, requests=5):
            return _CitationBatchOutcome(
                attempt_id=task.attempt_id,
                batch_index=batch_index,
                expected_occurrence_ids=tuple(item.occurrence_id for item in packet.occurrences),
                result="pending",
                citations=(),
                payload={
                    "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                    "input": packet.model_dump(mode="json"),
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
            input_json = _json(
                {
                    "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                    "messages": [
                        {"role": message.role, "content": message.content} for message in messages
                    ],
                }
            )
            try:
                result = await asyncio.wait_for(
                    provider.complete(
                        messages,
                        max_output_tokens=CITATION_SUPPORT_MAX_OUTPUT_TOKENS,
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
                parsed = _parse_citation_output(result, packet)
            except ValueError as error:
                last_error = str(error)
                invocations[-1] = replace_invocation(
                    invocations[-1],
                    status="output_invalid",
                    error_code=last_error,
                )
                if attempt_number < 2:
                    messages = tuple(messages) + (
                        ChatMessage(role="assistant", content=result.content),
                        ChatMessage(
                            role="user",
                            content=(
                                "上一次输出未通过结构化校验："
                                f"{last_error}。请只修正 JSON 输出，保持 occurrence_id 集合不变。"
                            ),
                        ),
                    )
                    continue
                break
            relation_packet = _supported_relation_packet(packet, parsed)
            if relation_packet is not None:
                claim_role_messages = render_citation_claim_role_messages(relation_packet)
                claim_role_input_json = _json(
                    {
                        "prompt_version": CITATION_CLAIM_ROLES_PROMPT_VERSION,
                        "prompt_hash": CITATION_CLAIM_ROLES_PROMPT_HASH,
                        "messages": [
                            {"role": message.role, "content": message.content}
                            for message in claim_role_messages
                        ],
                    }
                )
                claim_role_started_at = utc_now()
                try:
                    claim_role_result = await asyncio.wait_for(
                        provider.complete(
                            claim_role_messages,
                            max_output_tokens=CITATION_SUPPORT_MAX_OUTPUT_TOKENS,
                        ),
                        timeout=timeout_seconds,
                    )
                except TimeoutError:
                    invocations.append(
                        _InvocationDraft(
                            attempt_number=attempt_number + 1,
                            status="timeout",
                            input_json=claim_role_input_json,
                            raw_output=None,
                            error_code="judge_timeout",
                            input_tokens=0,
                            output_tokens=0,
                            request_id=None,
                            started_at=claim_role_started_at,
                            finished_at=utc_now(),
                        )
                    )
                    parsed = None
                    last_error = "citation_relation_review_failed"
                    break
                except ChatProviderError as error:
                    invocations.append(
                        _InvocationDraft(
                            attempt_number=attempt_number + 1,
                            status="provider_error",
                            input_json=claim_role_input_json,
                            raw_output=None,
                            error_code=error.code,
                            input_tokens=0,
                            output_tokens=0,
                            request_id=None,
                            started_at=claim_role_started_at,
                            finished_at=utc_now(),
                        )
                    )
                    parsed = None
                    last_error = "citation_relation_review_failed"
                    break
                invocations.append(
                    _InvocationDraft(
                        attempt_number=attempt_number + 1,
                        status="succeeded",
                        input_json=claim_role_input_json,
                        raw_output=claim_role_result.content,
                        error_code=None,
                        input_tokens=claim_role_result.input_tokens,
                        output_tokens=claim_role_result.output_tokens,
                        request_id=claim_role_result.request_id,
                        started_at=claim_role_started_at,
                        finished_at=utc_now(),
                    )
                )
                try:
                    claim_roles = _parse_claim_roles(
                        claim_role_result,
                        relation_packet,
                    )
                except ValueError as error:
                    last_error = str(error)
                    invocations[-1] = replace_invocation(
                        invocations[-1],
                        status="output_invalid",
                        error_code=last_error,
                    )
                    parsed = None
                    break
                related_packet = _related_relation_packet(relation_packet, claim_roles)
                evidence_roles: dict[str, CitationEvidenceRoleInput] = {}
                relation_checks: dict[str, CitationRelationReviewOutput] = {}
                if related_packet is not None:
                    evidence_messages = render_citation_evidence_role_messages(related_packet)
                    evidence_input_json = _json(
                        {
                            "prompt_version": CITATION_EVIDENCE_ROLES_PROMPT_VERSION,
                            "prompt_hash": CITATION_EVIDENCE_ROLES_PROMPT_HASH,
                            "messages": [
                                {"role": message.role, "content": message.content}
                                for message in evidence_messages
                            ],
                        }
                    )
                    evidence_started_at = utc_now()
                    try:
                        evidence_result = await asyncio.wait_for(
                            provider.complete(
                                evidence_messages,
                                max_output_tokens=CITATION_SUPPORT_MAX_OUTPUT_TOKENS,
                            ),
                            timeout=timeout_seconds,
                        )
                    except TimeoutError:
                        invocations.append(
                            _InvocationDraft(
                                attempt_number=attempt_number + 2,
                                status="timeout",
                                input_json=evidence_input_json,
                                raw_output=None,
                                error_code="judge_timeout",
                                input_tokens=0,
                                output_tokens=0,
                                request_id=None,
                                started_at=evidence_started_at,
                                finished_at=utc_now(),
                            )
                        )
                        parsed = None
                        last_error = "citation_relation_review_failed"
                        break
                    except ChatProviderError as error:
                        invocations.append(
                            _InvocationDraft(
                                attempt_number=attempt_number + 2,
                                status="provider_error",
                                input_json=evidence_input_json,
                                raw_output=None,
                                error_code=error.code,
                                input_tokens=0,
                                output_tokens=0,
                                request_id=None,
                                started_at=evidence_started_at,
                                finished_at=utc_now(),
                            )
                        )
                        parsed = None
                        last_error = "citation_relation_review_failed"
                        break
                    invocations.append(
                        _InvocationDraft(
                            attempt_number=attempt_number + 2,
                            status="succeeded",
                            input_json=evidence_input_json,
                            raw_output=evidence_result.content,
                            error_code=None,
                            input_tokens=evidence_result.input_tokens,
                            output_tokens=evidence_result.output_tokens,
                            request_id=evidence_result.request_id,
                            started_at=evidence_started_at,
                            finished_at=utc_now(),
                        )
                    )
                    try:
                        evidence_roles = _parse_evidence_roles(
                            evidence_result,
                            related_packet,
                        )
                    except ValueError as error:
                        last_error = str(error)
                        invocations[-1] = replace_invocation(
                            invocations[-1],
                            status="output_invalid",
                            error_code=last_error,
                        )
                        parsed = None
                        break
                    comparison_packet = _relation_comparison_packet(
                        related_packet,
                        claim_roles,
                        evidence_roles,
                    )
                    relation_messages = render_citation_relation_messages(comparison_packet)
                    relation_input_json = _json(
                        {
                            "prompt_version": CITATION_RELATION_PROMPT_VERSION,
                            "prompt_hash": CITATION_RELATION_PROMPT_HASH,
                            "messages": [
                                {"role": message.role, "content": message.content}
                                for message in relation_messages
                            ],
                        }
                    )
                    relation_started_at = utc_now()
                    try:
                        relation_result = await asyncio.wait_for(
                            provider.complete(
                                relation_messages,
                                max_output_tokens=CITATION_SUPPORT_MAX_OUTPUT_TOKENS,
                            ),
                            timeout=timeout_seconds,
                        )
                    except TimeoutError:
                        invocations.append(
                            _InvocationDraft(
                                attempt_number=attempt_number + 3,
                                status="timeout",
                                input_json=relation_input_json,
                                raw_output=None,
                                error_code="judge_timeout",
                                input_tokens=0,
                                output_tokens=0,
                                request_id=None,
                                started_at=relation_started_at,
                                finished_at=utc_now(),
                            )
                        )
                        parsed = None
                        last_error = "citation_relation_review_failed"
                        break
                    except ChatProviderError as error:
                        invocations.append(
                            _InvocationDraft(
                                attempt_number=attempt_number + 3,
                                status="provider_error",
                                input_json=relation_input_json,
                                raw_output=None,
                                error_code=error.code,
                                input_tokens=0,
                                output_tokens=0,
                                request_id=None,
                                started_at=relation_started_at,
                                finished_at=utc_now(),
                            )
                        )
                        parsed = None
                        last_error = "citation_relation_review_failed"
                        break
                    invocations.append(
                        _InvocationDraft(
                            attempt_number=attempt_number + 3,
                            status="succeeded",
                            input_json=relation_input_json,
                            raw_output=relation_result.content,
                            error_code=None,
                            input_tokens=relation_result.input_tokens,
                            output_tokens=relation_result.output_tokens,
                            request_id=relation_result.request_id,
                            started_at=relation_started_at,
                            finished_at=utc_now(),
                        )
                    )
                    try:
                        relation_checks = _parse_relation_review(
                            relation_result,
                            comparison_packet,
                        )
                    except ValueError as error:
                        last_error = str(error)
                        invocations[-1] = replace_invocation(
                            invocations[-1],
                            status="output_invalid",
                            error_code=last_error,
                        )
                        parsed = None
                        break
                parsed = _apply_relation_review(
                    parsed,
                    claim_roles,
                    evidence_roles,
                    relation_checks,
                )
            break

        budget.settle(
            reserved_tokens=reserved_tokens,
            actual_tokens=sum(
                invocation.input_tokens + invocation.output_tokens for invocation in invocations
            ),
            reserved_requests=5,
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
            return _CitationBatchOutcome(
                attempt_id=task.attempt_id,
                batch_index=batch_index,
                expected_occurrence_ids=tuple(item.occurrence_id for item in packet.occurrences),
                result="pending",
                citations=(),
                payload={
                    "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                    "prompt_hash": CITATION_SUPPORT_PROMPT_HASH,
                    "model_id": provider.profile.model_id,
                    "input": packet.model_dump(mode="json"),
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
        return _CitationBatchOutcome(
            attempt_id=task.attempt_id,
            batch_index=batch_index,
            expected_occurrence_ids=tuple(item.occurrence_id for item in packet.occurrences),
            result=normalized,
            citations=tuple(parsed.citations),
            payload={
                "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
                "prompt_hash": CITATION_SUPPORT_PROMPT_HASH,
                "model_id": provider.profile.model_id,
                "input": packet.model_dump(mode="json"),
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
        outcome: _CitationAttemptOutcome,
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
    """一个 attempt 的全部有界引用支持批次。"""

    attempt_id: str
    batches: tuple[CitationSupportInput, ...]
    occurrences: tuple[CitationOccurrenceInput, ...]
    preflight_failures: tuple[_CitationPreflightFailure, ...]
    context_count: int
    evidence_text_by_id: dict[str, str]
    context_text_by_id: dict[str, str]


@dataclass(frozen=True, slots=True)
class _CitationPreflightFailure:
    """无法安全送入模型、只能显式 pending 的单 occurrence。"""

    occurrence: CitationOccurrenceInput
    input_chars: int
    error_code: str


@dataclass(frozen=True, slots=True)
class _CitationBatchOutcome:
    """一个引用支持批次的结果与调用审计。"""

    attempt_id: str
    batch_index: int
    expected_occurrence_ids: tuple[str, ...]
    result: Literal["pass", "fail", "pending"]
    citations: tuple[CitationResultOutput, ...]
    payload: dict[str, Any]
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _CitationAttemptOutcome:
    """一个 attempt 的全部批次合并后的最终引用支持结果。"""

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
        reserved_requests: int,
        actual_requests: int,
    ) -> None:
        self._tokens += max(0, reserved_tokens - actual_tokens)
        self._requests += max(0, reserved_requests - actual_requests)


def _build_citation_task(
    *,
    attempt_id: str,
    answer: str,
    citations: list[CitationEvidenceInput],
    annotations: list[CitationAnnotation],
) -> _PreparedCitationTask | None:
    """按答案语义上下文构建有界引用检查批次。"""

    evidence_by_id = {item.block_id: item for item in citations}
    occurrences: list[CitationOccurrenceInput] = []
    preflight_failures: list[_CitationPreflightFailure] = []
    if annotations:
        contexts = [
            CitationContextInput(
                context_id=annotation.annotation_id,
                kind="claim",
                text=annotation.text,
                focus_text=annotation.text,
                context_fragments=[
                    CitationContextFragmentInput(
                        fragment_id=fragment.fragment_id,
                        kind=fragment.kind,
                        text=fragment.text,
                    )
                    for fragment in annotation.context_fragments
                ],
            )
            for annotation in annotations
        ]
        for annotation in annotations:
            block_ids = [
                block_id for block_id in annotation.block_ids if block_id in evidence_by_id
            ]
            if not block_ids:
                continue
            occurrences.append(
                CitationOccurrenceInput(
                    occurrence_id=f"citation_{len(occurrences) + 1:04d}",
                    block_ids=block_ids,
                    context_id=annotation.annotation_id,
                )
            )
    else:
        contexts = split_citation_contexts(answer)
        for context in contexts:
            block_ids = [
                block_id for block_id in cited_block_ids(context.text) if block_id in evidence_by_id
            ]
            if not block_ids:
                continue
            occurrences.append(
                CitationOccurrenceInput(
                    occurrence_id=f"citation_{len(occurrences) + 1:04d}",
                    block_ids=block_ids,
                    context_id=context.context_id,
                )
            )
        covered_block_ids = {
            block_id for occurrence in occurrences for block_id in occurrence.block_ids
        }
        for block_id in cited_block_ids(answer):
            if block_id in covered_block_ids:
                continue
            occurrence = CitationOccurrenceInput(
                occurrence_id=f"citation_{len(occurrences) + 1:04d}",
                block_ids=[block_id],
                context_id="context_missing",
            )
            occurrences.append(occurrence)
            preflight_failures.append(
                _CitationPreflightFailure(
                    occurrence=occurrence,
                    input_chars=0,
                    error_code="citation_support_context_missing",
                )
            )
    if not occurrences:
        return None

    context_by_id = {item.context_id: item for item in contexts}
    batches: list[CitationSupportInput] = []
    current: list[CitationOccurrenceInput] = []
    preflight_occurrence_ids = {item.occurrence.occurrence_id for item in preflight_failures}
    for occurrence in occurrences:
        if occurrence.occurrence_id in preflight_occurrence_ids:
            continue
        candidate = [*current, occurrence]
        candidate_packet = _citation_packet(
            candidate,
            context_by_id=context_by_id,
            evidence_by_id=evidence_by_id,
        )
        candidate_chars = len(_json(candidate_packet.model_dump(mode="json")))
        exceeds_occurrences = len(candidate) > MAX_CITATION_BATCH_OCCURRENCES
        exceeds_chars = candidate_chars > MAX_CITATION_BATCH_INPUT_CHARS
        if current and (exceeds_occurrences or exceeds_chars):
            batches.append(
                _citation_packet(
                    current,
                    context_by_id=context_by_id,
                    evidence_by_id=evidence_by_id,
                )
            )
            current = [occurrence]
            single_packet = _citation_packet(
                current,
                context_by_id=context_by_id,
                evidence_by_id=evidence_by_id,
            )
            if len(_json(single_packet.model_dump(mode="json"))) > (MAX_CITATION_BATCH_INPUT_CHARS):
                preflight_failures.append(
                    _CitationPreflightFailure(
                        occurrence=occurrence,
                        input_chars=len(_json(single_packet.model_dump(mode="json"))),
                        error_code="citation_support_unit_too_large",
                    )
                )
                current = []
            continue
        if not current and (exceeds_occurrences or exceeds_chars):
            preflight_failures.append(
                _CitationPreflightFailure(
                    occurrence=occurrence,
                    input_chars=candidate_chars,
                    error_code="citation_support_unit_too_large",
                )
            )
            continue
        current = candidate
    if current:
        batches.append(
            _citation_packet(
                current,
                context_by_id=context_by_id,
                evidence_by_id=evidence_by_id,
            )
        )
    return _PreparedCitationTask(
        attempt_id=attempt_id,
        batches=tuple(batches),
        occurrences=tuple(occurrences),
        preflight_failures=tuple(preflight_failures),
        context_count=len(contexts),
        evidence_text_by_id={
            block_id: citation.text for block_id, citation in evidence_by_id.items()
        },
        context_text_by_id={context.context_id: context.text for context in contexts},
    )


def _citation_packet(
    occurrences: list[CitationOccurrenceInput],
    *,
    context_by_id: dict[str, CitationContextInput],
    evidence_by_id: dict[str, CitationEvidenceInput],
) -> CitationSupportInput:
    context_ids = list(dict.fromkeys(item.context_id for item in occurrences))
    block_ids = list(
        dict.fromkeys(block_id for occurrence in occurrences for block_id in occurrence.block_ids)
    )
    return CitationSupportInput(
        contexts=[context_by_id[context_id] for context_id in context_ids],
        citations=[evidence_by_id[block_id] for block_id in block_ids],
        occurrences=list(occurrences),
    )


def _merge_citation_outcomes(
    task: _PreparedCitationTask,
    batches: list[_CitationBatchOutcome],
) -> _CitationAttemptOutcome:
    """把引用组批次结果按 fail > pending > pass 合并，并投影到 block 视图。"""

    occurrence_results: dict[str, CitationResultOutput] = {}
    occurrence_errors: dict[str, str] = {}
    for batch in batches:
        for citation in batch.citations:
            occurrence_results[citation.occurrence_id] = citation
        if batch.result != "pass":
            for occurrence_id in batch.expected_occurrence_ids:
                occurrence_errors[occurrence_id] = batch.error_code or "citation_support_failed"
    for failure in task.preflight_failures:
        occurrence_errors[failure.occurrence.occurrence_id] = failure.error_code

    claim_results: list[dict[str, Any]] = []
    block_verdicts: dict[str, list[str]] = {}
    block_reasons: dict[str, list[str]] = {}
    block_quotes: dict[str, list[str]] = {}
    all_verdicts: list[str] = []
    for occurrence in task.occurrences:
        verdict_result = occurrence_results.get(occurrence.occurrence_id)
        verdict = verdict_result.verdict if verdict_result is not None else "unknown"
        reason = (
            verdict_result.reason
            if verdict_result is not None
            else occurrence_errors.get(
                occurrence.occurrence_id,
                "citation_support_occurrence_missing",
            )
        )
        all_verdicts.append(verdict)
        quotes = [] if verdict_result is None else list(verdict_result.evidence_quotes)
        relation_check = (
            None
            if verdict_result is None or verdict_result.relation_check is None
            else verdict_result.relation_check.model_dump(mode="json")
        )
        claim_results.append(
            {
                "occurrence_id": occurrence.occurrence_id,
                "context_id": occurrence.context_id,
                "context_text": task.context_text_by_id.get(occurrence.context_id, ""),
                "block_ids": occurrence.block_ids,
                "verdict": verdict,
                "reason": reason,
                "evidence_quotes": quotes,
                "relation_check": relation_check,
            }
        )
        for block_id in occurrence.block_ids:
            block_verdicts.setdefault(block_id, []).append(verdict)
            block_reasons.setdefault(block_id, []).append(reason)
            block_text = task.evidence_text_by_id.get(block_id, "")
            block_quote_values = block_quotes.setdefault(block_id, [])
            for quote in quotes:
                if quote in block_text and quote not in block_quote_values:
                    block_quote_values.append(quote)

    merged_citations: list[dict[str, Any]] = []
    for block_id, verdicts in block_verdicts.items():
        merged_verdict = (
            "unsupported"
            if "unsupported" in verdicts
            else "unknown"
            if "unknown" in verdicts
            else "supported"
        )
        merged_citations.append(
            {
                "block_id": block_id,
                "verdict": merged_verdict,
                "reason": "；".join(dict.fromkeys(block_reasons[block_id])),
                "evidence_quotes": block_quotes.get(block_id, []),
            }
        )

    preflight_error_codes = [item.error_code for item in task.preflight_failures]
    batch_error_codes = [item.error_code for item in batches if item.error_code is not None]
    if "unsupported" in all_verdicts:
        result: Literal["pass", "fail", "pending"] = "fail"
        error_code = "citation_not_supported"
    elif "unknown" in all_verdicts or batch_error_codes or preflight_error_codes:
        result = "pending"
        error_code = (
            preflight_error_codes[0]
            if preflight_error_codes
            else batch_error_codes[0]
            if batch_error_codes
            else "citation_support_unknown"
        )
    else:
        result = "pass"
        error_code = None

    batch_payloads: list[dict[str, Any]] = []
    for batch in batches:
        batch_payload = dict(batch.payload)
        batch_payload.update(
            {
                "batch_index": batch.batch_index,
                "result": batch.result,
                "occurrence_ids": list(batch.expected_occurrence_ids),
            }
        )
        batch_payloads.append(batch_payload)
    model_ids = [
        value
        for value in dict.fromkeys(
            item.payload.get("model_id") for item in batches if item.payload.get("model_id")
        )
        if isinstance(value, str)
    ]
    return _CitationAttemptOutcome(
        attempt_id=task.attempt_id,
        result=result,
        payload={
            "prompt_version": CITATION_SUPPORT_PROMPT_VERSION,
            "prompt_hash": CITATION_SUPPORT_PROMPT_HASH,
            "model_ids": model_ids,
            "batching": {
                "strategy": "citation-context-group-v1",
                "context_count": task.context_count,
                "occurrence_count": len(task.occurrences),
                "citation_relation_count": sum(
                    len(occurrence.block_ids) for occurrence in task.occurrences
                ),
                "block_count": len(block_verdicts),
                "batch_count": len(batches),
                "preflight_failure_count": len(task.preflight_failures),
                "max_occurrences_per_batch": MAX_CITATION_BATCH_OCCURRENCES,
                "max_input_chars_per_batch": MAX_CITATION_BATCH_INPUT_CHARS,
            },
            "claim_results": claim_results,
            "citations": merged_citations,
            "preflight_failures": [
                {
                    "occurrence_id": item.occurrence.occurrence_id,
                    "block_ids": item.occurrence.block_ids,
                    "context_id": item.occurrence.context_id,
                    "input_chars": item.input_chars,
                    "error": item.error_code,
                }
                for item in task.preflight_failures
            ],
            "batches": batch_payloads,
        },
        error_code=error_code,
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
    """校验引用支持结论、occurrence 覆盖和 quote 原文。"""

    try:
        parsed = CitationSupportOutput.model_validate_json(result.content)
    except ValidationError as error:
        raise ValueError("citation_output_invalid") from error
    evidence_by_id = {item.block_id: item.text for item in packet.citations}
    occurrence_by_id = {item.occurrence_id: item for item in packet.occurrences}
    result_ids = [item.occurrence_id for item in parsed.citations]
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != set(occurrence_by_id):
        raise ValueError("citation_occurrence_set_invalid")
    for item in parsed.citations:
        occurrence = occurrence_by_id[item.occurrence_id]
        for quote in item.evidence_quotes:
            if not any(quote in evidence_by_id[block_id] for block_id in occurrence.block_ids):
                raise ValueError("citation_evidence_quote_invalid")
    return parsed


def _supported_relation_packet(
    packet: CitationSupportInput,
    parsed: CitationSupportOutput,
) -> CitationSupportInput | None:
    """提取主判官判为 supported、需要独立关系核验的 occurrence。"""

    supported_ids = {item.occurrence_id for item in parsed.citations if item.verdict == "supported"}
    context_by_id = {item.context_id: item for item in packet.contexts}
    occurrence_by_id = {item.occurrence_id: item for item in packet.occurrences}
    supported_ids = {
        occurrence_id
        for occurrence_id in supported_ids
        if context_by_id[occurrence_by_id[occurrence_id].context_id].focus_text is not None
    }
    if not supported_ids:
        return None
    supported_occurrences = [
        item for item in packet.occurrences if item.occurrence_id in supported_ids
    ]
    context_ids = list(dict.fromkeys(item.context_id for item in supported_occurrences))
    block_ids = list(
        dict.fromkeys(block_id for item in supported_occurrences for block_id in item.block_ids)
    )
    return CitationSupportInput(
        contexts=[item for item in packet.contexts if item.context_id in context_ids],
        citations=[item for item in packet.citations if item.block_id in block_ids],
        occurrences=supported_occurrences,
    )


def _parse_claim_roles(
    result: ChatCompletionResult,
    packet: CitationSupportInput,
) -> dict[str, CitationClaimRoleInput]:
    """严格校验不读取 evidence 的 claim 角色解析结果。"""

    try:
        parsed = CitationClaimRolePacket.model_validate_json(result.content)
    except ValidationError as error:
        raise ValueError("citation_claim_roles_output_invalid") from error
    result_ids = [item.occurrence_id for item in parsed.claims]
    expected_ids = {item.occurrence_id for item in packet.occurrences}
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != expected_ids:
        raise ValueError("citation_claim_roles_occurrence_set_invalid")
    return {item.occurrence_id: _normalize_claim_role(item) for item in parsed.claims}


def _normalize_claim_role(item: CitationClaimRoleInput) -> CitationClaimRoleInput:
    """移除连接词伪角色，并拒绝没有对应角色对的低信息关系。"""

    roles = [
        assertion
        for assertion in item.claim_roles
        if assertion.fact.strip().casefold()
        not in {"因此", "所以", "因而", "于是", "therefore", "thus", "so"}
    ]
    if not item.relation_present:
        return item
    required_roles = {
        "benefit_cost": {"benefit", "cost"},
        "cause_effect": {"cause", "effect"},
        "condition_result": {"condition", "result"},
        "direction": {"direction"},
        "other": {
            "benefit",
            "cost",
            "cause",
            "effect",
            "condition",
            "result",
            "direction",
        },
    }[item.relation_type or "other"]
    present_roles = {assertion.role for assertion in roles}
    if not roles or not present_roles.intersection(required_roles):
        return item.model_copy(
            update={
                "relation_present": False,
                "relation_type": None,
                "claim_objective": None,
                "claim_roles": [],
            }
        )
    if item.relation_type == "cause_effect" and not {"cause", "effect"}.issubset(present_roles):
        return item.model_copy(
            update={
                "relation_present": False,
                "relation_type": None,
                "claim_objective": None,
                "claim_roles": [],
            }
        )
    if item.relation_type == "condition_result" and not {
        "condition",
        "result",
    }.issubset(present_roles):
        return item.model_copy(
            update={
                "relation_present": False,
                "relation_type": None,
                "claim_objective": None,
                "claim_roles": [],
            }
        )
    return item.model_copy(update={"claim_roles": roles})


def _related_relation_packet(
    packet: CitationSupportInput,
    claim_roles: dict[str, CitationClaimRoleInput],
) -> CitationSupportInput | None:
    """提取明确存在关系、需要继续解析 evidence 角色的 occurrence。"""

    related_ids = {
        occurrence_id for occurrence_id, claim in claim_roles.items() if claim.relation_present
    }
    if not related_ids:
        return None
    occurrences = [item for item in packet.occurrences if item.occurrence_id in related_ids]
    block_ids = list(
        dict.fromkeys(block_id for occurrence in occurrences for block_id in occurrence.block_ids)
    )
    context_ids = list(dict.fromkeys(item.context_id for item in occurrences))
    return CitationSupportInput(
        contexts=[item for item in packet.contexts if item.context_id in context_ids],
        citations=[item for item in packet.citations if item.block_id in block_ids],
        occurrences=occurrences,
    )


def _parse_evidence_roles(
    result: ChatCompletionResult,
    packet: CitationSupportInput,
) -> dict[str, CitationEvidenceRoleInput]:
    """严格校验不读取 claim 的 evidence 角色解析结果。"""

    try:
        parsed = CitationEvidenceRolePacket.model_validate_json(result.content)
    except ValidationError as error:
        raise ValueError("citation_evidence_roles_output_invalid") from error
    result_ids = [item.occurrence_id for item in parsed.evidence]
    expected_ids = {item.occurrence_id for item in packet.occurrences}
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != expected_ids:
        raise ValueError("citation_evidence_roles_occurrence_set_invalid")
    return {item.occurrence_id: item for item in parsed.evidence}


def _relation_comparison_packet(
    packet: CitationSupportInput,
    claim_roles: dict[str, CitationClaimRoleInput],
    evidence_roles: dict[str, CitationEvidenceRoleInput],
) -> CitationRelationComparisonInput:
    """组合两份独立角色结果，供最终比较器使用。"""

    occurrences = [
        item for item in packet.occurrences if claim_roles[item.occurrence_id].relation_present
    ]
    return CitationRelationComparisonInput(
        claims=[claim_roles[item.occurrence_id] for item in occurrences],
        evidence=[evidence_roles[item.occurrence_id] for item in occurrences],
        occurrences=occurrences,
    )


def _parse_relation_review(
    result: ChatCompletionResult,
    packet: CitationRelationComparisonInput,
) -> dict[str, CitationRelationReviewOutput]:
    """严格校验 claim 角色与 evidence 角色的比较结果。"""

    try:
        parsed = CitationRelationReviewPacket.model_validate_json(result.content)
    except ValidationError as error:
        raise ValueError("citation_relation_output_invalid") from error
    result_ids = [item.occurrence_id for item in parsed.relations]
    expected_ids = {item.occurrence_id for item in packet.claims}
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != expected_ids:
        raise ValueError("citation_relation_occurrence_set_invalid")
    normalized: dict[str, CitationRelationReviewOutput] = {}
    for item in parsed.relations:
        if item.direction_preserved is False and not item.explicit_conflict:
            item = item.model_copy(
                update={
                    "direction_preserved": None,
                    "unmatched_claim_roles": [
                        *item.unmatched_claim_roles,
                        "判官未给出明确冲突依据",
                    ],
                }
            )
        elif item.explicit_conflict and item.direction_preserved is not False:
            item = item.model_copy(
                update={
                    "direction_preserved": None,
                    "unmatched_claim_roles": [
                        *item.unmatched_claim_roles,
                        "明确冲突标记与方向结论不一致",
                    ],
                }
            )
        normalized[item.occurrence_id] = item
    return normalized


def _apply_relation_review(
    parsed: CitationSupportOutput,
    claim_roles: dict[str, CitationClaimRoleInput],
    evidence_roles: dict[str, CitationEvidenceRoleInput],
    relation_checks: dict[str, CitationRelationReviewOutput],
) -> CitationSupportOutput:
    """把窄关系核验结果并入主判官结论，并阻止方向反转的 supported。"""

    citations: list[CitationResultOutput] = []
    for item in parsed.citations:
        claim = claim_roles.get(item.occurrence_id)
        if claim is None:
            citations.append(item)
            continue
        if not claim.relation_present:
            citations.append(
                item.model_copy(
                    update={
                        "relation_check": CitationRelationCheckOutput(
                            relation_present=False,
                        )
                    }
                )
            )
            continue
        relation_check = relation_checks.get(item.occurrence_id)
        evidence_check = evidence_roles.get(item.occurrence_id)
        if relation_check is None or evidence_check is None:
            citations.append(
                item.model_copy(
                    update={
                        "verdict": "unknown",
                        "reason": f"{item.reason}；关系判官未返回比较结果",
                    }
                )
            )
            continue
        combined = CitationRelationCheckOutput(
            relation_present=True,
            relation_type=claim.relation_type,
            source_objective=evidence_check.source_objective,
            claim_objective=claim.claim_objective,
            evidence_roles=evidence_check.evidence_roles,
            claim_roles=claim.claim_roles,
            direction_preserved=relation_check.direction_preserved,
            explicit_conflict=relation_check.explicit_conflict,
        )
        numeric_conflict = _numeric_role_conflict(
            claim.claim_roles,
            evidence_check.evidence_roles,
        )
        if numeric_conflict is not None:
            combined = combined.model_copy(
                update={
                    "direction_preserved": False,
                    "explicit_conflict": True,
                }
            )
            citations.append(
                item.model_copy(
                    update={
                        "verdict": "unsupported",
                        "reason": f"{item.reason}；关系角色确定性冲突：{numeric_conflict}",
                        "relation_check": combined,
                    }
                )
            )
            continue
        substantive_unmatched = _has_substantive_unmatched_roles(
            claim.claim_roles,
            relation_check.unmatched_claim_roles,
        )
        if (
            relation_check.direction_preserved is True
            and relation_check.explicit_conflict is False
            and not substantive_unmatched
        ):
            citations.append(item.model_copy(update={"relation_check": combined}))
            continue
        if relation_check.direction_preserved is None or substantive_unmatched:
            citations.append(
                item.model_copy(
                    update={
                        "verdict": "unknown",
                        "reason": f"{item.reason}；关系判官无法可靠确定目标或语义角色",
                        "relation_check": combined,
                    }
                )
            )
            continue
        citations.append(
            item.model_copy(
                update={
                    "verdict": "unsupported",
                    "reason": f"{item.reason}；关系方向核验：{relation_check.reason}",
                    "relation_check": combined,
                }
            )
        )
    return parsed.model_copy(update={"citations": citations})


def _has_substantive_unmatched_roles(
    claim_roles: list[CitationRoleAssertionInput],
    unmatched_claim_roles: list[str | CitationRoleAssertionInput],
) -> bool:
    """忽略 other 背景事实未匹配；只有实质角色缺失才进入 pending。"""

    other_facts = {assertion.fact.strip() for assertion in claim_roles if assertion.role == "other"}
    return any(
        (value if isinstance(value, str) else value.fact).strip() not in other_facts
        for value in unmatched_claim_roles
    )


def _numeric_role_conflict(
    claim_roles: list[CitationRoleAssertionInput],
    evidence_roles: list[CitationRoleAssertionInput],
) -> str | None:
    """对同一带数字事实出现互斥角色时做确定性冲突检查。"""

    role_families = {
        "benefit": "value",
        "cost": "value",
        "cause": "cause_effect",
        "effect": "cause_effect",
        "condition": "condition_result",
        "result": "condition_result",
        "direction": "direction",
    }
    evidence_roles_by_number: dict[str, set[str]] = {}
    for assertion in evidence_roles:
        for number in _numeric_fact_tokens(assertion.fact):
            evidence_roles_by_number.setdefault(number, set()).add(assertion.role)
    for assertion in claim_roles:
        claim_family = role_families.get(assertion.role)
        if claim_family is None:
            continue
        for number in _numeric_fact_tokens(assertion.fact):
            evidence_roles_for_number = evidence_roles_by_number.get(number, set())
            comparable_evidence_roles = {
                role
                for role in evidence_roles_for_number
                if role_families.get(role) == claim_family
            }
            if len(comparable_evidence_roles) == 1:
                evidence_role = next(iter(comparable_evidence_roles))
                if evidence_role != assertion.role:
                    return (
                        f"数字 {number} 在 claim 中为 {assertion.role}，"
                        f"在 evidence 中为 {evidence_role}"
                    )
    return None


def _numeric_fact_tokens(text: str) -> set[str]:
    """提取事实中的百分比或普通数字，作为角色对齐锚点。"""

    return set(re.findall(r"\d+(?:\.\d+)?%?", text))


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
