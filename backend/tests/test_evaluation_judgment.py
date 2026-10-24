from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.evaluation.database import EvaluationDatabase
from app.evaluation.evidence import split_citation_contexts
from app.evaluation.judgment import EvaluationJudgmentService
from app.evaluation.models import (
    CheckReviewRequest,
    CitationReviewItem,
    JudgeOverrideRequest,
    StartJudgmentRequest,
)
from app.evaluation.repository import EvaluationRepository
from app.evaluation.storage_models import (
    EvalAttemptRow,
    EvalExecutionPlanRow,
    EvalExecutionRunRow,
)
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
    FakeChatProvider,
)
from app.storage.credentials import CHAT_API_KEY_USERNAME
from app.tracing.catalog import TraceNotFoundError
from app.tracing.models import (
    TraceArtifactRecord,
    TraceDetailRecord,
    TraceManifestRecord,
    TraceSpanRecord,
)


class _MemoryCredentials:
    def get(self, username: str) -> str | None:
        return "judge-key" if username == CHAT_API_KEY_USERNAME else None

    def set(self, username: str, value: str) -> None:
        raise AssertionError("not used")

    def delete(self, username: str) -> None:
        raise AssertionError("not used")


class _FakeTraceCatalog:
    def __init__(
        self,
        root: Path,
        *,
        answer_completeness: str = "complete",
        blocks: list[dict[str, object]] | None = None,
    ) -> None:
        payload = {
            "blocks": blocks
            or [
                {
                    "block_id": "block_1",
                    "doc_id": "doc_1",
                    "doc_title": "测试文档",
                    "source_label": "doc",
                    "section_path": ["边界"],
                    "text": "MCP 沙箱是本文最重要的原文证据。",
                }
            ]
        }
        self._artifact_path = root / "payload.json"
        self._artifact_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        artifact = TraceArtifactRecord(
            artifact_id="a" * 64,
            kind="tool.payload",
            mime_type="application/json",
            size_bytes=self._artifact_path.stat().st_size,
            content_hash=hashlib.sha256(self._artifact_path.read_bytes()).hexdigest(),
            state="stored",
            relative_path="artifacts/payload.json",
        )
        started_at = "2026-10-23T00:00:00+00:00"
        self._detail = TraceDetailRecord(
            manifest=TraceManifestRecord(
                trace_id="b" * 32,
                mode="full",
                app_version="0.1.0",
                started_at=started_at,
                ended_at="2026-10-23T00:00:02+00:00",
                duration_ms=2000,
                status="succeeded",
                termination_reason="finish",
                answer_completeness=answer_completeness,
                question_preview="测试",
                snapshot_id="s_0123456789abcdef",
                snapshot_status="ready",
                completeness="complete",
                agent_turns=2,
                tool_calls=1,
                model_calls=2,
                retry_count=0,
                input_tokens=10,
                output_tokens=5,
                artifacts=[artifact],
                replay_scope="retrieval",
            ),
            spans=[
                TraceSpanRecord(
                    trace_id="b" * 32,
                    span_id="c" * 16,
                    parent_span_id=None,
                    name="tool",
                    kind="TOOL",
                    start_time_unix_nano=1,
                    end_time_unix_nano=2,
                    started_at=started_at,
                    ended_at="2026-10-23T00:00:01+00:00",
                    status="ok",
                    attributes={"app.payload_artifact": artifact.model_dump(mode="json")},
                )
            ],
        )

    def load(self, trace_id: str) -> TraceDetailRecord:
        assert trace_id == self._detail.manifest.trace_id
        return self._detail

    def artifact_path(self, trace_id: str, artifact_id: str) -> Path:
        assert trace_id == self._detail.manifest.trace_id
        assert artifact_id == "a" * 64
        return self._artifact_path


class _MissingTraceCatalog:
    def load(self, trace_id: str) -> TraceDetailRecord:
        raise TraceNotFoundError(trace_id)


class _UnavailableChatProvider:
    def __init__(self) -> None:
        self._profile = FakeChatProvider(["unused"]).profile

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        raise ChatProviderError(
            code="chat_upstream_unavailable",
            message="upstream unavailable",
            retryable=True,
        )


