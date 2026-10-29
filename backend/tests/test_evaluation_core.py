from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.agent.prompt import PromptBundle
from app.agent.runtime import AgentEvent, AgentRunResult
from app.collections.models import CandidateSnapshotManifest, SnapshotTotals
from app.evaluation.cases import load_design_case_seeds
from app.evaluation.database import EvaluationDatabase, _migration_config
from app.evaluation.models import (
    CaseDraftCriterionView,
    CriterionSeed,
    PreflightRequest,
    QuickPlanRequest,
    SaveCaseDraftRequest,
    SaveSuiteDraftRequest,
)
from app.evaluation.outcomes import classify_attempt_outcome
from app.evaluation.planner import EvaluationPlanner
from app.evaluation.repository import EvaluationRepository
from app.evaluation.runner import EvaluationRunner
from app.evaluation.storage_models import (
    EvalCaseRevisionRow,
    EvalCaseRow,
    EvalSuiteRevisionCaseRow,
    EvalSuiteRevisionRow,
    EvalSuiteRow,
)
from app.settings.models import RuntimeSettingsResponse


class _StaticCollectionService:
    def __init__(self, manifest: CandidateSnapshotManifest) -> None:
        self._manifest = manifest

    def snapshot(self, snapshot_id: str) -> CandidateSnapshotManifest:
        assert snapshot_id == self._manifest.snapshot_id
        return self._manifest


class _FakeRuntime:
    def __init__(self, owner: _FakeExecutionService) -> None:
        self._owner = owner

    async def run(
        self,
        question: str,
        *,
        emit=None,
        snapshot_id: str | None = None,
    ) -> AgentRunResult:
        self._owner.calls.append((question, snapshot_id))
        self._owner.active += 1
        self._owner.max_active = max(self._owner.max_active, self._owner.active)
        if emit is not None:
            emit(AgentEvent(event_type="answer_delta", payload={"delta": "答"}))
        try:
            delay = self._owner.attempt_delays.pop(0) if self._owner.attempt_delays else 0.005
            await asyncio.sleep(delay)
            if self._owner.block_first and not self._owner.has_blocked:
                self._owner.has_blocked = True
                self._owner.entered.set()
                await self._owner.release.wait()
            should_fail = len(self._owner.calls) == 1 and self._owner.fail_first_with is not None
            status = "failed" if should_fail else "succeeded"
            answer = None if should_fail else "答案"
            completeness = "partial" if should_fail else "complete"
            termination_reason = self._owner.fail_first_with if should_fail else "finish"
            return AgentRunResult(
                status=status,
                trace_id=f"{len(self._owner.calls):032x}",
                answer=answer,
                answer_completeness=completeness,
                limitations=(),
                termination_reason=termination_reason,
                snapshot_id=snapshot_id,
                prompt_bundle_version="test-prompt-v1",
                prompt_template_hash="a" * 64,
                observation_token_counter_version="test-counter-v1",
                agent_turns=1,
                tool_calls=0,
                model_calls=1,
                input_tokens=10,
                output_tokens=2,
                sources=[],
                events=(),
                trace_status="complete",
            )
        finally:
            self._owner.active -= 1


