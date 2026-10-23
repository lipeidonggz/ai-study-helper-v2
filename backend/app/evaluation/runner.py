from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from app.agent.factory import AgentExecutionService
from app.agent.prompt import PromptBundle, release_prompt_bundle
from app.agent.runtime import AgentEvent, AgentRunResult
from app.evaluation.models import (
    ExecutionPlanView,
    PlanCaseView,
    RunView,
)
from app.evaluation.outcomes import (
    AttemptOutcomeClass,
    classify_attempt_outcome,
    is_product_sample,
)
from app.evaluation.repository import EvaluationRepository
from app.settings.models import RuntimeSettingsResponse

UPSTREAM_PAUSE_CODES = {
    "chat_rate_limited",
    "chat_upstream_unavailable",
    "embedding_upstream_unavailable",
}


@dataclass(slots=True)
class _BudgetReservation:
    """一个 attempt 启动前的保守预算预留。"""

    tokens: int
    requests: int


@dataclass(slots=True)
class _AttemptOutcome:
    """worker 返回给运行编排器的单次 attempt 结果。"""

    execution_status: str
    outcome_class: AttemptOutcomeClass
    error_code: str | None
    termination_reason: str | None
    input_tokens: int
    output_tokens: int
    model_calls: int


@dataclass(slots=True)
class _RunningAttempt:
    """执行中的 attempt 预算与所属 case。"""

    reservation: _BudgetReservation
    attempt_id: str
    case_revision_id: str


class _RunBudget:
    """在并发 attempt 间原子预留 token 与模型请求额度。"""

    def __init__(
        self,
        *,
        token_budget: int,
        request_budget: int,
        spent_tokens: int,
        spent_requests: int,
    ) -> None:
        self._token_remaining = max(0, token_budget - spent_tokens)
        self._request_remaining = max(0, request_budget - spent_requests)

    @property
    def exhausted(self) -> bool:
        """返回两个硬预算是否至少一个已无法继续预留。"""

        return self._token_remaining <= 0 or self._request_remaining <= 0

    def reserve(self, *, tokens: int, requests: int) -> bool:
        """预留一次 attempt 的最大可能消耗。"""

        if self._token_remaining < tokens or self._request_remaining < requests:
            return False
        self._token_remaining -= tokens
        self._request_remaining -= requests
        return True

    def settle(
        self,
        reservation: _BudgetReservation,
        *,
        actual_tokens: int,
        actual_requests: int,
    ) -> None:
        """按供应商 usage 和实际模型调用数退还未使用额度。"""

        self._token_remaining += max(0, reservation.tokens - actual_tokens)
        self._request_remaining += max(0, reservation.requests - actual_requests)


