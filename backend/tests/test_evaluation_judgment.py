from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent.citations import CitationAnnotation, CitationAnnotationArtifact
from app.evaluation.citation_prompt import (
    CITATION_CLAIM_ROLES_PROMPT_VERSION,
    CITATION_CLAIM_ROLES_SYSTEM_PROMPT,
    CITATION_EVIDENCE_ROLES_PROMPT_VERSION,
    CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT,
    CITATION_RELATION_PROMPT_VERSION,
    CITATION_RELATION_SYSTEM_PROMPT,
    CITATION_SUPPORT_PROMPT_VERSION,
    CITATION_SUPPORT_SYSTEM_PROMPT,
    CitationClaimRoleInput,
    CitationContextInput,
    CitationEvidenceInput,
    CitationEvidenceRoleInput,
    CitationOccurrenceInput,
    CitationRelationComparisonInput,
    CitationRoleAssertionInput,
    CitationSupportInput,
)
from app.evaluation.database import EvaluationDatabase
from app.evaluation.evidence import split_citation_contexts
from app.evaluation.judgment import (
    CitationRelationCheckOutput,
    EvaluationJudgmentService,
    _has_substantive_unmatched_roles,
    _normalize_claim_role,
    _numeric_role_conflict,
    _parse_citation_output,
    _parse_relation_review,
)
from app.evaluation.models import (
    CaseDraftCriterionView,
    CheckReviewRequest,
    CitationReviewItem,
    JudgeOverrideRequest,
    SaveCaseDraftRequest,
    StartJudgmentRequest,
)
from app.evaluation.repository import EvaluationRepository
from app.evaluation.storage_models import (
    EvalAttemptRow,
    EvalEvaluationRevisionRow,
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
        citation_annotation_artifact: CitationAnnotationArtifact | None = None,
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
        artifacts = [artifact]
        artifact_paths = {artifact.artifact_id: self._artifact_path}
        if citation_annotation_artifact is not None:
            annotation_path = root / "annotations.json"
            annotation_path.write_text(
                citation_annotation_artifact.model_dump_json(),
                encoding="utf-8",
            )
            annotation_artifact = TraceArtifactRecord(
                artifact_id="d" * 64,
                kind="citations",
                mime_type="application/json",
                size_bytes=annotation_path.stat().st_size,
                content_hash=hashlib.sha256(annotation_path.read_bytes()).hexdigest(),
                state="stored",
                relative_path="artifacts/annotations.json",
            )
            artifacts.append(annotation_artifact)
            artifact_paths[annotation_artifact.artifact_id] = annotation_path
        self._artifact_paths = artifact_paths
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
                artifacts=artifacts,
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
        assert artifact_id in self._artifact_paths
        return self._artifact_paths[artifact_id]


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
        invalid_first_citation_output: bool = False,
        invalid_first_citation_quote: bool = False,
        relation_review_present: bool = False,
        relation_review_direction_preserved: bool | None = True,
    ) -> None:
        self._profile = FakeChatProvider(["unused"]).profile
        self._first_batch_verdicts = first_batch_verdicts
        self._omit_last_occurrence = omit_last_occurrence
        self._invalid_first_citation_output = invalid_first_citation_output
        self._invalid_first_citation_quote = invalid_first_citation_quote
        self._relation_review_present = relation_review_present
        self._relation_review_direction_preserved = relation_review_direction_preserved
        self.requests: list[tuple[ChatMessage, ...]] = []
        self.max_output_tokens: list[int | None] = []
        self.citation_request_count = 0
        self.claim_role_request_count = 0
        self.evidence_role_request_count = 0
        self.relation_review_request_count = 0

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
        payload = next(
            json.loads(message.content)
            for message in reversed(messages)
            if message.role == "user" and message.content.startswith("{")
        )
        if messages[0].content == CITATION_CLAIM_ROLES_SYSTEM_PROMPT:
            self.claim_role_request_count += 1
            content = json.dumps(
                {
                    "claims": [
                        {
                            "occurrence_id": item["occurrence_id"],
                            "relation_present": self._relation_review_present,
                            "relation_type": (
                                "benefit_cost" if self._relation_review_present else None
                            ),
                            "claim_objective": (
                                "比较收益与代价" if self._relation_review_present else None
                            ),
                            "claim_roles": (
                                [
                                    {"fact": "风险减少", "role": "benefit"},
                                    {"fact": "摩擦增加", "role": "cost"},
                                ]
                                if self._relation_review_present
                                else []
                            ),
                        }
                        for item in payload["occurrences"]
                    ]
                },
                ensure_ascii=False,
            )
        elif messages[0].content == CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT:
            self.evidence_role_request_count += 1
            content = json.dumps(
                {
                    "evidence": [
                        {
                            "occurrence_id": item["occurrence_id"],
                            "source_objective": "比较收益与代价",
                            "evidence_roles": [
                                {"fact": "风险减少", "role": "benefit"},
                                {"fact": "摩擦增加", "role": "cost"},
                            ],
                            "reason": "测试 evidence 角色",
                        }
                        for item in payload["occurrences"]
                    ]
                },
                ensure_ascii=False,
            )
        elif messages[0].content == CITATION_RELATION_SYSTEM_PROMPT:
            self.relation_review_request_count += 1
            content = json.dumps(
                {
                    "relations": [
                        {
                            "occurrence_id": item["occurrence_id"],
                            "direction_preserved": self._relation_review_direction_preserved,
                            "explicit_conflict": (
                                self._relation_review_direction_preserved is False
                            ),
                            "unmatched_claim_roles": (
                                ["测试未匹配角色"]
                                if self._relation_review_direction_preserved is None
                                else []
                            ),
                            "reason": "测试关系方向结论",
                        }
                        for item in payload["claims"]
                    ]
                },
                ensure_ascii=False,
            )
        elif "occurrences" in payload:
            first_request = self.citation_request_count == 0
            self.citation_request_count += 1
            if first_request and self._invalid_first_citation_output:
                content = '{"citations":['
                return ChatCompletionResult(
                    content=content,
                    request_id=f"fake-citation-{self.citation_request_count:04d}",
                    actual_model_id=self.profile.model_id,
                    finish_reason="stop",
                    input_tokens=sum(max(1, len(message.content) // 4) for message in messages),
                    output_tokens=max(1, len(content) // 4),
                )
            occurrence_by_id = {item["occurrence_id"]: item for item in payload["occurrences"]}
            evidence_by_id = {item["block_id"]: item["text"] for item in payload["citations"]}
            verdicts = (
                self._first_batch_verdicts
                if first_request and self._first_batch_verdicts is not None
                else ["supported"] * len(occurrence_by_id)
            )
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
                            "evidence_quotes": [
                                (
                                    "不存在的证据原文"
                                    if first_request
                                    and self._invalid_first_citation_quote
                                    and index == 0
                                    else evidence_by_id[occurrence["block_ids"][-1]][:12]
                                )
                            ],
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


def _single_claim_annotation(
    answer: str,
    *,
    block_ids: list[str],
) -> CitationAnnotationArtifact:
    """为整段测试答案构造一个冻结 claim annotation。"""

    return CitationAnnotationArtifact(
        status="complete",
        model_id="fake-annotation",
        span_count=1,
        evidence_count=len(block_ids),
        annotations=[
            CitationAnnotation(
                annotation_id="annotation_0001",
                span_id="span_0001",
                kind="sentence",
                start_index=0,
                end_index=len(answer),
                text=answer,
                block_ids=block_ids,
            )
        ],
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


def test_citation_prompt_uses_semantic_equivalence_and_isolates_occurrences() -> None:
    assert CITATION_SUPPORT_PROMPT_VERSION == "citation-rubric-v11"
    assert "中文近义转述不要求逐词对应" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "该问题本身合理／有道理／是对的" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "不能只因 claim 使用了“正确／对”等概括词就判定增强" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "删除必要限制" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "context_fragments" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "不能替 focus_text 获得支持" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "relation_check" not in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "不得使用同批其他 occurrence" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "保留相同的关系与角色" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "不得只因为数值本身看似有利" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "该标签属于 claim" in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "c1-001" not in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "c2-001" not in CITATION_SUPPORT_SYSTEM_PROMPT
    assert "good question" not in CITATION_SUPPORT_SYSTEM_PROMPT
    for case_specific_token in ("83%", "93%", "17%", "0.4%"):
        assert case_specific_token not in CITATION_SUPPORT_SYSTEM_PROMPT


def test_relation_prompt_uses_objective_frame_and_allows_unknown_direction() -> None:
    assert CITATION_CLAIM_ROLES_PROMPT_VERSION == "citation-claim-roles-v1"
    assert "不会收到 citations" in CITATION_CLAIM_ROLES_SYSTEM_PROMPT
    assert "claim_roles 必须逐项列出" in CITATION_CLAIM_ROLES_SYSTEM_PROMPT
    assert CITATION_EVIDENCE_ROLES_PROMPT_VERSION == "citation-evidence-roles-v3"
    assert "不会收到 contexts 或 claim" in CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT
    assert "把 A 记录为 cause" in CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT
    assert "evidence_roles 返回空数组" in CITATION_EVIDENCE_ROLES_SYSTEM_PROMPT
    assert CITATION_RELATION_PROMPT_VERSION == "citation-relation-v8"
    assert "claim_roles" in CITATION_RELATION_SYSTEM_PROMPT
    assert "evidence_roles" in CITATION_RELATION_SYSTEM_PROMPT
    assert "不得重新解释原始文本" in CITATION_RELATION_SYSTEM_PROMPT
    assert "direction_preserved=null" in CITATION_RELATION_SYSTEM_PROMPT
    assert "所有显式角色都必须覆盖" in CITATION_RELATION_SYSTEM_PROMPT
    assert "证据没有列出该角色" in CITATION_RELATION_SYSTEM_PROMPT
    assert "explicit_conflict" in CITATION_RELATION_SYSTEM_PROMPT


def test_numeric_role_guard_rejects_same_metric_in_opposite_roles() -> None:
    conflict = _numeric_role_conflict(
        claim_roles=[
            CitationRoleAssertionInput(
                fact="约 0.4% 的良性命令被误拦",
                role="cost",
            )
        ],
        evidence_roles=[
            CitationRoleAssertionInput(
                fact="降低摩擦，约 0.4% 的良性命令被拦",
                role="benefit",
            ),
            CitationRoleAssertionInput(
                fact="约 17% 的风险行为漏过",
                role="cost",
            ),
        ],
    )

    assert conflict is not None
    assert "0.4%" in conflict
    assert (
        _numeric_role_conflict(
            claim_roles=[
                CitationRoleAssertionInput(
                    fact="约 0.4% 的良性命令被误拦",
                    role="benefit",
                )
            ],
            evidence_roles=[
                CitationRoleAssertionInput(
                    fact="降低摩擦，约 0.4% 的良性命令被拦",
                    role="benefit",
                )
            ],
        )
        is None
    )


def test_claim_role_normalization_drops_low_information_other_relation() -> None:
    normalized = _normalize_claim_role(
        CitationClaimRoleInput(
            occurrence_id="citation_0001",
            relation_present=True,
            relation_type="other",
            claim_objective=None,
            claim_roles=[
                CitationRoleAssertionInput(
                    fact="The system uses a classifier",
                    role="other",
                )
            ],
        )
    )

    assert normalized.relation_present is False
    assert normalized.relation_type is None
    assert normalized.claim_roles == []


def test_unmatched_other_role_does_not_block_matched_benefit_cost() -> None:
    claim_roles = [
        CitationRoleAssertionInput(fact="system mechanism", role="other"),
        CitationRoleAssertionInput(fact="low friction", role="benefit"),
        CitationRoleAssertionInput(fact="missed risk", role="cost"),
    ]

    assert (
        _has_substantive_unmatched_roles(
            claim_roles,
            ["system mechanism"],
        )
        is False
    )
    assert _has_substantive_unmatched_roles(claim_roles, ["missed risk"]) is True


def test_relation_check_allows_evidence_without_explicit_objective() -> None:
    relation = CitationRelationCheckOutput(
        relation_present=True,
        relation_type="cause_effect",
        source_objective=None,
        claim_objective=None,
        evidence_roles=[CitationRoleAssertionInput(fact="risk increases", role="effect")],
        claim_roles=[CitationRoleAssertionInput(fact="risk increases", role="effect")],
        direction_preserved=True,
        explicit_conflict=False,
    )

    assert relation.source_objective is None
    assert relation.direction_preserved is True


def test_inconsistent_relation_output_is_narrowed_to_unknown() -> None:
    packet = CitationRelationComparisonInput(
        claims=[
            CitationClaimRoleInput(
                occurrence_id="citation_0001",
                relation_present=True,
                relation_type="cause_effect",
                claim_roles=[
                    CitationRoleAssertionInput(fact="change", role="cause"),
                    CitationRoleAssertionInput(fact="risk", role="effect"),
                ],
            )
        ],
        evidence=[
            CitationEvidenceRoleInput(
                occurrence_id="citation_0001",
                evidence_roles=[
                    CitationRoleAssertionInput(fact="risk", role="effect"),
                ],
                reason="same role",
            )
        ],
        occurrences=[
            CitationOccurrenceInput(
                occurrence_id="citation_0001",
                block_ids=["block_1"],
                context_id="context_1",
            )
        ],
    )
    result = ChatCompletionResult(
        content=json.dumps(
            {
                "relations": [
                    {
                        "occurrence_id": "citation_0001",
                        "direction_preserved": False,
                        "explicit_conflict": False,
                        "unmatched_claim_roles": [
                            {"fact": "risk", "role": "effect"},
                        ],
                        "reason": "inconsistent test output",
                    }
                ]
            }
        ),
        request_id="relation-normalize",
        actual_model_id="fake",
        finish_reason="stop",
        input_tokens=1,
        output_tokens=1,
    )

    parsed = _parse_relation_review(result, packet)

    assert parsed["citation_0001"].direction_preserved is None
    assert "判官未给出明确冲突依据" in parsed["citation_0001"].unmatched_claim_roles


def test_citation_output_allows_relation_free_main_result() -> None:
    packet = CitationSupportInput(
        contexts=[
            CitationContextInput(
                context_id="context_relation",
                kind="claim",
                text="方案以减少摩擦为代价，换来风险漏过。",
            )
        ],
        citations=[
            CitationEvidenceInput(
                block_id="block_relation",
                doc_title="calibration",
                section_path=[],
                text="收益是减少摩擦，代价是漏过风险。",
            )
        ],
        occurrences=[
            CitationOccurrenceInput(
                occurrence_id="citation_relation",
                block_ids=["block_relation"],
                context_id="context_relation",
            )
        ],
    )
    result = ChatCompletionResult(
        content=json.dumps(
            {
                "citations": [
                    {
                        "occurrence_id": "citation_relation",
                        "verdict": "supported",
                        "reason": "测试缺少关系字段",
                        "evidence_quotes": ["收益是减少摩擦，代价是漏过风险。"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        request_id="relation-missing",
        actual_model_id="fake",
        finish_reason="stop",
        input_tokens=1,
        output_tokens=1,
    )

    parsed = _parse_citation_output(result, packet)
    assert parsed.citations[0].relation_check is None


def test_human_override_appends_judgment_and_switches_effective_binding(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        answer = "MCP 是外部内容边界。【来源：block_1】"
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer=answer,
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

        assert updated.judge_usage == initial.judge_usage
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
        assert final.judge_usage == initial.judge_usage
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
                                "verdict": "unsupported",
                                "reason": "模型怀疑引用原文不支持答案主张。",
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
        initial_attempt = next(
            item for item in initial.attempt_aggregates if item.attempt_id == attempt_id
        )
        initial_case = next(item for item in initial.case_aggregates if item.case_id == "c1-001")
        assert initial.status == "complete"
        assert original_support.result == "pending"
        assert initial_attempt.result == "pass"
        assert initial_attempt.check_quality_failures == []
        assert initial_attempt.review_pending is False
        assert initial_attempt.check_failures == []
        assert initial_attempt.citation_support_quality is not None
        assert initial_attempt.citation_support_quality.status == "insufficient_sample"
        assert initial_attempt.citation_support_quality.support_rate == 0.0
        assert initial_case.result == "pending"
        assert initial_case.quality_failure_count == 0
        assert initial_case.review_pending_count == 0
        assert initial_case.redline_violation_count == 0
        assert all("citation_support" not in reason for reason in initial_case.reasons)

        reviewed = service.review_llm_check(
            initial.revision_id,
            CheckReviewRequest(
                attempt_id=attempt_id,
                checker_id="citation_support",
                claims=[
                    CitationReviewItem(
                        occurrence_id="citation_0001",
                        block_ids=["block_1"],
                        verdict="unsupported",
                        reason="人工确认证据原文没有支持该主张。",
                    )
                ],
                reason="复核引用支持。",
                reviewer="reviewer-a",
            ),
        )

        assert len(reviewed.check_reviews) == 1
        attempt_aggregate = next(
            item for item in reviewed.attempt_aggregates if item.attempt_id == attempt_id
        )
        assert attempt_aggregate.result == "pass"
        assert attempt_aggregate.check_failures == []
        assert attempt_aggregate.check_quality_failures == []
        assert attempt_aggregate.review_pending is False
        assert attempt_aggregate.citation_support_quality is not None
        assert attempt_aggregate.citation_support_quality.supported_count == 0
        assert attempt_aggregate.citation_support_quality.unsupported_count == 1
        rejected = service.review_llm_check(
            initial.revision_id,
            CheckReviewRequest(
                attempt_id=attempt_id,
                checker_id="citation_support",
                claims=[
                    CitationReviewItem(
                        occurrence_id="citation_0001",
                        block_ids=["block_1"],
                        verdict="supported",
                        reason="复查确认证据原文足以支持该主张。",
                    )
                ],
                reason="驳回语义红线候选。",
                reviewer="reviewer-b",
            ),
        )
        rejected_attempt = next(
            item for item in rejected.attempt_aggregates if item.attempt_id == attempt_id
        )
        assert rejected_attempt.result == "pass"
        assert rejected.check_reviews[-1].result == "pass"
        disputed = service.review_llm_check(
            initial.revision_id,
            CheckReviewRequest(
                attempt_id=attempt_id,
                checker_id="citation_support",
                claims=[
                    CitationReviewItem(
                        occurrence_id="citation_0001",
                        block_ids=["block_1"],
                        verdict="unknown",
                        reason="证据语义仍有合理分歧，需要进一步裁决。",
                    )
                ],
                reason="保留争议。",
                reviewer="reviewer-c",
            ),
        )
        disputed_attempt = next(
            item for item in disputed.attempt_aggregates if item.attempt_id == attempt_id
        )
        assert disputed_attempt.result == "pass"
        assert disputed_attempt.citation_support_quality is not None
        assert disputed_attempt.citation_support_quality.unknown_count == 1
        assert disputed.check_reviews[-1].result == "pending"
        with pytest.raises(ValueError):
            service.review_llm_check(
                initial.revision_id,
                CheckReviewRequest(
                    attempt_id=attempt_id,
                    checker_id="citation_integrity",
                    claims=[
                        CitationReviewItem(
                            occurrence_id="citation_0001",
                            block_ids=["block_1"],
                            verdict="supported",
                            reason="不允许改程序检查。",
                        )
                    ],
                    reason="非法覆盖。",
                    reviewer="reviewer-a",
                ),
            )
        with pytest.raises(ValueError):
            service.review_llm_check(
                initial.revision_id,
                CheckReviewRequest(
                    attempt_id=attempt_id,
                    checker_id="citation_support",
                    claims=[
                        CitationReviewItem(
                            occurrence_id="citation_0001",
                            block_ids=["block_2"],
                            verdict="supported",
                            reason="错误的证据组不能被接受。",
                        )
                    ],
                    reason="验证 block 集合校验。",
                    reviewer="reviewer-a",
                ),
            )
        with pytest.raises(ValueError):
            service.review_llm_check(
                initial.revision_id,
                CheckReviewRequest(
                    attempt_id=attempt_id,
                    checker_id="citation_support",
                    claims=[
                        CitationReviewItem(
                            occurrence_id="citation_missing",
                            block_ids=["block_1"],
                            verdict="supported",
                            reason="错误的 occurrence 不能被接受。",
                        )
                    ],
                    reason="验证 claim 集合校验。",
                    reviewer="reviewer-a",
                ),
            )

    asyncio.run(scenario())


def test_human_review_persists_only_changed_claims(tmp_path: Path) -> None:
    async def scenario() -> None:
        answer = "第一句支持。第二句只作背景。"
        first_end = answer.index("第二句")
        blocks = [
            {
                "block_id": "block_1",
                "doc_id": "doc_1",
                "doc_title": "测试文档",
                "source_label": "doc",
                "section_path": ["复核"],
                "text": "第一句支持。",
            },
            {
                "block_id": "block_2",
                "doc_id": "doc_1",
                "doc_title": "测试文档",
                "source_label": "doc",
                "section_path": ["复核"],
                "text": "第二句只作背景。",
            },
        ]
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer=answer,
            trace_id="b" * 32,
            run_id="run_check_review_delta",
        )
        provider = _CitationBatchAwareProvider()
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(
                tmp_path,
                blocks=blocks,
                citation_annotation_artifact=CitationAnnotationArtifact(
                    status="complete",
                    model_id="fake-annotation",
                    span_count=2,
                    evidence_count=2,
                    annotations=[
                        CitationAnnotation(
                            annotation_id="annotation_0001",
                            span_id="span_0001",
                            kind="sentence",
                            start_index=0,
                            end_index=first_end,
                            text=answer[:first_end],
                            block_ids=["block_1"],
                        ),
                        CitationAnnotation(
                            annotation_id="annotation_0002",
                            span_id="span_0002",
                            kind="sentence",
                            start_index=first_end,
                            end_index=len(answer),
                            text=answer[first_end:],
                            block_ids=["block_2"],
                        ),
                    ],
                ),
            ),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )
        started = await service.start(
            "run_check_review_delta",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        initial = await _wait_revision(service, started.revision_id)
        support = next(check for check in initial.checks if check.checker_id == "citation_support")
        attempt_id = initial.judgments[0].attempt_id
        claims = support.payload["claim_results"]
        assert len(claims) == 2

        reviewed = service.review_llm_check(
            initial.revision_id,
            CheckReviewRequest(
                attempt_id=attempt_id,
                checker_id="citation_support",
                claims=[
                    CitationReviewItem(
                        occurrence_id=claims[0]["occurrence_id"],
                        block_ids=claims[0]["block_ids"],
                        verdict="unsupported",
                        reason="人工只改判第一个 claim。",
                        evidence_quotes=[],
                    ),
                    CitationReviewItem(
                        occurrence_id=claims[1]["occurrence_id"],
                        block_ids=claims[1]["block_ids"],
                        verdict="supported",
                        reason=claims[1]["reason"],
                        evidence_quotes=[],
                    ),
                ],
                reason="只复核有变化的 claim。",
                reviewer="reviewer-a",
            ),
        )

        stored = reviewed.check_reviews[-1].payload["claim_results"]
        assert [item["occurrence_id"] for item in stored] == [claims[0]["occurrence_id"]]
        assert stored[0]["evidence_quotes"] == claims[0]["evidence_quotes"]
        assert reviewed.check_reviews[-1].payload["reviewed_occurrence_ids"] == [
            claims[0]["occurrence_id"]
        ]

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
        attempt_id = revision.judgments[0].attempt_id
        assert citation_support.result == "pending"
        assert citation_support.error_code == "citation_support_review_required"
        assert citation_support.payload["observed_result"] == "fail"
        assert citation_support.payload["review_status"] == "pending"
        assert citation_support.payload["claim_results"][0]["review_status"] == "pending"
        assert (
            citation_support.payload["claim_results"][0]["semantic_severity"]
            == "critical_candidate"
        )
        assert citation_support.payload["semantic_candidates"] == [
            {
                "occurrence_id": "citation_0001",
                "semantic_severity": "critical_candidate",
                "review_status": "pending",
            }
        ]
        attempt_aggregate = next(
            item for item in revision.attempt_aggregates if item.attempt_id == attempt_id
        )
        case_aggregate = next(item for item in revision.case_aggregates if item.case_id == "c1-001")
        assert attempt_aggregate.result == "pass"
        assert attempt_aggregate.check_failures == []
        assert attempt_aggregate.check_quality_failures == []
        assert attempt_aggregate.review_pending is False
        assert attempt_aggregate.citation_support_quality is not None
        assert attempt_aggregate.citation_support_quality.status == "insufficient_sample"
        assert attempt_aggregate.citation_support_quality.unsupported_count == 1
        assert case_aggregate.result == "pending"
        assert case_aggregate.review_pending_count == 0
        assert case_aggregate.redline_violation_count == 0
        assert all("citation_support" not in reason for reason in case_aggregate.reasons)
        with database.session() as session, session.begin():
            stored_revision = session.get(EvalEvaluationRevisionRow, revision.revision_id)
            assert stored_revision is not None
            stored_revision.status = "incomplete"
            stored_revision.error_code = None
        assert service.get_revision(revision.revision_id).status == "complete"
        citation_request = json.loads(provider.requests[-1][1].content)
        assert set(citation_request) == {"contexts", "citations", "occurrences"}
        assert citation_request["contexts"][0]["kind"] == "paragraph"
        assert citation_request["contexts"][0]["text"] == ("MCP 是外部内容边界。【来源：block_1】")
        assert citation_request["citations"][0]["block_id"] == "block_1"
        assert citation_request["occurrences"][0] == {
            "occurrence_id": "citation_0001",
            "block_ids": ["block_1"],
            "context_id": "context_0001",
        }
        assert set(citation_request["citations"][0]) == {
            "block_id",
            "doc_title",
            "section_path",
            "text",
        }

    asyncio.run(scenario())


def test_frozen_run_can_be_rejudged_with_case_revision_override(tmp_path: Path) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer="MCP 是外部内容边界。【来源：block_1】",
            trace_id="b" * 32,
            run_id="run_case_revision_override",
        )
        original_case = next(
            item for item in repository.list_case_catalog() if item.case_id == "c1-001"
        )
        assert original_case.active_revision_id is not None
        draft = repository.create_case_draft(
            "c1-001",
            updated_by="test-author",
            base_revision_id=original_case.active_revision_id,
        )
        changed = draft.criteria[0].model_copy(
            update={
                "title": "替代判据",
                "rule_text": "替代 case revision 的 R1 判定标准。",
            }
        )
        unchanged = [
            CaseDraftCriterionView.model_validate(item.model_dump()) for item in draft.criteria[1:]
        ]
        repository.save_case_draft(
            "c1-001",
            SaveCaseDraftRequest(
                category=draft.category,
                question=draft.question,
                target_bindings=draft.target_bindings,
                distractor_metadata=draft.distractor_metadata,
                n=draft.n,
                k=draft.k,
                execution_policy=draft.execution_policy,
                change_note="验证冻结 run 的 case revision override。",
                criteria=[changed, *unchanged],
                updated_by="test-author",
            ),
        )
        published = repository.publish_case_draft("c1-001", reviewer="reviewer-a")
        replacement = repository.review_case_revision(
            published.revision_id,
            review_status="active",
            reviewer="reviewer-a",
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
            "run_case_revision_override",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
                case_revision_overrides={"c1-001": replacement.revision_id},
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        assert revision.status == "complete"
        assert set(item.criterion_revision_id for item in revision.judgments) == {
            item.revision_id for item in replacement.criteria
        }
        assert revision.config["case_revision_overrides"] == {"c1-001": replacement.revision_id}
        assert revision.case_aggregates[0].case_revision_id == replacement.revision_id
        assert revision.result_matrix is not None
        assert (
            revision.result_matrix.categories[0].cases[0].case_revision_id
            == replacement.revision_id
        )
        original_revision = repository.get_case_revisions([original_case.active_revision_id])[0]
        assert original_revision.revision_number < replacement.revision_number

    asyncio.run(scenario())


def test_citation_support_judges_one_claim_with_multiple_blocks_as_a_group(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        answer = "两项隔离共同保护系统。"
        blocks = [
            {
                "block_id": "block_1",
                "doc_id": "doc_1",
                "doc_title": "测试文档",
                "source_label": "doc",
                "section_path": ["隔离"],
                "text": "第一项隔离限制文件访问。",
            },
            {
                "block_id": "block_2",
                "doc_id": "doc_1",
                "doc_title": "测试文档",
                "source_label": "doc",
                "section_path": ["隔离"],
                "text": "第二项隔离限制网络访问。",
            },
        ]
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer=answer,
            trace_id="b" * 32,
            run_id="run_citation_group",
        )
        provider = _CitationBatchAwareProvider()
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(
                tmp_path,
                blocks=blocks,
                citation_annotation_artifact=CitationAnnotationArtifact(
                    status="complete",
                    model_id="fake-annotation",
                    span_count=1,
                    evidence_count=2,
                    annotations=[
                        CitationAnnotation(
                            annotation_id="annotation_0001",
                            span_id="span_0001",
                            kind="sentence",
                            start_index=0,
                            end_index=len(answer),
                            text=answer,
                            block_ids=["block_1", "block_2"],
                        )
                    ],
                ),
            ),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_group",
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
        citation_request = json.loads(provider.requests[-1][1].content)
        assert citation_request["occurrences"] == [
            {
                "occurrence_id": "citation_0001",
                "block_ids": ["block_1", "block_2"],
                "context_id": "annotation_0001",
            }
        ]
        assert citation_support.result == "pass"
        assert citation_support.checker_version == "18"
        assert citation_support.payload["prompt_version"] == "citation-rubric-v11"
        assert citation_support.payload["claim_results"][0]["block_ids"] == [
            "block_1",
            "block_2",
        ]
        assert citation_support.payload["claim_results"][0]["verdict"] == "supported"
        assert {item["block_id"] for item in citation_support.payload["citations"]} == {
            "block_1",
            "block_2",
        }
        block_projections = {
            item["block_id"]: item for item in citation_support.payload["citations"]
        }
        assert block_projections["block_1"]["evidence_quotes"] == []
        assert block_projections["block_2"]["evidence_quotes"] == ["第二项隔离限制网络访问。"[:12]]
        assert citation_support.payload["batching"]["occurrence_count"] == 1
        assert citation_support.payload["batching"]["citation_relation_count"] == 2
        assert provider.claim_role_request_count == 0
        assert provider.evidence_role_request_count == 0
        assert provider.relation_review_request_count == 0

    asyncio.run(scenario())


def test_citation_support_retries_invalid_output_once_then_passes(
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
            run_id="run_citation_retry",
        )
        provider = _CitationBatchAwareProvider(invalid_first_citation_output=True)
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_retry",
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
        assert revision.status == "complete"
        assert citation_support.result == "pass"
        assert [
            invocation["status"]
            for invocation in citation_support.payload["batches"][0]["invocations"]
        ] == ["output_invalid", "succeeded"]
        assert provider.citation_request_count == 2
        criterion_invocations = [
            invocation for judgment in revision.judgments for invocation in judgment.invocations
        ]
        citation_invocations = [
            invocation
            for batch in citation_support.payload["batches"]
            for invocation in batch["invocations"]
        ]
        assert revision.judge_usage.invocation_count == (
            len(criterion_invocations) + len(citation_invocations)
        )
        assert revision.judge_usage.input_tokens == (
            sum(invocation.input_tokens for invocation in criterion_invocations)
            + sum(invocation["input_tokens"] for invocation in citation_invocations)
        )
        assert revision.judge_usage.output_tokens == (
            sum(invocation.output_tokens for invocation in criterion_invocations)
            + sum(invocation["output_tokens"] for invocation in citation_invocations)
        )
        assert revision.judge_usage.total_tokens == (
            revision.judge_usage.input_tokens + revision.judge_usage.output_tokens
        )

    asyncio.run(scenario())


def test_citation_support_retries_invalid_quote_once_then_passes(
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
            run_id="run_citation_quote_retry",
        )
        provider = _CitationBatchAwareProvider(invalid_first_citation_quote=True)
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(tmp_path),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_quote_retry",
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
        assert citation_support.result == "pass"
        assert [
            (invocation["status"], invocation["error_code"])
            for invocation in citation_support.payload["batches"][0]["invocations"]
        ] == [
            ("output_invalid", "citation_evidence_quote_invalid"),
            ("succeeded", None),
        ]

    asyncio.run(scenario())


def test_citation_support_does_not_promote_relation_advisory_to_default_gate(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        answer = "MCP 是外部内容边界。【来源：block_1】"
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer=answer,
            trace_id="b" * 32,
            run_id="run_citation_relation_retry",
        )
        provider = _CitationBatchAwareProvider(
            relation_review_present=True,
            relation_review_direction_preserved=False,
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(
                tmp_path,
                citation_annotation_artifact=_single_claim_annotation(
                    answer,
                    block_ids=["block_1"],
                ),
            ),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            "run_citation_relation_retry",
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
        assert citation_support.result == "pass"
        assert citation_support.error_code is None
        assert [
            (invocation["status"], invocation["error_code"])
            for invocation in citation_support.payload["batches"][0]["invocations"]
        ] == [("succeeded", None)]
        assert provider.claim_role_request_count == 0
        assert provider.evidence_role_request_count == 0
        assert provider.relation_review_request_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("direction_preserved", "expected_result", "expected_error"),
    [
        (True, "pass", None),
        (False, "pass", None),
        (None, "pass", None),
    ],
)
def test_citation_support_keeps_relation_advisory_out_of_the_default_path(
    tmp_path: Path,
    direction_preserved: bool | None,
    expected_result: str,
    expected_error: str | None,
) -> None:
    async def scenario() -> None:
        answer = (
            "The approach increases friction at the cost of reducing risk."
            "\u3010\u6765\u6e90\uff1ablock_1\u3011"
        )
        run_suffix = "unknown" if direction_preserved is None else str(direction_preserved).lower()
        run_id = f"run_citation_relation_review_{run_suffix}"
        database = EvaluationDatabase(tmp_path / run_suffix / "eval")
        repository = EvaluationRepository(database)
        blocks = [
            {
                "block_id": "block_1",
                "doc_id": "doc_1",
                "doc_title": "tradeoff calibration",
                "source_label": "doc",
                "section_path": ["tradeoff"],
                "text": "The approach increases friction while reducing risk.",
            }
        ]
        _add_judgeable_run(
            database,
            repository,
            case_id="c1-001",
            answer=answer,
            trace_id="b" * 32,
            run_id=run_id,
        )
        provider = _CitationBatchAwareProvider(
            relation_review_present=True,
            relation_review_direction_preserved=direction_preserved,
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(
                tmp_path / run_suffix,
                blocks=blocks,
                citation_annotation_artifact=_single_claim_annotation(
                    answer,
                    block_ids=["block_1"],
                ),
            ),  # type: ignore[arg-type]
            credentials=_MemoryCredentials(),
            chat_provider_factory=lambda _key: provider,
        )

        started = await service.start(
            run_id,
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
        assert citation_support.result == expected_result
        assert citation_support.error_code == expected_error
        assert provider.citation_request_count == 1
        assert provider.claim_role_request_count == 0
        assert provider.evidence_role_request_count == 0
        assert provider.relation_review_request_count == 0
        assert [
            invocation["status"]
            for invocation in citation_support.payload["batches"][0]["invocations"]
        ] == ["succeeded"]
        assert citation_support.payload["claim_results"][0]["relation_check"] is None

        criterion_invocations = [
            invocation for judgment in revision.judgments for invocation in judgment.invocations
        ]
        citation_invocations = citation_support.payload["batches"][0]["invocations"]
        assert revision.judge_usage.invocation_count == (
            len(criterion_invocations) + len(citation_invocations)
        )
        assert revision.judge_usage.input_tokens == (
            sum(invocation.input_tokens for invocation in criterion_invocations)
            + sum(invocation["input_tokens"] for invocation in citation_invocations)
        )
        assert revision.judge_usage.output_tokens == (
            sum(invocation.output_tokens for invocation in criterion_invocations)
            + sum(invocation["output_tokens"] for invocation in citation_invocations)
        )

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
        assert len(citation_support.payload["claim_results"]) == 22

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
        assert citation_support.result == "pending"
        assert citation_support.error_code == "citation_support_review_required"
        assert citation_support.payload["batching"]["batch_count"] >= 2
        assert citation_support.payload["batches"][0]["result"] == "pending"
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
        claim_results = citation_support.payload["claim_results"]
        assert claim_results[0]["verdict"] == "unknown"

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


def test_pending_citation_annotation_does_not_block_truncation_checker(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        database = EvaluationDatabase(tmp_path / "eval")
        repository = EvaluationRepository(database)
        _add_judgeable_run(
            database,
            repository,
            case_id="cf-003",
            answer="第一部分内容……（内容未完）",
            trace_id="b" * 32,
            run_id="run_checker_citation_pending",
        )
        service = EvaluationJudgmentService(
            database=database,
            repository=repository,
            trace_catalog=_FakeTraceCatalog(
                tmp_path,
                answer_completeness="partial",
                citation_annotation_artifact=CitationAnnotationArtifact(
                    status="pending",
                    model_id="fake-annotation",
                    span_count=1,
                    evidence_count=1,
                    error_code="citation_annotation_model_failed",
                    error_message="upstream unavailable",
                ),
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
            "run_checker_citation_pending",
            StartJudgmentRequest(
                concurrency=1,
                token_budget=1_000_000,
                judge_review_confirmed=True,
            ),
        )
        revision = await _wait_revision(service, started.revision_id)

        checker = next(item for item in revision.judgments if item.executor == "checker")
        citation_integrity = next(
            check for check in revision.checks if check.checker_id == "citation_integrity"
        )
        assert checker.normalized_result == "pass"
        assert checker.reason == "answer_completeness is partial"
        assert citation_integrity.result == "pending"
        assert citation_integrity.error_code == "citation_annotation_model_failed"
        assert revision.status == "incomplete"

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