class _FakeExecutionService:
    def __init__(
        self,
        *,
        block_first: bool = False,
        fail_first_with: str | None = None,
        attempt_delays: list[float] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.settings_overrides: list[RuntimeSettingsResponse] = []
        self.prompt_overrides: list[PromptBundle | None] = []
        self.block_first = block_first
        self.fail_first_with = fail_first_with
        self.attempt_delays = list(attempt_delays or [])
        self.has_blocked = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    def create_runtime(
        self,
        *,
        settings_override: RuntimeSettingsResponse | None = None,
        prompt_bundle_override: PromptBundle | None = None,
    ) -> _FakeRuntime:
        assert settings_override is not None
        self.settings_overrides.append(settings_override)
        self.prompt_overrides.append(prompt_bundle_override)
        return _FakeRuntime(self)


def _manifest() -> CandidateSnapshotManifest:
    return CandidateSnapshotManifest(
        snapshot_id="s_0123456789abcdef",
        status="ready",
        completeness="complete",
        created_at="2026-10-22T00:00:00+00:00",
        totals=SnapshotTotals(
            documents=1,
            sections=1,
            blocks=1,
            table_blocks=0,
            embedding_requests=1,
            embedding_input_tokens=1,
        ),
    )


def _add_test_suite(
    database: EvaluationDatabase,
    *,
    n: int = 1,
) -> str:
    now = datetime.now(UTC)
    with database.session() as session, session.begin():
        session.add(EvalCaseRow(case_id="test-case"))
        session.flush()
        session.add(
            EvalCaseRevisionRow(
                revision_id="cr_test_case_r1",
                case_id="test-case",
                revision_number=1,
                category="test",
                question="测试问题",
                target_bindings_json="[]",
                distractor_metadata_json="{}",
                n=n,
                k=max(1, n - 1),
                execution_policy_json='{"latency_class":"simple"}',
                source_ref="tests#test-case",
                content_hash="c" * 64,
                review_status="active",
                reviewer="test",
                reviewed_at=now,
            )
        )
        session.flush()
        session.add(EvalSuiteRow(suite_id="test-suite"))
        session.flush()
        session.add(
            EvalSuiteRevisionRow(
                revision_id="sr_test_suite_r1",
                suite_id="test-suite",
                revision_number=1,
                scope_id="test",
                review_status="active",
                reviewer="test",
                reviewed_at=now,
                content_hash="d" * 64,
            )
        )
        session.flush()
        session.add(
            EvalSuiteRevisionCaseRow(
                suite_revision_id="sr_test_suite_r1",
                case_revision_id="cr_test_case_r1",
                ordinal=1,
            )
        )
    return "sr_test_suite_r1"


def _create_plan(
    database: EvaluationDatabase,
    repository: EvaluationRepository,
    suite_revision_id: str,
    *,
    concurrency: int = 2,
) -> str:
    planner = EvaluationPlanner(repository, _StaticCollectionService(_manifest()))
    plan = planner.preflight(
        PreflightRequest(
            suite_revision_id=suite_revision_id,
            collection_snapshot_id="s_0123456789abcdef",
            concurrency=concurrency,
            total_token_budget=10_000_000,
        ),
        runtime_settings=RuntimeSettingsResponse(),
    )
    assert plan.status == "planned"
    return plan.plan_id


async def _wait_for_status(
    repository: EvaluationRepository,
    run_id: str,
    expected: set[str],
) -> str:
    for _ in range(200):
        status = repository.get_run(run_id).status
        if status in expected:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} did not reach {expected}")


def test_design_case_seed_is_complete_and_deterministic() -> None:
    first = load_design_case_seeds()
    second = load_design_case_seeds()

    assert len(first) == 18
    assert sum(len(case.criteria) for case in first) == 70
    assert [case.content_hash for case in first] == [case.content_hash for case in second]
    assert all(criterion.revision_id for case in first for criterion in case.criteria)
    assert {criterion.rule_type for case in first for criterion in case.criteria} == {
        "R",
        "X",
    }
    c1 = next(case for case in first if case.case_id == "c1-001")
    assert c1.criteria[1].rule_text.endswith("没有说明其所属类别，不算满足。")
    assert not any(
        criterion.rule_text.endswith(" 边界") for case in first for criterion in case.criteria
    )


