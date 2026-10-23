from __future__ import annotations

from dataclasses import asdict

from app import __version__
from app.agent.prompt import release_prompt_bundle
from app.agent.runtime import AGENT_LOOP_VERSION
from app.agent.token_counter import ConservativeObservationTokenCounter
from app.agent.tool_adapter import TOOL_CONTRACT_VERSION
from app.collections.models import CandidateSnapshotManifest
from app.collections.service import CollectionSnapshotService
from app.evaluation.models import (
    CaseRevisionView,
    ExecutionPlanView,
    PlanCaseView,
    PreflightCheck,
    PreflightRequest,
    QuickPlanRequest,
    ResolvedTarget,
    TargetedDocumentSelector,
)
from app.evaluation.repository import EvaluationRepository
from app.ingestion.errors import IngestFailure
from app.providers.chat import DeepSeekChatProvider
from app.providers.embeddings import QWEN_EMBEDDING_PROFILE
from app.settings.models import RuntimeSettingsResponse


class EvaluationPlanner:
    """把 active suite revision 解析为不可变 ExecutionPlan。"""

    def __init__(
        self,
        repository: EvaluationRepository,
        collection_service: CollectionSnapshotService,
    ) -> None:
        self._repository = repository
        self._collection_service = collection_service

    def preflight(
        self,
        request: PreflightRequest,
        *,
        runtime_settings: RuntimeSettingsResponse,
    ) -> ExecutionPlanView:
        """验证 suite、快照、目标文档和运行预算，并持久化计划。"""

        suite = self._repository.get_suite_revision(request.suite_revision_id)
        checks: list[PreflightCheck] = []
        checks.append(
            PreflightCheck(
                code="suite_active",
                status=(
                    "passed"
                    if suite.review_status == "active"
                    and not suite.stale
                    and all(case.review_status == "active" for case in suite.cases)
                    else "failed"
                ),
                message=(
                    f"suite {suite.revision_id} 含 {suite.case_count} 条 active revision"
                    if suite.review_status == "active" and not suite.stale
                    else ("suite revision is stale: " + ", ".join(suite.stale_reasons))
                ),
            )
        )
        manifest = self._load_snapshot(request.collection_snapshot_id, checks)
        plan_cases: list[PlanCaseView] = []
        for case in suite.cases:
            resolved_targets: list[ResolvedTarget] = []
            for target_value in case.target_bindings:
                selector = TargetedDocumentSelector.model_validate(target_value)
                matches = (
                    []
                    if manifest is None
                    else [
                        document
                        for document in manifest.documents
                        if selector.title_contains.casefold() in document.title.casefold()
                        or selector.title_contains.casefold() in document.source_label.casefold()
                    ]
                )
                if not matches:
                    checks.append(
                        PreflightCheck(
                            code=f"target:{case.case_id}",
                            status="failed",
                            message=(f"目标文档不存在：{selector.title_contains}"),
                        )
                    )
                    continue
                resolved_targets.extend(
                    ResolvedTarget(
                        selector=selector,
                        doc_id=document.doc_id,
                        doc_version=document.doc_version,
                        title=document.title,
                        section_path=selector.section_path,
                    )
                    for document in matches
                )
            plan_cases.append(
                PlanCaseView(
                    case_revision_id=case.revision_id,
                    case_id=case.case_id,
                    question=case.question,
                    n=case.n,
                    k=case.k,
                    source_n=case.n,
                    source_k=case.k,
                    resolved_targets=resolved_targets,
                    execution_policy=case.execution_policy,
                )
            )
        prompt = release_prompt_bundle()
        model_profile = DeepSeekChatProvider(None).profile
        plan = self._repository.create_plan(
            suite_revision_id=request.suite_revision_id,
            collection_snapshot_id=request.collection_snapshot_id,
            run_type=request.run_type,
            mode="formal",
            agent_version_set={
                "app_version": __version__,
                "prompt_bundle_version": prompt.version,
                "prompt_template_hash": prompt.template_hash,
                "agent_loop_version": AGENT_LOOP_VERSION,
                "tool_contract_version": TOOL_CONTRACT_VERSION,
                "observation_token_counter_version": (
                    ConservativeObservationTokenCounter().version
                ),
                "embedding_profile": QWEN_EMBEDDING_PROFILE.model_dump(mode="json"),
            },
            model_configuration=asdict(model_profile),
            run_policy={
                "concurrency": request.concurrency,
                "total_token_budget": request.total_token_budget,
                "model_request_budget": request.model_request_budget,
                "replacement_attempt_budget": request.replacement_attempt_budget,
                "attempt_timeout_seconds": request.attempt_timeout_seconds,
                "trace_mode": "full",
                "runtime_settings": runtime_settings.model_dump(mode="json"),
            },
            experiment_variable={
                "name": request.experiment_variable_name,
                "value": request.experiment_variable_value,
            },
            checks=checks,
            plan_cases=plan_cases,
        )
        return plan

    def quick_preflight(
        self,
        request: QuickPlanRequest,
        *,
        runtime_settings: RuntimeSettingsResponse,
    ) -> ExecutionPlanView:
        """冻结 reviewed／active 用例，生成不依赖 suite 的探索性计划。"""

        cases = self._repository.get_case_revisions(request.case_revision_ids)
        checks: list[PreflightCheck] = [
            PreflightCheck(
                code="quick_source_revisions",
                status=(
                    "passed"
                    if all(case.review_status in {"reviewed", "active"} for case in cases)
                    else "failed"
                ),
                message=(
                    f"已选择 {len(cases)} 条 reviewed／active case revisions"
                    if all(case.review_status in {"reviewed", "active"} for case in cases)
                    else "quick run only accepts reviewed or active case revisions"
                ),
            )
        ]
        stable_case_ids = [case.case_id for case in cases]
        checks.append(
            PreflightCheck(
                code="quick_case_unique",
                status=(
                    "passed" if len(stable_case_ids) == len(set(stable_case_ids)) else "failed"
                ),
                message=(
                    "每条稳定用例最多选择一个 revision"
                    if len(stable_case_ids) == len(set(stable_case_ids))
                    else "quick run contains multiple revisions of the same case"
                ),
            )
        )
        manifest = self._load_snapshot(request.collection_snapshot_id, checks)
        plan_cases: list[PlanCaseView] = []
        for case in cases:
            resolved_targets = self._resolve_targets(
                case=case,
                manifest=manifest,
                checks=checks,
            )
            plan_cases.append(
                PlanCaseView(
                    case_revision_id=case.revision_id,
                    case_id=case.case_id,
                    question=case.question,
                    n=request.n_override,
                    k=case.k,
                    source_n=case.n,
                    source_k=case.k,
                    resolved_targets=resolved_targets,
                    execution_policy=case.execution_policy,
                )
            )
        prompt = release_prompt_bundle()
        model_profile = DeepSeekChatProvider(None).profile
        return self._repository.create_plan(
            suite_revision_id=None,
            collection_snapshot_id=request.collection_snapshot_id,
            run_type=request.run_type,
            mode="exploratory",
            agent_version_set={
                "app_version": __version__,
                "prompt_bundle_version": prompt.version,
                "prompt_template_hash": prompt.template_hash,
                "agent_loop_version": AGENT_LOOP_VERSION,
                "tool_contract_version": TOOL_CONTRACT_VERSION,
                "observation_token_counter_version": (
                    ConservativeObservationTokenCounter().version
                ),
                "embedding_profile": QWEN_EMBEDDING_PROFILE.model_dump(mode="json"),
            },
            model_configuration=asdict(model_profile),
            run_policy={
                "concurrency": request.concurrency,
                "total_token_budget": request.total_token_budget,
                "model_request_budget": request.model_request_budget,
                "replacement_attempt_budget": request.replacement_attempt_budget,
                "attempt_timeout_seconds": request.attempt_timeout_seconds,
                "n_override": request.n_override,
                "source_review_statuses": {case.revision_id: case.review_status for case in cases},
                "trace_mode": "full",
                "runtime_settings": runtime_settings.model_dump(mode="json"),
            },
            experiment_variable={
                "name": "quick_trial",
                "value": f"n_override={request.n_override}",
            },
            checks=checks,
            plan_cases=plan_cases,
        )

    def _resolve_targets(
        self,
        *,
        case: CaseRevisionView,
        manifest: CandidateSnapshotManifest | None,
        checks: list[PreflightCheck],
    ) -> list[ResolvedTarget]:
        resolved_targets: list[ResolvedTarget] = []
        for target_value in case.target_bindings:
            selector = TargetedDocumentSelector.model_validate(target_value)
            matches = (
                []
                if manifest is None
                else [
                    document
                    for document in manifest.documents
                    if selector.title_contains.casefold() in document.title.casefold()
                    or selector.title_contains.casefold() in document.source_label.casefold()
                ]
            )
            if not matches:
                checks.append(
                    PreflightCheck(
                        code=f"target:{case.case_id}",
                        status="failed",
                        message=f"目标文档不存在：{selector.title_contains}",
                    )
                )
                continue
            resolved_targets.extend(
                ResolvedTarget(
                    selector=selector,
                    doc_id=document.doc_id,
                    doc_version=document.doc_version,
                    title=document.title,
                    section_path=selector.section_path,
                )
                for document in matches
            )
        return resolved_targets

    def _load_snapshot(
        self,
        snapshot_id: str,
        checks: list[PreflightCheck],
    ) -> CandidateSnapshotManifest | None:
        try:
            manifest = self._collection_service.snapshot(snapshot_id)
        except IngestFailure as error:
            checks.append(
                PreflightCheck(
                    code="collection_snapshot",
                    status="failed",
                    message=f"{error.code}: {error.message}",
                )
            )
            return None
        readable = manifest.status in {"ready", "ready_partial"}
        checks.append(
            PreflightCheck(
                code="collection_snapshot",
                status="passed" if readable else "failed",
                message=(
                    f"{manifest.snapshot_id} 包含 {len(manifest.documents)} 篇文档，"
                    f"completeness={manifest.completeness}"
                ),
            )
        )
        return manifest if readable else None