class EvaluationRunner:
    """在独立 eval sidecar 内串行 run、并发 attempt 的执行内核。"""

    def __init__(
        self,
        repository: EvaluationRepository,
        execution_service: AgentExecutionService,
    ) -> None:
        self._repository = repository
        self._execution_service = execution_service
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._pause_requested: set[str] = set()
        self._cancel_requested: set[str] = set()
        self._pause_reason: dict[str, str] = {}

    async def start(self, plan_id: str) -> RunView:
        """为计划创建 run，并启动后台 execution loop。"""

        active = self._repository.active_run()
        if active is not None:
            raise ValueError(f"another evaluation run is active: {active.run_id}")
        run = self._repository.create_run(plan_id)
        task = asyncio.create_task(
            self._execute(run.run_id),
            name=f"evaluation-run-{run.run_id}",
        )
        self._run_tasks[run.run_id] = task
        task.add_done_callback(lambda _task: self._run_tasks.pop(run.run_id, None))
        return run

    async def pause(self, run_id: str) -> RunView:
        """请求在正在执行的 attempt 完成后停止启动新 attempt。"""

        run = self._repository.get_run(run_id)
        if run.status not in {"planned", "running", "paused"}:
            raise ValueError("only an active run can pause")
        self._pause_requested.add(run_id)
        self._pause_reason[run_id] = "user_paused"
        return run

    async def resume(self, run_id: str) -> RunView:
        """从暂停现场继续缺少的 attempt；不重发 outcome_unknown。"""

        run = self._repository.get_run(run_id)
        if run.status != "paused":
            raise ValueError("only a paused run can resume")
        pending = self._run_tasks.get(run_id)
        if pending is not None:
            await pending
            self._run_tasks.pop(run_id, None)
        self._pause_requested.discard(run_id)
        self._pause_reason.pop(run_id, None)
        self._cancel_requested.discard(run_id)
        self._repository.set_run_status(run_id, "running")
        task = asyncio.create_task(
            self._execute(run_id),
            name=f"evaluation-run-{run_id}",
        )
        self._run_tasks[run_id] = task
        task.add_done_callback(lambda _task: self._run_tasks.pop(run_id, None))
        return self._repository.get_run(run_id)

    async def cancel(self, run_id: str) -> RunView:
        """请求在 attempt 安全边界取消 run，保留已生成结果。"""

        run = self._repository.get_run(run_id)
        if run.status not in {"planned", "running", "paused"}:
            raise ValueError("only an active run can cancel")
        self._cancel_requested.add(run_id)
        self._pause_requested.discard(run_id)
        if run.status == "paused" and run_id not in self._run_tasks:
            self._repository.set_run_status(run_id, "canceled")
        return self._repository.get_run(run_id)

    async def shutdown(self) -> None:
        """进程关闭时保留持久现场；重启恢复会把 running 标成未知。"""

        tasks = list(self._run_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._run_tasks.clear()

    async def _execute(self, run_id: str) -> None:
        run = self._repository.get_run(run_id)
        plan = self._repository.get_plan(run.plan_id)
        settings = RuntimeSettingsResponse.model_validate(
            plan.run_policy["runtime_settings"]
        ).model_copy(update={"trace_mode": "full"})
        prompt = release_prompt_bundle()
        required_attempts = sum(case.n for case in plan.cases)
        current = self._repository.get_run(run_id)
        replacement_limit = int(plan.run_policy["replacement_attempt_budget"])
        max_attempts = required_attempts + replacement_limit
        budget = _RunBudget(
            token_budget=int(plan.run_policy["total_token_budget"]),
            request_budget=int(plan.run_policy["model_request_budget"]),
            spent_tokens=current.totals["input_tokens"] + current.totals["output_tokens"],
            spent_requests=current.totals["model_calls"],
        )
        concurrency = int(plan.run_policy["concurrency"])
        timeout_seconds = int(plan.run_policy["attempt_timeout_seconds"])
        per_attempt_tokens = settings.max_request_tokens
        # Runtime 对每次模型调用最多做两次供应商尝试；预留上限必须覆盖
        # 工具轮次、最终回答和这些确定性重试，否则并发会突破请求硬预算。
        per_attempt_requests = 2 * (settings.max_tool_calls + 1)
        running: dict[asyncio.Task[_AttemptOutcome], _RunningAttempt] = {}
        consecutive_upstream_failures = 0
        self._repository.set_run_status(run_id, "running", started=True)

        try:
            while True:
                current = self._repository.get_run(run_id)
                if run_id in self._cancel_requested:
                    if running:
                        await asyncio.gather(*running)
                        running.clear()
                    self._repository.set_run_status(run_id, "canceled")
                    return
                running_case_counts = Counter(item.case_revision_id for item in running.values())
                deficits = self._case_deficits(
                    plan,
                    current,
                    running_case_counts,
                )
                if not deficits and not running:
                    self._repository.set_run_status(
                        run_id,
                        "sealed_complete",
                        sealed=True,
                    )
                    return
                if run_id in self._pause_requested or budget.exhausted:
                    if running:
                        await asyncio.gather(*running)
                        running.clear()
                        continue
                    pause_reason = self._pause_reason.get(
                        run_id,
                        "total_budget_exhausted" if budget.exhausted else "paused",
                    )
                    self._repository.set_run_status(
                        run_id,
                        "paused",
                        pause_reason=pause_reason,
                    )
                    return
                if current.totals["attempts"] >= max_attempts:
                    if running:
                        await asyncio.gather(*running)
                        running.clear()
                        continue
                    self._repository.set_run_status(
                        run_id,
                        "sealed_partial",
                        pause_reason="replacement_attempt_budget_exhausted",
                        sealed=True,
                    )
                    return

                while len(running) < concurrency and not self._pause_requested:
                    current = self._repository.get_run(run_id)
                    running_case_counts = Counter(
                        item.case_revision_id for item in running.values()
                    )
                    deficits = self._case_deficits(
                        plan,
                        current,
                        running_case_counts,
                    )
                    if not deficits:
                        break
                    if current.totals["attempts"] >= max_attempts:
                        break
                    case = deficits[0]
                    reservation = _BudgetReservation(
                        tokens=per_attempt_tokens,
                        requests=per_attempt_requests,
                    )
                    if not budget.reserve(
                        tokens=reservation.tokens,
                        requests=reservation.requests,
                    ):
                        self._pause_requested.add(run_id)
                        self._pause_reason[run_id] = "total_budget_exhausted"
                        break
                    replacement_for = self._replacement_parent(
                        current,
                        case,
                    )
                    attempt_id = self._repository.start_attempt(
                        run_id=run_id,
                        case_revision_id=case.case_revision_id,
                        replacement_for_attempt_id=replacement_for,
                    )
                    task = asyncio.create_task(
                        self._run_attempt(
                            attempt_id=attempt_id,
                            case=case,
                            plan=plan,
                            settings=settings,
                            prompt=prompt,
                            reservation=reservation,
                            budget=budget,
                            timeout_seconds=timeout_seconds,
                        ),
                        name=f"evaluation-attempt-{attempt_id}",
                    )
                    running[task] = _RunningAttempt(
                        reservation=reservation,
                        attempt_id=attempt_id,
                        case_revision_id=case.case_revision_id,
                    )

                if not running:
                    continue
                done, _pending = await asyncio.wait(
                    running,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for completed in done:
                    running_attempt = running.pop(completed)
                    reservation = running_attempt.reservation
                    try:
                        outcome = completed.result()
                    except Exception:
                        outcome = _AttemptOutcome(
                            execution_status="failed",
                            outcome_class="infrastructure",
                            error_code="evaluation_worker_exception",
                            termination_reason=None,
                            input_tokens=0,
                            output_tokens=0,
                            model_calls=0,
                        )
                    budget.settle(
                        reservation,
                        actual_tokens=outcome.input_tokens + outcome.output_tokens,
                        actual_requests=outcome.model_calls,
                    )
                    if outcome.error_code in UPSTREAM_PAUSE_CODES:
                        consecutive_upstream_failures += 1
                        if (
                            outcome.error_code == "chat_rate_limited"
                            or consecutive_upstream_failures >= 3
                        ):
                            self._pause_requested.add(run_id)
                            self._pause_reason[run_id] = (
                                "upstream_rate_limited"
                                if outcome.error_code == "chat_rate_limited"
                                else "continuous_upstream_failure"
                            )
                    elif is_product_sample(outcome.outcome_class):
                        consecutive_upstream_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            if run_id not in self._cancel_requested:
                self._repository.set_run_status(
                    run_id,
                    "paused",
                    pause_reason="evaluation_runner_failure",
                )
            raise
        finally:
            if running:
                for task in running:
                    task.cancel()
                await asyncio.gather(*running, return_exceptions=True)

    async def _run_attempt(
        self,
        *,
        attempt_id: str,
        case: PlanCaseView,
        plan: ExecutionPlanView,
        settings: RuntimeSettingsResponse,
        prompt: PromptBundle,
        reservation: _BudgetReservation,
        budget: _RunBudget,
        timeout_seconds: int,
    ) -> _AttemptOutcome:
        """执行一个 fresh Runtime 会话，并把结果和 trace 关联落库。"""

        first_token_at: datetime | None = None

        def emit(event: AgentEvent) -> None:
            nonlocal first_token_at
            if event.event_type == "answer_delta" and first_token_at is None:
                first_token_at = datetime.now(UTC)

        try:
            runtime = self._execution_service.create_runtime(
                settings_override=settings,
                prompt_bundle_override=prompt,
            )
            result = await asyncio.wait_for(
                runtime.run(
                    case.question,
                    emit=emit,
                    snapshot_id=plan.collection_snapshot_id,
                ),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            self._repository.finish_attempt(
                attempt_id,
                execution_status="outcome_unknown",
                trace_id=None,
                trace_status="missing",
                outcome_class="unknown",
                termination_reason="evaluation_attempt_cancelled",
                error_code="evaluation_attempt_cancelled",
                answer=None,
                input_tokens=0,
                output_tokens=0,
                model_calls=0,
                tool_calls=0,
                first_token_at=first_token_at,
                usage={"error_type": "CancelledError"},
            )
            raise
        except Exception as error:
            error_code = _error_code(error)
            self._repository.finish_attempt(
                attempt_id,
                execution_status="failed",
                trace_id=None,
                trace_status="missing",
                outcome_class="infrastructure",
                termination_reason=None,
                error_code=error_code,
                answer=None,
                input_tokens=0,
                output_tokens=0,
                model_calls=0,
                tool_calls=0,
                first_token_at=first_token_at,
                usage={"error_type": type(error).__name__},
            )
            return _AttemptOutcome(
                execution_status="failed",
                outcome_class="infrastructure",
                error_code=error_code,
                termination_reason=None,
                input_tokens=0,
                output_tokens=0,
                model_calls=0,
            )
        execution_status = _execution_status(result)
        trace_status = _trace_status(result)
        error_code = _result_error_code(result)
        outcome_class = classify_attempt_outcome(execution_status, error_code)
        self._repository.finish_attempt(
            attempt_id,
            execution_status=execution_status,
            trace_id=result.trace_id,
            trace_status=trace_status,
            outcome_class=outcome_class,
            termination_reason=result.termination_reason,
            error_code=error_code,
            answer=result.answer,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_calls=result.model_calls,
            tool_calls=result.tool_calls,
            first_token_at=first_token_at,
            usage={
                "agent_turns": result.agent_turns,
                "snapshot_id": result.snapshot_id,
                "prompt_bundle_version": result.prompt_bundle_version,
                "prompt_template_hash": result.prompt_template_hash,
                "observation_token_counter_version": (result.observation_token_counter_version),
            },
        )
        return _AttemptOutcome(
            execution_status=execution_status,
            outcome_class=outcome_class,
            error_code=error_code,
            termination_reason=result.termination_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            model_calls=result.model_calls,
        )

    @staticmethod
    def _case_deficits(
        plan: ExecutionPlanView,
        run: RunView,
        running_case_counts: Counter[str],
    ) -> list[PlanCaseView]:
        valid_counts = Counter(
            attempt.case_revision_id
            for attempt in run.attempts
            if is_product_sample(attempt.outcome_class)
        )
        return [
            case
            for case in plan.cases
            if valid_counts[case.case_revision_id] + running_case_counts[case.case_revision_id]
            < case.n
        ]

    @staticmethod
    def _replacement_parent(run: RunView, case: PlanCaseView) -> str | None:
        case_attempts = [
            attempt for attempt in run.attempts if attempt.case_revision_id == case.case_revision_id
        ]
        if len(case_attempts) < case.n:
            return None
        infrastructure_failure = next(
            (
                attempt
                for attempt in reversed(case_attempts)
                if attempt.outcome_class == "infrastructure"
            ),
            None,
        )
        return None if infrastructure_failure is None else infrastructure_failure.attempt_id


def _execution_status(result: AgentRunResult) -> str:
    if result.status == "succeeded":
        return "succeeded"
    if result.status == "partial":
        return "partial"
    if result.status == "canceled":
        return "canceled"
    return "failed"


def _trace_status(result: AgentRunResult) -> str:
    return result.trace_status


def _result_error_code(result: AgentRunResult) -> str | None:
    if result.status != "failed":
        return None
    limitation = next(
        (item for item in result.limitations if "unavailable" in item.code or "rate" in item.code),
        None,
    )
    if limitation is not None:
        return limitation.code
    return result.termination_reason or "agent_run_failed"


def _error_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "evaluation_attempt_timeout"
    code = getattr(error, "code", None)
    return code if isinstance(code, str) else "evaluation_attempt_exception"