class _CitationBatchAwareProvider:
    """按请求中的 occurrence 动态返回引用结论，供多批集成测试使用。"""

    def __init__(
        self,
        *,
        first_batch_verdicts: list[str] | None = None,
        omit_last_occurrence: bool = False,
    ) -> None:
        self._profile = FakeChatProvider(["unused"]).profile
        self._first_batch_verdicts = first_batch_verdicts
        self._omit_last_occurrence = omit_last_occurrence
        self.requests: list[tuple[ChatMessage, ...]] = []
        self.max_output_tokens: list[int | None] = []
        self.citation_request_count = 0

    @property
    def profile(self) -> ChatProviderProfile:
        return self._profile

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        self.requests.append(tuple(messages))
        self.max_output_tokens.append(max_output_tokens)
        payload = json.loads(messages[-1].content)
        if "occurrences" in payload:
            occurrence_by_id = {item["occurrence_id"]: item for item in payload["occurrences"]}
            evidence_by_id = {item["block_id"]: item["text"] for item in payload["citations"]}
            verdicts = (
                self._first_batch_verdicts
                if self.citation_request_count == 0 and self._first_batch_verdicts is not None
                else ["supported"] * len(occurrence_by_id)
            )
            self.citation_request_count += 1
            occurrence_items = list(occurrence_by_id.items())
            if self._omit_last_occurrence and occurrence_items:
                occurrence_items = occurrence_items[:-1]
            selected_verdicts = [
                verdicts[index] if index < len(verdicts) else "supported"
                for index in range(len(occurrence_items))
            ]
            content = json.dumps(
                {
                    "citations": [
                        {
                            "occurrence_id": occurrence_id,
                            "verdict": selected_verdicts[index],
                            "reason": f"测试 verdict {selected_verdicts[index]}",
                            "evidence_quotes": [evidence_by_id[occurrence["block_id"]][:12]],
                        }
                        for index, (occurrence_id, occurrence) in enumerate(occurrence_items)
                    ]
                },
                ensure_ascii=False,
            )
        else:
            criterion = payload["criterion"]
            verdict = "satisfied" if criterion["rule_type"] == "R" else "not_violated"
            content = json.dumps(
                {"verdict": verdict, "reason": "测试满足当前 criterion"},
                ensure_ascii=False,
            )
        return ChatCompletionResult(
            content=content,
            request_id=f"fake-citation-{len(self.requests):04d}",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=sum(max(1, len(message.content) // 4) for message in messages),
            output_tokens=max(1, len(content) // 4),
        )


def _add_judgeable_run(
    database: EvaluationDatabase,
    repository: EvaluationRepository,
    *,
    case_id: str,
    answer: str | None,
    trace_id: str,
    run_id: str,
    trace_status: str = "complete",
    execution_status: str = "succeeded",
    outcome_class: str = "product",
    termination_reason: str = "finish",
    error_code: str | None = None,
) -> None:
    case = next(item for item in repository.list_case_catalog() if item.case_id == case_id)
    assert case.active_revision_id is not None
    active_suite = next(
        suite
        for suite in repository.list_suite_revisions()
        if suite.suite_id == "design03" and suite.review_status == "active" and not suite.stale
    )
    now = datetime.now(UTC)
    plan_id = f"plan_{run_id}"
    with database.session() as session, session.begin():
        session.add(
            EvalExecutionPlanRow(
                plan_id=plan_id,
                suite_revision_id=active_suite.revision_id,
                collection_snapshot_id="s_0123456789abcdef",
                run_type="quality",
                mode="formal",
                agent_version_set_json="{}",
                model_config_json="{}",
                run_policy_json="{}",
                experiment_variable_json="{}",
                preflight_result_json="[]",
                status="planned",
            )
        )
        session.add(
            EvalExecutionRunRow(
                run_id=run_id,
                plan_id=plan_id,
                status="sealed_complete",
                started_at=now,
                sealed_at=now,
            )
        )
        session.flush()
        session.add(
            EvalAttemptRow(
                attempt_id=f"attempt_{run_id}",
                run_id=run_id,
                case_revision_id=case.active_revision_id,
                session_id=f"session_{run_id}",
                trace_id=trace_id,
                trace_status=trace_status,
                execution_status=execution_status,
                outcome_class=outcome_class,
                termination_reason=termination_reason,
                error_code=error_code,
                answer=answer,
                input_tokens=10,
                output_tokens=5,
                model_calls=1,
                tool_calls=1,
                started_at=now,
                first_token_at=now,
                finished_at=now,
                usage_json="{}",
            )
        )


async def _wait_revision(
    service: EvaluationJudgmentService,
    revision_id: str,
) -> object:
    for _ in range(200):
        revision = service.get_revision(revision_id)
        if revision.status in {"complete", "incomplete", "canceled"}:
            return revision
        await asyncio.sleep(0.01)
    raise AssertionError("judgment revision did not finish")


def test_llm_judge_builds_whitelist_input_and_judgments(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        trace_catalog = _FakeTraceCatalog(tmp_path)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="MCP 是外部内容边界。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_judge_success",
        )
        case = next(item for item in repository.list_case_catalog() if item.case_id == "c1-001")
        active = next(
            revision
            for revision in case.revisions
            if revision.revision_id == case.active_revision_id
        )
        provider = FakeChatProvider(
            [
                json.dumps(
                    {
                        "verdict": ("satisfied" if criterion.rule_type == "R" else "not_violated"),
                        "reason": f"满足 {criterion.title}",
                    },
                    ensure_ascii=False,
                )
                for criterion in active.criteria
            ]
            + [
                json.dumps(
                    {
                        "citations": [
                            {
                                "occurrence_id": "citation_0001",
                                "verdict": "supported",
                                "reason": "引用原文支持答案主张。",
                                "evidence_quotes": ["MCP 沙箱是本文最重要的原文证据。"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=trace_catalog,  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )
        started = await service.start(
            "run_judge_success",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)
        assert revision.status == "complete", [
            (item.error_code, item.reason, [inv.error_code for inv in item.invocations])
            for item in revision.judgments
        ]
        assert len(revision.judgments) == 3
        assert all(item.executor == "llm_judge" for item in revision.judgments)
        assert all(item.normalized_result == "pass" for item in revision.judgments)
        assert revision.result_matrix is not None
        assert revision.result_matrix.totals.case_count == 1
        assert revision.result_matrix.categories[0].cases[0].case_id == "c1-001"
        assert (
            revision.result_matrix.categories[0].cases[0].result
            == revision.case_aggregates[0].result
        )
        assert all(
            invocation.input_json
            and json.loads(invocation.input_json)["prompt_version"] == "judge-rubric-v2"
            for judgment in revision.judgments
            for invocation in judgment.invocations
        )
        assert (
            next(
                check for check in revision.checks if check.checker_id == "citation_integrity"
            ).result
            == "pass"
        )
        assert (
            next(
                check for check in revision.checks if check.checker_id == "citation_support"
            ).result
            == "pass"
        )
        first_request = provider.requests[0]
        judge_payload = json.loads(first_request[1].content)
        assert set(judge_payload) == {"answer", "criterion"}
        assert set(judge_payload["criterion"]) == {"rule_type", "rule_text"}

    asyncio.run(scenario())


def test_human_override_appends_judgment_and_switches_effective_binding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="MCP 是外部内容边界。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_human_override",
        )
        case = next(item for item in repository.list_case_catalog() if item.case_id == "c1-001")
        active = next(
            revision
            for revision in case.revisions
            if revision.revision_id == case.active_revision_id
        )
        provider = FakeChatProvider(
            [
                json.dumps(
                    {
                        "verdict": "satisfied" if criterion.rule_type == "R" else "not_violated",
                        "reason": f"满足 {criterion.title}",
                    },
                    ensure_ascii=False,
                )
                for criterion in active.criteria
            ]
            + [
                json.dumps(
                    {
                        "citations": [
                            {
                                "occurrence_id": "citation_0001",
                                "verdict": "supported",
                                "reason": "引用原文支持答案主张。",
                                "evidence_quotes": ["MCP 沙箱是本文最重要的原文证据。"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            ]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )
        started = await service.start(
            "run_human_override",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        initial = await _wait_revision(service, started.revision_id)
        target = next(
            item
            for item in initial.judgments
            if item.criterion_revision_id == "ccr_c1_001_r1_minimal_judge_v2"
        )
        original_ids = {item.judgment_id for item in initial.judgments}

        updated = service.override_judgment(
            initial.revision_id,
            JudgeOverrideRequest(
                attempt_id=target.attempt_id,
                criterion_revision_id=target.criterion_revision_id,
                verdict="unsatisfied",
                reason="人工确认该答案没有满足 R1。",
                reviewer="reviewer-a",
            ),
        )

        assert original_ids.issubset({item.judgment_id for item in updated.judgments})
        effective = next(
            item
            for item in updated.effective_bindings
            if item.criterion_revision_id == target.criterion_revision_id
        )
        assert effective.executor == "human"
        assert effective.normalized_result == "fail"
        history = [
            item
            for item in updated.judgment_bindings
            if item.criterion_revision_id == target.criterion_revision_id
        ]
        assert len(history) == 2
        assert sum(item.is_effective for item in history) == 1
        attempt_aggregate = next(
            item for item in updated.attempt_aggregates if item.attempt_id == target.attempt_id
        )
        assert attempt_aggregate.result == "fail"

        final = service.override_judgment(
            initial.revision_id,
            JudgeOverrideRequest(
                attempt_id=target.attempt_id,
                criterion_revision_id=target.criterion_revision_id,
                verdict="satisfied",
                reason="再次复核后确认 R1 实际满足。",
                reviewer="reviewer-b",
            ),
        )
        final_effective = next(
            item
            for item in final.effective_bindings
            if item.criterion_revision_id == target.criterion_revision_id
        )
        assert final_effective.executor == "human"
        assert final_effective.normalized_result == "pass"
        final_history = [
            item
            for item in final.judgment_bindings
            if item.criterion_revision_id == target.criterion_revision_id
        ]
        assert len(final_history) == 3
        assert sum(item.is_effective for item in final_history) == 1
        final_attempt = next(
            item for item in final.attempt_aggregates if item.attempt_id == target.attempt_id
        )
        assert final_attempt.result == "pass"

    asyncio.run(scenario())


def test_human_can_review_llm_citation_support_but_not_integrity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="MCP 是外部内容边界。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_check_review",
        )
        case = next(item for item in repository.list_case_catalog() if item.case_id == "c1-001")
        active = next(
            revision
            for revision in case.revisions
            if revision.revision_id == case.active_revision_id
        )
        provider = FakeChatProvider(
            [
                json.dumps(
                    {
                        "verdict": "satisfied" if criterion.rule_type == "R" else "not_violated",
                        "reason": f"满足 {criterion.title}",
                    },
                    ensure_ascii=False,
                )
                for criterion in active.criteria
            ]
            + [
                json.dumps(
                    {
                        "citations": [
                            {
                                "occurrence_id": "citation_0001",
                                "verdict": "supported",
                                "reason": "引用原文支持答案主张。",
                                "evidence_quotes": ["MCP 沙箱是本文最重要的原文证据。"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            ]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )
        started = await service.start(
            "run_check_review",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        initial = await _wait_revision(service, started.revision_id)
        attempt_id = initial.judgments[0].attempt_id
        original_support = next(
            check for check in initial.checks if check.checker_id == "citation_support"
        )

        reviewed = service.review_llm_check(
            initial.revision_id,
            CheckReviewRequest(
                attempt_id=attempt_id,
                checker_id="citation_support",
                citations=[
                    CitationReviewItem(
                        block_id="block_1",
                        verdict="unsupported",
                        reason="人工确认证据原文没有支持该主张。",
                    )
                ],
                reason="复核引用支持。",
                reviewer="reviewer-a",
            ),
        )

        assert len(reviewed.check_reviews) == 1
        assert original_support.result == "pass"
        attempt_aggregate = next(
            item for item in reviewed.attempt_aggregates if item.attempt_id == attempt_id
        )
        assert "citation_support" in attempt_aggregate.check_failures
        with pytest.raises(ValueError):
            service.review_llm_check(
                initial.revision_id,
                CheckReviewRequest(
                    attempt_id=attempt_id,
                    checker_id="citation_integrity",
                    citations=[
                        CitationReviewItem(
                            block_id="block_1",
                            verdict="supported",
                            reason="不允许改程序检查。",
                        )
                    ],
                    reason="非法覆盖。",
                    reviewer="reviewer-a",
                ),
            )

    asyncio.run(scenario())


def test_failed_product_attempt_without_answer_is_still_judged(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="cf-003",
            answer=None,
            trace_id="b" * 32,
            run_id="run_failed_product",
            execution_status="failed",
            outcome_class="product",
            termination_reason="request_budget_exceeded",
            error_code="request_budget_exceeded",
        )
        provider = FakeChatProvider(
            [
                json.dumps(
                    {
                        "verdict": "unsatisfied",
                        "reason": "答案为空，没有告知用户回答被截断。",
                    },
                    ensure_ascii=False,
                )
            ]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(  # type: ignore[arg-type]
                tmp_path,
                answer_completeness="partial",
            ),
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )
        started = await service.start(
            "run_failed_product",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=100_000,
                judge_review_confirmed=True,
            ),
        )

        revision = await _wait_revision(service, started.revision_id)

        assert revision.status == "complete"
        assert len(revision.judgments) == 2
        checker_judgment = next(item for item in revision.judgments if item.executor == "checker")
        judge_judgment = next(item for item in revision.judgments if item.executor == "llm_judge")
        assert checker_judgment.normalized_result == "pass"
        assert judge_judgment.normalized_result == "fail"
        with pytest.raises(ValueError, match="only llm_judge"):
            service.override_judgment(
                revision.revision_id,
                JudgeOverrideRequest(
                    attempt_id=checker_judgment.attempt_id,
                    criterion_revision_id=checker_judgment.criterion_revision_id,
                    verdict="unsatisfied",
                    reason="不允许人工覆盖 checker。",
                    reviewer="reviewer-a",
                ),
            )
        payload = json.loads(provider.requests[0][1].content)
        assert payload["answer"] == ""
        assert (
            next(
                check for check in revision.checks if check.checker_id == "citation_integrity"
            ).result
            == "pass"
        )
        assert not any(check.checker_id == "citation_support" for check in revision.checks)

    asyncio.run(scenario())


def test_invalid_judge_output_becomes_pending(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="答案【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_judge_invalid",
        )
        provider = FakeChatProvider(
            [
                "not-json",
                "not-json",
                "not-json",
                json.dumps(
                    {
                        "citations": [
                            {
                                "occurrence_id": "citation_0001",
                                "verdict": "supported",
                                "reason": "引用原文支持答案主张。",
                                "evidence_quotes": ["MCP 沙箱是本文最重要的原文证据。"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
            ]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )
        started = await service.start(
            "run_judge_invalid",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)
        assert revision.status == "incomplete"
        assert all(item.normalized_result == "pending" for item in revision.judgments)
        assert all(
            invocation.status == "output_invalid"
            for judgment in revision.judgments
            for invocation in judgment.invocations
        )

    asyncio.run(scenario())


def test_citation_support_is_separate_from_criterion_judgments(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="MCP 是外部内容边界。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_citation_support",
        )
        case = next(item for item in repository.list_case_catalog() if item.case_id == "c1-001")
        active = next(
            revision
            for revision in case.revisions
            if revision.revision_id == case.active_revision_id
        )
        provider = FakeChatProvider(
            [
                json.dumps(
                    {
                        "verdict": ("satisfied" if criterion.rule_type == "R" else "not_violated"),
                        "reason": f"满足 {criterion.title}",
                    },
                    ensure_ascii=False,
                )
                for criterion in active.criteria
            ]
            + [
                json.dumps(
                    {
                        "citations": [
                            {
                                "occurrence_id": "citation_0001",
                                "verdict": "unsupported",
                                "reason": "引用原文没有提到 MCP。",
                                "evidence_quotes": ["MCP 沙箱是本文最重要的原文证据。"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_support",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        assert revision.status == "complete"
        assert all(judgment.normalized_result == "pass" for judgment in revision.judgments)
        citation_support = next(
            check for check in revision.checks if check.checker_id == "citation_support"
        )
        assert citation_support.result == "fail"
        assert citation_support.error_code == "citation_not_supported"
        citation_request = json.loads(provider.requests[-1][1].content)
        assert set(citation_request) == {"contexts", "citations", "occurrences"}
        assert citation_request["contexts"][0]["kind"] == "paragraph"
        assert citation_request["contexts"][0]["text"] == ("MCP 是外部内容边界。【来源：block_1】")
        assert citation_request["citations"][0]["block_id"] == "block_1"
        assert citation_request["occurrences"][0] == {
            "occurrence_id": "citation_0001",
            "block_id": "block_1",
            "context_id": "context_0001",
        }
        assert set(citation_request["citations"][0]) == {
            "block_id",
            "doc_title",
            "section_path",
            "text",
        }

    asyncio.run(scenario())


def test_citation_contexts_cover_paragraphs_list_items_and_table_rows() -> None:
    answer = """第一句是背景。第二句才引用证据。【来源：block_1】

- 列表第一句。
  列表第二句才引用证据。【来源：block_2】

| 字段 | 内容 |
| --- | --- |
| 表格 | 表格单元格引用证据。【来源：block_3】 |
"""

    contexts = split_citation_contexts(answer)

    assert [item.kind for item in contexts] == ["paragraph", "list_item", "table_row"]
    assert "第一句是背景。第二句才引用证据。" in contexts[0].text
    assert "列表第一句。" in contexts[1].text
    assert "列表第二句" in contexts[1].text
    assert "表格单元格引用证据" in contexts[2].text


def test_citation_support_splits_large_answer_and_covers_every_occurrence(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        evidence_text = "长证据" * 1_000
        blocks = [
            {
                "block_id": f"block_{index:02d}",
                "doc_id": f"doc_{index:02d}",
                "doc_title": f"文档 {index}",
                "source_label": "doc",
                "section_path": ["测试"],
                "text": f"{index}:{evidence_text}",
            }
            for index in range(22)
        ]
        answer = "\n\n".join(
            f"第 {index} 段包含两句。第二句由引用支持。【来源：block_{index:02d}】"
            for index in range(22)
        )
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer=answer,
            trace_id="b" * 32,
            run_id="run_citation_batches",
        )
        provider = _CitationBatchAwareProvider()
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path, blocks=blocks),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_batches",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=100_000_000,
                request_budget=100,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        assert revision.status == "complete"
        citation_support = next(
            check for check in revision.checks if check.checker_id == "citation_support"
        )
        batching = citation_support.payload["batching"]
        assert citation_support.result == "pass"
        assert batching["occurrence_count"] == 22
        assert batching["block_count"] == 22
        assert batching["batch_count"] >= 2
        assert provider.citation_request_count == batching["batch_count"]
        assert all(
            max_output_tokens == 4_096
            for request, max_output_tokens in zip(
                provider.requests,
                provider.max_output_tokens,
                strict=True,
            )
            if "occurrences" in json.loads(request[-1].content)
        )
        assert len(citation_support.payload["occurrence_results"]) == 22

    asyncio.run(scenario())


def test_citation_support_batch_failure_cannot_hide_behind_supported_batches(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        evidence_text = "长证据" * 1_000
        blocks = [
            {
                "block_id": f"block_{index:02d}",
                "doc_id": f"doc_{index:02d}",
                "doc_title": f"文档 {index}",
                "source_label": "doc",
                "section_path": ["测试"],
                "text": f"{index}:{evidence_text}",
            }
            for index in range(22)
        ]
        answer = "\n\n".join(
            f"第 {index} 段包含主张。【来源：block_{index:02d}】" for index in range(22)
        )
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer=answer,
            trace_id="b" * 32,
            run_id="run_citation_mixed",
        )
        provider = _CitationBatchAwareProvider(
            first_batch_verdicts=["unsupported", *(["unknown"] * 19)]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path, blocks=blocks),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_mixed",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=100_000_000,
                request_budget=100,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        citation_support = next(
            check for check in revision.checks if check.checker_id == "citation_support"
        )
        assert citation_support.result == "fail"
        assert citation_support.error_code == "citation_not_supported"
        assert citation_support.payload["batching"]["batch_count"] >= 2
        assert citation_support.payload["batches"][0]["result"] == "fail"
        assert citation_support.payload["batches"][-1]["result"] == "pass"

    asyncio.run(scenario())


def test_citation_support_single_oversized_context_is_explicitly_pending(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        blocks = [
            {
                "block_id": "block_1",
                "doc_id": "doc_1",
                "doc_title": "测试文档",
                "source_label": "doc",
                "section_path": ["测试"],
                "text": "超长证据" * 20_000,
            }
        ]
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="这段引用上下文异常长但不是截断测试。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_citation_oversized",
        )
        provider = _CitationBatchAwareProvider()
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path, blocks=blocks),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_oversized",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=100_000_000,
                request_budget=100,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        citation_support = next(
            check for check in revision.checks if check.checker_id == "citation_support"
        )
        assert citation_support.result == "pending"
        assert citation_support.error_code == "citation_support_unit_too_large"
        assert citation_support.payload["batching"]["batch_count"] == 0
        assert citation_support.payload["batching"]["preflight_failure_count"] == 1
        assert provider.citation_request_count == 0

    asyncio.run(scenario())


def test_citation_support_without_locatable_answer_context_is_pending(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="```text\n代码中的引用不构成答案主张【来源：block_1】\n```",
            trace_id="b" * 32,
            run_id="run_citation_context_missing",
        )
        provider = _CitationBatchAwareProvider()
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_context_missing",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                request_budget=100,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        citation_support = next(
            check for check in revision.checks if check.checker_id == "citation_support"
        )
        assert citation_support.result == "pending"
        assert citation_support.error_code == "citation_support_context_missing"
        assert citation_support.payload["batching"]["preflight_failure_count"] == 1
        assert provider.citation_request_count == 0

    asyncio.run(scenario())


def test_citation_support_missing_occurrence_output_is_pending(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="MCP 是外部内容边界。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_citation_occurrence_missing",
        )
        provider = _CitationBatchAwareProvider(omit_last_occurrence=True)
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_occurrence_missing",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                request_budget=100,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        citation_support = next(
            check for check in revision.checks if check.checker_id == "citation_support"
        )
        assert citation_support.result == "pending"
        assert citation_support.error_code == "citation_occurrence_set_invalid"
        occurrence_results = citation_support.payload["occurrence_results"]
        assert occurrence_results[0]["verdict"] == "unknown"

    asyncio.run(scenario())


def test_truncation_checker_judgment(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="cf-003",
            answer="第一部分内容……（内容未完）",
            trace_id="b" * 32,
            run_id="run_checker",
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(
                tmp_path,
                answer_completeness="partial",
            ),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: FakeChatProvider(
                [
                    json.dumps(
                        {
                            "verdict": "satisfied",
                            "reason": "答案明确声明内容未完成。",
                        },
                        ensure_ascii=False,
                    )
                ]
            ),
        )
        started = await service.start(
            "run_checker",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)
        assert revision.status == "complete"
        assert len(revision.judgments) == 2
        checker = next(item for item in revision.judgments if item.executor == "checker")
        llm = next(item for item in revision.judgments if item.executor == "llm_judge")
        assert checker.normalized_result == "pass"
        assert checker.invocations == []
        assert llm.normalized_result == "pass"

    asyncio.run(scenario())


def test_rejudge_creates_new_revision_without_replacing_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="答案。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_judge_repeat",
        )
        case = next(item for item in repository.list_case_catalog() if item.case_id == "c1-001")
        active = next(
            revision
            for revision in case.revisions
            if revision.revision_id == case.active_revision_id
        )
        criteria_outputs = [
            json.dumps(
                {
                    "verdict": ("satisfied" if criterion.rule_type == "R" else "not_violated"),
                    "reason": f"满足 {criterion.title}",
                },
                ensure_ascii=False,
            )
            for criterion in active.criteria
        ]
        citation_output = json.dumps(
            {
                "citations": [
                    {
                        "occurrence_id": "citation_0001",
                        "verdict": "supported",
                        "reason": "引用原文支持答案主张。",
                        "evidence_quotes": ["MCP 沙箱是本文最重要的原文证据。"],
                    }
                ]
            },
            ensure_ascii=False,
        )
        provider = FakeChatProvider(
            [response for _ in range(2) for response in [*criteria_outputs, citation_output]]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        first_started = await service.start(
            "run_judge_repeat",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        first = await _wait_revision(service, first_started.revision_id)
        second_started = await service.start(
            "run_judge_repeat",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        second = await _wait_revision(service, second_started.revision_id)

        revisions = service.list_run_revisions("run_judge_repeat")
        assert {revision.revision_id for revision in revisions} == {
            first.revision_id,
            second.revision_id,
        }
        assert {
            judgment.judgment_id for judgment in service.get_revision(first.revision_id).judgments
        }.isdisjoint(
            {
                judgment.judgment_id
                for judgment in service.get_revision(second.revision_id).judgments
            }
        )

    asyncio.run(scenario())


def test_request_budget_exhaustion_keeps_judgments_pending(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="答案。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_judge_budget",
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: FakeChatProvider(["unused"]),
        )

        started = await service.start(
            "run_judge_budget",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                request_budget=1,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        assert revision.status == "incomplete"
        assert revision.summary["pending"] == len(revision.judgments)
        assert all(
            judgment.error_code == "judge_budget_exhausted" for judgment in revision.judgments
        )
        assert all(not judgment.invocations for judgment in revision.judgments)

    asyncio.run(scenario())


def test_judging_rejects_run_bound_to_legacy_prompt_revision(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="答案。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_judge_legacy_prompt",
        )
        with database.session() as session, session.begin():
            attempt = session.get(
                EvalAttemptRow,
                "attempt_run_judge_legacy_prompt",
            )
            assert attempt is not None
            attempt.case_revision_id = "cr_c1_001_selfcontained_v1"
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: FakeChatProvider(["unused"]),
        )

        with pytest.raises(ValueError, match="different judge prompt revision"):
            await service.start(
                "run_judge_legacy_prompt",
                StartJudgmentRequest(
                    concurrency=1,
                    token_budget=1_000_000,
                    judge_review_confirmed=True,
                ),
            )

    asyncio.run(scenario())


def test_missing_trace_only_blocks_trace_dependent_checks(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="答案。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_judge_trace_missing",
        )
        case = next(item for item in repository.list_case_catalog() if item.case_id == "c1-001")
        active = next(
            revision
            for revision in case.revisions
            if revision.revision_id == case.active_revision_id
        )
        provider = FakeChatProvider(
            [
                json.dumps(
                    {
                        "verdict": ("satisfied" if criterion.rule_type == "R" else "not_violated"),
                        "reason": f"满足 {criterion.title}",
                    },
                    ensure_ascii=False,
                )
                for criterion in active.criteria
            ]
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_MissingTraceCatalog(),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_judge_trace_missing",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        assert revision.status == "incomplete"
        assert all(judgment.normalized_result == "pass" for judgment in revision.judgments), [
            (judgment.criterion_revision_id, judgment.error_code, judgment.reason)
            for judgment in revision.judgments
        ]
        assert all(judgment.invocations for judgment in revision.judgments)
        assert (
            next(
                check for check in revision.checks if check.checker_id == "citation_integrity"
            ).error_code
            == "judge_trace_missing"
        )

    asyncio.run(scenario())


def test_retryable_provider_failure_keeps_judgments_pending(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="答案。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_judge_upstream",
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: _UnavailableChatProvider(),
        )

        started = await service.start(
            "run_judge_upstream",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        assert revision.status == "incomplete"
        assert all(
            judgment.error_code == "judge_upstream_unavailable" for judgment in revision.judgments
        )
        assert all(
            len(judgment.invocations) == 2
            and all(
                invocation.error_code == "chat_upstream_unavailable"
                for invocation in judgment.invocations
            )
            for judgment in revision.judgments
        )

    asyncio.run(scenario())