def test_schema_v1_upgrades_plan_facts_without_rewriting_them(tmp_path: Path) -> None:
    database_path = tmp_path / "db" / "eval.db"
    database_path.parent.mkdir(parents=True)
    database_url = f"sqlite:///{database_path.as_posix()}"
    command.upgrade(_migration_config(database_url), "0001")
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO eval_suites (suite_id, created_at) VALUES (?, ?)",
            ("suite_old", "2026-10-23T00:00:00+00:00"),
        )
        for revision_id, revision_number in (("sr_old", 1), ("sr_new", 2)):
            connection.execute(
                """
                INSERT INTO eval_suite_revisions (
                    revision_id, suite_id, revision_number, scope_id,
                    review_status, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    "suite_old",
                    revision_number,
                    "probe",
                    "active",
                    "a" * 64,
                    "2026-10-23T00:00:00+00:00",
                ),
            )
        connection.execute(
            """
            INSERT INTO eval_execution_plans (
                plan_id, suite_revision_id, collection_snapshot_id, run_type,
                agent_version_set_json, model_config_json, run_policy_json,
                experiment_variable_json, preflight_result_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "plan_old",
                "sr_old",
                "s_0123456789abcdef",
                "quality",
                "{}",
                "{}",
                '{"concurrency":1}',
                "{}",
                "[]",
                "planned",
                "2026-10-23T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO eval_plan_cases (
                plan_id, case_revision_id, ordinal, n, k,
                resolved_targets_json, execution_policy_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("plan_old", "cr_old_r1", 1, 10, 9, "[]", "{}"),
        )
        connection.execute(
            """
            CREATE TRIGGER eval_plan_case_content_immutable
            BEFORE UPDATE ON eval_plan_cases
            BEGIN
                SELECT RAISE(ABORT, 'execution plan cases are immutable');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    database = EvaluationDatabase(tmp_path)
    connection = sqlite3.connect(database_path)
    try:
        source_n, source_k = connection.execute(
            "SELECT source_n, source_k FROM eval_plan_cases WHERE plan_id = 'plan_old'"
        ).fetchone()
        mode = connection.execute(
            "SELECT mode FROM eval_execution_plans WHERE plan_id = 'plan_old'"
        ).fetchone()[0]
        suite_statuses = dict(
            connection.execute(
                """
                SELECT revision_id, review_status
                FROM eval_suite_revisions
                WHERE suite_id = 'suite_old'
                """
            ).fetchall()
        )
    finally:
        connection.close()
    assert database.schema_version() == 7
    assert (source_n, source_k) == (10, 9)
    assert mode == "formal"
    assert suite_statuses == {"sr_old": "retired", "sr_new": "active"}


def test_revision_content_is_immutable_and_suite_requires_active_case(
    tmp_path: Path,
) -> None:
    database = EvaluationDatabase(tmp_path)
    EvaluationRepository(database)

    with (
        pytest.raises(IntegrityError, match="case revision content is immutable"),
        database.session() as session,
        session.begin(),
    ):
        session.execute(
            update(EvalCaseRevisionRow)
            .where(EvalCaseRevisionRow.revision_id == "cr_c1_001_r1")
            .values(question="changed")
        )

    with (
        pytest.raises(IntegrityError, match="suite may only reference active"),
        database.session() as session,
        session.begin(),
    ):
        session.add(
            EvalCaseRow(case_id="draft-case"),
        )
        session.flush()
        session.add(
            EvalCaseRevisionRow(
                revision_id="cr_draft_case_r1",
                case_id="draft-case",
                revision_number=1,
                category="test",
                question="draft",
                target_bindings_json="[]",
                distractor_metadata_json="{}",
                n=1,
                k=1,
                execution_policy_json="{}",
                source_ref="test",
                content_hash="e" * 64,
                review_status="draft",
            )
        )
        session.flush()
        session.add(
            EvalSuiteRevisionCaseRow(
                suite_revision_id="sr_design03_r1",
                case_revision_id="cr_draft_case_r1",
                ordinal=99,
            )
        )


def test_repository_seed_is_idempotent(tmp_path: Path) -> None:
    database = EvaluationDatabase(tmp_path)
    repository = EvaluationRepository(database)
    repository.seed_design_cases()

    suite = repository.get_suite_revision("sr_design03_r1")
    assert suite.case_count == 18
    assert sum(len(case.criteria) for case in suite.cases) == 70


def test_existing_seed_revision_is_not_extended_by_design_changes(tmp_path: Path) -> None:
    database = EvaluationDatabase(tmp_path)
    repository = EvaluationRepository(database)
    seed = next(item for item in load_design_case_seeds() if item.case_id == "cf-003")
    changed_seed = seed.model_copy(
        update={
            "criteria": [
                *seed.criteria,
                CriterionSeed(
                    criterion_id="cf-003-r99",
                    revision_id="ccr_cf_003_r99_r1",
                    rule_type="R",
                    ordinal=99,
                    title="不应写入历史",
                    rule_text="新设计内容不应扩展旧 revision。",
                ),
            ]
        }
    )
    with database.session() as session, session.begin():
        repository._seed_case(session, changed_seed, datetime.now(UTC))

    historical = repository.get_case_revisions([seed.revision_id])[0]
    assert [criterion.criterion_id for criterion in historical.criteria] == [
        criterion.criterion_id for criterion in seed.criteria
    ]


def test_minimal_judge_seed_routes_semantic_and_truncation_criteria(
    tmp_path: Path,
) -> None:
    database = EvaluationDatabase(tmp_path)
    repository = EvaluationRepository(database)
    suites = repository.list_suite_revisions()
    active_suite = next(
        suite
        for suite in suites
        if suite.suite_id == "design03" and suite.review_status == "active"
    )

    assert active_suite.revision_id == "sr_design03_minimal_judge_v2"
    assert active_suite.case_count == 18
    c1 = next(case for case in active_suite.cases if case.case_id == "c1-001")
    cf3 = next(case for case in active_suite.cases if case.case_id == "cf-003")
    assert all(criterion.evaluator == "llm_judge" for criterion in c1.criteria)
    assert len(cf3.criteria) == 2
    assert [criterion.title for criterion in cf3.criteria] == ["截断事实", "截断声明"]
    assert cf3.criteria[0].evaluator == "checker"
    assert cf3.criteria[0].evaluator_ref == "truncation_declared"
    assert cf3.criteria[1].evaluator == "llm_judge"


def test_runner_pauses_resumes_and_seals_at_attempt_boundary(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path)
        repository = EvaluationRepository(database)
        suite_revision_id = _add_test_suite(database, n=2)
        plan_id = _create_plan(
            database,
            repository,
            suite_revision_id,
            concurrency=1,
        )
        execution = _FakeExecutionService(block_first=True)
        runner = EvaluationRunner(repository, execution)  # type: ignore[arg-type]
        try:
            started = await runner.start(plan_id)
            await asyncio.wait_for(execution.entered.wait(), timeout=1)
            await runner.pause(started.run_id)
            execution.release.set()
            assert await _wait_for_status(repository, started.run_id, {"paused"}) == "paused"
            paused = repository.get_run(started.run_id)
            assert paused.plan_mode == "formal"
            assert paused.suite_id == "test-suite"
            assert paused.suite_name == "test-suite"
            assert paused.totals["succeeded"] == 1
            assert paused.totals["attempts"] == 1

            await runner.resume(started.run_id)
            assert (
                await _wait_for_status(
                    repository,
                    started.run_id,
                    {"sealed_complete"},
                )
                == "sealed_complete"
            )
            finished = repository.get_run(started.run_id)
            assert finished.totals["succeeded"] == 2
            assert all(call[1] == "s_0123456789abcdef" for call in execution.calls)
            assert all(settings.trace_mode == "full" for settings in execution.settings_overrides)
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_runner_reserves_hard_budget_before_starting_attempt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path)
        repository = EvaluationRepository(database)
        suite_revision_id = _add_test_suite(database)
        planner = EvaluationPlanner(
            repository,
            _StaticCollectionService(_manifest()),
        )
        plan = planner.preflight(
            PreflightRequest(
                suite_revision_id=suite_revision_id,
                collection_snapshot_id="s_0123456789abcdef",
                total_token_budget=1,
            ),
            runtime_settings=RuntimeSettingsResponse(),
        )
        execution = _FakeExecutionService()
        runner = EvaluationRunner(repository, execution)  # type: ignore[arg-type]
        try:
            run = await runner.start(plan.plan_id)
            assert await _wait_for_status(repository, run.run_id, {"paused"}) == "paused"
            paused = repository.get_run(run.run_id)
            assert paused.pause_reason == "total_budget_exhausted"
            assert paused.totals["attempts"] == 0
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_runner_uses_global_attempt_concurrency(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path)
        repository = EvaluationRepository(database)
        suite_revision_id = _add_test_suite(database, n=4)
        plan_id = _create_plan(
            database,
            repository,
            suite_revision_id,
            concurrency=3,
        )
        execution = _FakeExecutionService()
        runner = EvaluationRunner(repository, execution)  # type: ignore[arg-type]
        try:
            run = await runner.start(plan_id)
            assert (
                await _wait_for_status(
                    repository,
                    run.run_id,
                    {"sealed_complete"},
                )
                == "sealed_complete"
            )
            assert execution.max_active == 3
            finished = repository.get_run(run.run_id)
            assert finished.totals["attempts"] == 4
            assert finished.totals["valid_samples"] == 4
            assert finished.totals["infrastructure_failures"] == 0
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_runner_waits_for_inflight_attempts_before_sealing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path)
        repository = EvaluationRepository(database)
        suite_revision_id = _add_test_suite(database, n=10)
        plan_id = _create_plan(
            database,
            repository,
            suite_revision_id,
            concurrency=8,
        )
        execution = _FakeExecutionService(
            attempt_delays=[0.001, 0.001, 0.001, *([0.05] * 20)],
        )
        runner = EvaluationRunner(repository, execution)  # type: ignore[arg-type]
        try:
            run = await runner.start(plan_id)
            assert (
                await _wait_for_status(repository, run.run_id, {"sealed_complete"})
                == "sealed_complete"
            )
            finished = repository.get_run(run.run_id)
            assert finished.totals["attempts"] == 10
            assert finished.totals["valid_samples"] == 10
            assert finished.totals["infrastructure_failures"] == 0
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("execution_status", "error_code", "expected"),
    [
        ("succeeded", None, "product"),
        ("partial", None, "product"),
        ("failed", "request_budget_exceeded", "product"),
        ("failed", "chat_upstream_unavailable", "infrastructure"),
        ("failed", "embedding_upstream_unavailable", "infrastructure"),
        ("failed", "evaluation_attempt_timeout", "infrastructure"),
        ("failed", "agent_protocol_loop", "product"),
        ("canceled", "canceled_by_user", "canceled"),
        ("outcome_unknown", "evaluation_process_restarted", "unknown"),
    ],
)
def test_attempt_outcome_classification(
    execution_status: str,
    error_code: str | None,
    expected: str,
) -> None:
    assert classify_attempt_outcome(execution_status, error_code) == expected


def test_product_failure_counts_as_sample_without_replacement(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path)
        repository = EvaluationRepository(database)
        suite_revision_id = _add_test_suite(database)
        plan_id = _create_plan(
            database,
            repository,
            suite_revision_id,
            concurrency=1,
        )
        execution = _FakeExecutionService(fail_first_with="request_budget_exceeded")
        runner = EvaluationRunner(repository, execution)  # type: ignore[arg-type]
        try:
            run = await runner.start(plan_id)
            assert (
                await _wait_for_status(repository, run.run_id, {"sealed_complete"})
                == "sealed_complete"
            )
            finished = repository.get_run(run.run_id)
            assert finished.totals["attempts"] == 1
            assert finished.totals["valid_samples"] == 1
            assert finished.attempts[0].outcome_class == "product"
            assert finished.attempts[0].execution_status == "failed"
            assert finished.attempts[0].trace_status == "complete"
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_infrastructure_failure_is_replaced_without_counting_n(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path)
        repository = EvaluationRepository(database)
        suite_revision_id = _add_test_suite(database)
        plan_id = _create_plan(
            database,
            repository,
            suite_revision_id,
            concurrency=1,
        )
        execution = _FakeExecutionService(
            fail_first_with="chat_upstream_unavailable",
        )
        runner = EvaluationRunner(repository, execution)  # type: ignore[arg-type]
        try:
            run = await runner.start(plan_id)
            assert (
                await _wait_for_status(repository, run.run_id, {"sealed_complete"})
                == "sealed_complete"
            )
            finished = repository.get_run(run.run_id)
            assert finished.totals["attempts"] == 2
            assert finished.totals["valid_samples"] == 1
            assert finished.totals["infrastructure_failures"] == 1
            assert finished.attempts[0].outcome_class == "infrastructure"
            assert finished.attempts[1].outcome_class == "product"
            assert (
                finished.attempts[1].replacement_for_attempt_id == finished.attempts[0].attempt_id
            )
        finally:
            await runner.shutdown()

    asyncio.run(scenario())


def test_restart_marks_running_attempt_unknown_without_resend(tmp_path: Path) -> None:
    database = EvaluationDatabase(tmp_path)
    repository = EvaluationRepository(database)
    suite_revision_id = _add_test_suite(database)
    plan_id = _create_plan(database, repository, suite_revision_id)
    run = repository.create_run(plan_id)
    repository.set_run_status(run.run_id, "running", started=True)
    repository.start_attempt(
        run_id=run.run_id,
        case_revision_id="cr_test_case_r1",
    )

    restarted = EvaluationRepository(EvaluationDatabase(tmp_path))
    recovered = restarted.get_run(run.run_id)

    assert recovered.status == "paused"
    assert recovered.pause_reason == "evaluation_process_restarted"
    assert recovered.attempts[0].execution_status == "outcome_unknown"
    assert recovered.attempts[0].outcome_class == "unknown"
    assert recovered.attempts[0].error_code == "evaluation_process_restarted"


def test_case_draft_is_mutable_until_atomic_reviewed_publication(
    tmp_path: Path,
) -> None:
    database = EvaluationDatabase(tmp_path)
    repository = EvaluationRepository(database)
    draft = repository.create_case_draft("c1-001", updated_by="author")
    before_revisions = next(
        item for item in repository.list_case_catalog() if item.case_id == "c1-001"
    ).revisions

    changed_criterion = draft.criteria[0].model_copy(
        update={"title": "修改后的结论", "rule_text": "修改后的判定标准"}
    )
    unchanged_criteria = [
        CaseDraftCriterionView.model_validate(item.model_dump()) for item in draft.criteria[1:]
    ]
    request = SaveCaseDraftRequest(
        category=draft.category,
        question="修改后的问题",
        target_bindings=draft.target_bindings,
        distractor_metadata=draft.distractor_metadata,
        n=5,
        k=4,
        execution_policy=draft.execution_policy,
        change_note="调整问题与 R1",
        criteria=[changed_criterion, *unchanged_criteria],
        updated_by="author",
    )
    repository.save_case_draft("c1-001", request)
    repository.save_case_draft(
        "c1-001",
        request.model_copy(update={"question": "再次修改后的问题"}),
    )
    assert len(
        next(item for item in repository.list_case_catalog() if item.case_id == "c1-001").revisions
    ) == len(before_revisions)

    published = repository.publish_case_draft("c1-001", reviewer="reviewer-a")
    assert published.review_status == "reviewed"
    assert published.revision_number == 6
    assert published.question == "再次修改后的问题"
    assert published.criteria[0].criterion_id == draft.criteria[0].criterion_id
    assert published.criteria[0].revision_id != draft.criteria[0].base_revision_id
    assert published.criteria[1].revision_id == draft.criteria[1].base_revision_id
    assert (
        next(
            item for item in repository.list_case_catalog() if item.case_id == "c1-001"
        ).active_revision_id
        == "cr_c1_001_minimal_judge_v2"
    )

    activated = repository.review_case_revision(
        published.revision_id,
        review_status="active",
        reviewer="reviewer-a",
    )
    assert activated.review_status == "active"
    assert (
        next(item for item in repository.list_case_catalog() if item.case_id == "c1-001")
        .revisions[-1]
        .review_status
        == "retired"
    )


def test_suite_subset_publication_and_stale_after_case_upgrade(
    tmp_path: Path,
) -> None:
    database = EvaluationDatabase(tmp_path)
    repository = EvaluationRepository(database)
    repository.create_suite_draft("design03", updated_by="author")
    suite_draft = repository.save_suite_draft(
        "design03",
        SaveSuiteDraftRequest(
            name="最小回归",
            description="只跑前两条",
            scope_id="smoke",
            case_revision_ids=[
                "cr_c1_001_minimal_judge_v2",
                "cr_c2_001_minimal_judge_v2",
            ],
            change_note="建立小套件",
            updated_by="author",
        ),
    )
    assert len(suite_draft.cases) == 2

    suite = repository.publish_suite_draft("design03", reviewer="reviewer-a")
    assert suite.revision_number == 6
    assert suite.case_count == 2
    assert suite.stale is False
    assert {
        item.revision_id
        for item in repository.list_suite_revisions()
        if item.suite_id == "design03"
    } >= {"sr_design03_r1", suite.revision_id}

    case_draft = repository.create_case_draft("c1-001", updated_by="author")
    repository.publish_case_draft("c1-001", reviewer="reviewer-a")
    updated_case = next(
        item for item in repository.list_case_catalog() if item.case_id == "c1-001"
    ).latest_revision_id
    assert updated_case is not None
    repository.review_case_revision(
        updated_case,
        review_status="active",
        reviewer="reviewer-a",
    )

    stale = repository.get_suite_revision(suite.revision_id)
    assert stale.stale is True
    assert any("case_revision_not_active:c1-001" in item for item in stale.stale_reasons)
    assert case_draft.base_revision_id == "cr_c1_001_minimal_judge_v2"
    invalid = EvaluationPlanner(
        repository,
        _StaticCollectionService(_manifest()),
    ).preflight(
        PreflightRequest(
            suite_revision_id=suite.revision_id,
            collection_snapshot_id="s_0123456789abcdef",
            total_token_budget=10_000_000,
        ),
        runtime_settings=RuntimeSettingsResponse(),
    )
    assert invalid.status == "invalid_plan"
    suite_check = next(item for item in invalid.checks if item.code == "suite_active")
    assert suite_check.status == "failed"


def test_quick_plan_is_exploratory_and_only_overrides_n(tmp_path: Path) -> None:
    database = EvaluationDatabase(tmp_path)
    repository = EvaluationRepository(database)
    suite_revision_id = _add_test_suite(database, n=4)
    suite = repository.get_suite_revision(suite_revision_id)
    case_revision = suite.cases[0]
    planner = EvaluationPlanner(repository, _StaticCollectionService(_manifest()))

    plan = planner.quick_preflight(
        QuickPlanRequest(
            case_revision_ids=[case_revision.revision_id],
            collection_snapshot_id="s_0123456789abcdef",
            n_override=1,
            total_token_budget=10_000_000,
        ),
        runtime_settings=RuntimeSettingsResponse(),
    )

    assert plan.status == "planned"
    assert plan.mode == "exploratory"
    assert plan.suite_revision_id is None
    assert plan.cases[0].n == 1
    assert plan.cases[0].source_n == 4
    assert plan.cases[0].k == 3
    assert plan.run_policy["n_override"] == 1
    quick_run = repository.create_run(plan.plan_id)
    assert quick_run.plan_mode == "exploratory"
    assert quick_run.suite_id is None
    assert quick_run.suite_name is None
