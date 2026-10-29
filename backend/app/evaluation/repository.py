from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.evaluation.cases import load_design_case_seeds
from app.evaluation.database import EvaluationDatabase
from app.evaluation.models import (
    AttemptView,
    CaseCatalogItem,
    CaseDraftCriterionView,
    CaseDraftView,
    CaseRevisionView,
    CaseSeed,
    CreateCaseDraftRequest,
    CriterionView,
    ExecutionPlanView,
    PlanCaseView,
    PreflightCheck,
    ResolvedTarget,
    ReviewStatus,
    RunStatus,
    RunView,
    SaveCaseDraftRequest,
    SaveSuiteDraftRequest,
    SuiteDraftView,
    SuiteRevisionView,
)
from app.evaluation.outcomes import AttemptOutcomeClass
from app.evaluation.storage_models import (
    EvalAttemptRow,
    EvalCaseDraftCriterionRow,
    EvalCaseDraftRow,
    EvalCaseRevisionCriterionRow,
    EvalCaseRevisionRow,
    EvalCaseRow,
    EvalCriterionRevisionRow,
    EvalCriterionRow,
    EvalExecutionPlanRow,
    EvalExecutionRunRow,
    EvalPlanCaseRow,
    EvalSuiteDraftCaseRow,
    EvalSuiteDraftRow,
    EvalSuiteRevisionCaseRow,
    EvalSuiteRevisionRow,
    EvalSuiteRow,
)
from app.storage.models import utc_iso, utc_now

DESIGN_SUITE_ID = "design03"
DESIGN_SUITE_REVISION_ID = "sr_design03_r1"
TERMINAL_RUN_STATUSES = {"canceled", "sealed_complete", "sealed_partial"}


class EvaluationRepository:
    """评测 revision、计划和运行事实的 SQLite 持久化入口。"""

    def __init__(self, database: EvaluationDatabase) -> None:
        self._database = database
        self.recover_interrupted_attempts()
        self.seed_design_cases()

    def seed_design_cases(self) -> None:
        """幂等导入 18 条已审核用例，并保持内容 hash 可追踪。"""

        seeds = load_design_case_seeds()
        reviewed_at = datetime.now(UTC)
        with self._database.session() as session, session.begin():
            for seed in seeds:
                self._seed_case(session, seed, reviewed_at)
            suite = session.get(EvalSuiteRow, DESIGN_SUITE_ID)
            if suite is None:
                session.add(EvalSuiteRow(suite_id=DESIGN_SUITE_ID))
                session.flush()
                suite = session.get(EvalSuiteRow, DESIGN_SUITE_ID)
            if suite is None:
                raise RuntimeError("could not create design suite")
            suite_revision = session.get(EvalSuiteRevisionRow, DESIGN_SUITE_REVISION_ID)
            if suite_revision is None:
                suite_revision = EvalSuiteRevisionRow(
                    revision_id=DESIGN_SUITE_REVISION_ID,
                    suite_id=DESIGN_SUITE_ID,
                    revision_number=1,
                    name="设计基线用例集",
                    description="设计 03 的 18 条端到端评测用例。",
                    scope_id="current-html-15",
                    review_status="active",
                    reviewer="design-03-seed",
                    reviewed_at=reviewed_at,
                    content_hash=_content_hash([seed.revision_id for seed in seeds]),
                )
                session.add(suite_revision)
                session.flush()
            existing_links = set(
                session.scalars(
                    select(EvalSuiteRevisionCaseRow.case_revision_id).where(
                        EvalSuiteRevisionCaseRow.suite_revision_id == DESIGN_SUITE_REVISION_ID
                    )
                )
            )
            for ordinal, seed in enumerate(seeds, start=1):
                if seed.revision_id in existing_links:
                    continue
                session.add(
                    EvalSuiteRevisionCaseRow(
                        suite_revision_id=DESIGN_SUITE_REVISION_ID,
                        case_revision_id=seed.revision_id,
                        ordinal=ordinal,
                    )
                )
            self._ensure_judge_ready_versions(session, seeds, reviewed_at)
            self._ensure_self_contained_versions(session, seeds, reviewed_at)
            self._ensure_minimal_judge_versions(session, seeds, reviewed_at)

    def list_suite_revisions(self) -> list[SuiteRevisionView]:
        """返回全部 suite revision 及其固定 case revision。"""

        with self._database.session() as session:
            suite_rows = session.scalars(
                select(EvalSuiteRevisionRow).order_by(
                    EvalSuiteRevisionRow.suite_id,
                    EvalSuiteRevisionRow.revision_number.desc(),
                )
            ).all()
            return [self._suite_view(session, row) for row in suite_rows]

    def get_suite_revision(self, revision_id: str) -> SuiteRevisionView:
        """读取一个 suite revision；不存在时抛出 ``KeyError``。"""

        with self._database.session() as session:
            row = session.get(EvalSuiteRevisionRow, revision_id)
            if row is None:
                raise KeyError(revision_id)
            return self._suite_view(session, row)

    def list_case_catalog(self) -> list[CaseCatalogItem]:
        """返回含草稿状态和历史 revisions 的只读用例目录。"""

        with self._database.session() as session:
            case_ids = session.scalars(
                select(EvalCaseRow.case_id).order_by(EvalCaseRow.case_id)
            ).all()
            result: list[CaseCatalogItem] = []
            for case_id in case_ids:
                revisions = session.scalars(
                    select(EvalCaseRevisionRow)
                    .where(EvalCaseRevisionRow.case_id == case_id)
                    .order_by(EvalCaseRevisionRow.revision_number.desc())
                ).all()
                active = next(
                    (revision for revision in revisions if revision.review_status == "active"),
                    None,
                )
                result.append(
                    CaseCatalogItem(
                        case_id=case_id,
                        active_revision_id=None if active is None else active.revision_id,
                        latest_revision_id=(None if not revisions else revisions[0].revision_id),
                        has_draft=session.get(EvalCaseDraftRow, case_id) is not None,
                        revisions=[self._case_view(session, revision) for revision in revisions],
                    )
                )
            return result

    def get_case_revisions(
        self,
        revision_ids: list[str],
    ) -> list[CaseRevisionView]:
        """按请求顺序读取精确 case revisions，供快速试跑计划使用。"""

        with self._database.session() as session:
            rows = session.scalars(
                select(EvalCaseRevisionRow).where(EvalCaseRevisionRow.revision_id.in_(revision_ids))
            ).all()
            by_id = {row.revision_id: row for row in rows}
            if set(by_id) != set(revision_ids):
                raise KeyError("one or more case revisions do not exist")
            return [self._case_view(session, by_id[revision_id]) for revision_id in revision_ids]

    def create_case_draft(
        self,
        case_id: str,
        *,
        updated_by: str,
        base_revision_id: str | None = None,
    ) -> CaseDraftView:
        """从最新 case revision 创建可变草稿；已有草稿时直接返回。"""

        with self._database.session() as session, session.begin():
            existing = session.get(EvalCaseDraftRow, case_id)
            if existing is not None:
                return self._case_draft_view(session, existing)
            if session.get(EvalCaseRow, case_id) is None:
                raise KeyError(case_id)
            base = (
                session.get(EvalCaseRevisionRow, base_revision_id)
                if base_revision_id is not None
                else session.scalar(
                    select(EvalCaseRevisionRow)
                    .where(EvalCaseRevisionRow.case_id == case_id)
                    .order_by(EvalCaseRevisionRow.revision_number.desc())
                )
            )
            if base is None:
                raise ValueError("case has no revision to derive a draft from")
            if base.case_id != case_id:
                raise ValueError("base revision belongs to another case")
            draft = EvalCaseDraftRow(
                case_id=case_id,
                base_revision_id=base.revision_id,
                category=base.category,
                question=base.question,
                target_bindings_json=base.target_bindings_json,
                distractor_metadata_json=base.distractor_metadata_json,
                n=base.n,
                k=base.k,
                execution_policy_json=base.execution_policy_json,
                change_note="",
                updated_by=updated_by,
            )
            session.add(draft)
            session.flush()
            criteria = session.scalars(
                select(EvalCriterionRevisionRow)
                .join(
                    EvalCaseRevisionCriterionRow,
                    EvalCaseRevisionCriterionRow.criterion_revision_id
                    == EvalCriterionRevisionRow.revision_id,
                )
                .where(EvalCaseRevisionCriterionRow.case_revision_id == base.revision_id)
                .order_by(EvalCaseRevisionCriterionRow.ordinal)
            ).all()
            for criterion in criteria:
                session.add(
                    EvalCaseDraftCriterionRow(
                        criterion_id=criterion.criterion_id,
                        case_id=case_id,
                        base_revision_id=criterion.revision_id,
                        rule_type=criterion.rule_type,
                        ordinal=criterion.ordinal,
                        title=criterion.title,
                        rule_text=criterion.rule_text,
                        evaluator=criterion.evaluator,
                        evaluator_ref=criterion.evaluator_ref,
                    )
                )
        return self.get_case_draft(case_id)

    def create_new_case_draft(
        self,
        request: CreateCaseDraftRequest,
    ) -> CaseDraftView:
        """创建稳定用例身份和一个尚未提交的空判定项草稿。"""

        with self._database.session() as session, session.begin():
            if session.get(EvalCaseRow, request.case_id) is not None:
                raise ValueError(f"case already exists: {request.case_id}")
            session.add(EvalCaseRow(case_id=request.case_id))
            session.flush()
            session.add(
                EvalCaseDraftRow(
                    case_id=request.case_id,
                    base_revision_id=None,
                    category=request.category,
                    question=request.question,
                    target_bindings_json="[]",
                    distractor_metadata_json="{}",
                    n=10,
                    k=9,
                    execution_policy_json='{"latency_class":"simple"}',
                    change_note="新建用例",
                    updated_by=request.updated_by,
                )
            )
        return self.get_case_draft(request.case_id)

    def get_case_draft(self, case_id: str) -> CaseDraftView:
        """读取一个用例草稿。"""

        with self._database.session() as session:
            row = session.get(EvalCaseDraftRow, case_id)
            if row is None:
                raise KeyError(case_id)
            return self._case_draft_view(session, row)

    def save_case_draft(
        self,
        case_id: str,
        request: SaveCaseDraftRequest,
    ) -> CaseDraftView:
        """原子覆盖保存字段和完整判定项集合。"""

        with self._database.session() as session, session.begin():
            draft = session.get(EvalCaseDraftRow, case_id)
            if draft is None:
                raise KeyError(case_id)
            for criterion in request.criteria:
                if criterion.base_revision_id is None:
                    continue
                base = session.get(
                    EvalCriterionRevisionRow,
                    criterion.base_revision_id,
                )
                criterion_owner = (
                    None if base is None else session.get(EvalCriterionRow, base.criterion_id)
                )
                if base is None or criterion_owner is None or criterion_owner.case_id != case_id:
                    raise ValueError(
                        f"criterion base belongs to another case: {criterion.criterion_id}"
                    )
            draft.category = request.category
            draft.question = request.question
            draft.target_bindings_json = _json(request.target_bindings)
            draft.distractor_metadata_json = _json(request.distractor_metadata)
            draft.n = request.n
            draft.k = request.k
            draft.execution_policy_json = _json(request.execution_policy)
            draft.change_note = request.change_note
            draft.updated_by = request.updated_by
            session.execute(
                delete(EvalCaseDraftCriterionRow).where(
                    EvalCaseDraftCriterionRow.case_id == case_id
                )
            )
            for criterion in request.criteria:
                session.add(
                    EvalCaseDraftCriterionRow(
                        criterion_id=criterion.criterion_id,
                        case_id=case_id,
                        base_revision_id=criterion.base_revision_id,
                        rule_type=criterion.rule_type,
                        ordinal=criterion.ordinal,
                        title=criterion.title,
                        rule_text=criterion.rule_text,
                        evaluator=criterion.evaluator,
                        evaluator_ref=criterion.evaluator_ref,
                    )
                )
        return self.get_case_draft(case_id)

    def publish_case_draft(
        self,
        case_id: str,
        *,
        reviewer: str,
    ) -> CaseRevisionView:
        """原子发布 reviewed case revision 并删除已消费草稿。"""

        published_at = utc_now()
        with self._database.session() as session, session.begin():
            draft = session.get(EvalCaseDraftRow, case_id)
            if draft is None:
                raise KeyError(case_id)
            draft_criteria = session.scalars(
                select(EvalCaseDraftCriterionRow)
                .where(EvalCaseDraftCriterionRow.case_id == case_id)
                .order_by(EvalCaseDraftCriterionRow.ordinal)
            ).all()
            if not draft_criteria:
                raise ValueError("case draft must contain at least one criterion")
            base = (
                None
                if draft.base_revision_id is None
                else session.get(EvalCaseRevisionRow, draft.base_revision_id)
            )
            source_ref = "manual-evaluation-editor" if base is None else base.source_ref
            criterion_revision_ids: list[str] = []
            criterion_hashes: list[str] = []
            for criterion in draft_criteria:
                criterion_revision = self._publish_criterion_draft(
                    session,
                    case_id=case_id,
                    draft=criterion,
                    reviewer=reviewer,
                    source_ref=source_ref,
                    reviewed_at=published_at,
                )
                criterion_revision_ids.append(criterion_revision.revision_id)
                criterion_hashes.append(criterion_revision.content_hash)
            revision_number = self._next_case_revision_number(session, case_id)
            content_hash = _content_hash(
                {
                    "case_id": case_id,
                    "category": draft.category,
                    "question": draft.question,
                    "target_bindings": json.loads(draft.target_bindings_json),
                    "distractors": json.loads(draft.distractor_metadata_json),
                    "n": draft.n,
                    "k": draft.k,
                    "execution_policy": json.loads(draft.execution_policy_json),
                    "criteria": [
                        {
                            "revision_id": revision_id,
                            "content_hash": criterion_hash,
                        }
                        for revision_id, criterion_hash in zip(
                            criterion_revision_ids,
                            criterion_hashes,
                            strict=True,
                        )
                    ],
                }
            )
            revision_id = f"cr_{case_id.replace('-', '_')}_{uuid4().hex[:12]}"
            session.add(
                EvalCaseRevisionRow(
                    revision_id=revision_id,
                    case_id=case_id,
                    revision_number=revision_number,
                    category=draft.category,
                    question=draft.question,
                    target_bindings_json=draft.target_bindings_json,
                    distractor_metadata_json=draft.distractor_metadata_json,
                    n=draft.n,
                    k=draft.k,
                    execution_policy_json=draft.execution_policy_json,
                    source_ref=source_ref,
                    content_hash=content_hash,
                    change_note=draft.change_note,
                    review_status="reviewed",
                    reviewer=reviewer,
                    reviewed_at=published_at,
                )
            )
            session.flush()
            for ordinal, criterion_revision_id in enumerate(
                criterion_revision_ids,
                start=1,
            ):
                session.add(
                    EvalCaseRevisionCriterionRow(
                        case_revision_id=revision_id,
                        criterion_revision_id=criterion_revision_id,
                        ordinal=ordinal,
                    )
                )
            session.execute(
                delete(EvalCaseDraftCriterionRow).where(
                    EvalCaseDraftCriterionRow.case_id == case_id
                )
            )
            session.delete(draft)
        return self._get_case_revision(revision_id)

    def review_case_revision(
        self,
        revision_id: str,
        *,
        review_status: ReviewStatus,
        reviewer: str,
    ) -> CaseRevisionView:
        """推进 reviewed／active／retired 状态并保持 active 唯一。"""

        with self._database.session() as session, session.begin():
            row = session.get(EvalCaseRevisionRow, revision_id)
            if row is None:
                raise KeyError(revision_id)
            allowed = {
                "draft": {"reviewed", "retired"},
                "reviewed": {"active", "retired"},
                "active": {"retired"},
                "retired": set(),
            }
            if review_status not in allowed[row.review_status]:
                raise ValueError(
                    f"invalid review transition: {row.review_status} -> {review_status}"
                )
            if review_status == "active":
                previous_active = session.scalars(
                    select(EvalCaseRevisionRow).where(
                        EvalCaseRevisionRow.case_id == row.case_id,
                        EvalCaseRevisionRow.review_status == "active",
                        EvalCaseRevisionRow.revision_id != revision_id,
                    )
                ).all()
                for previous in previous_active:
                    previous.review_status = "retired"
                    previous.reviewer = reviewer
                    previous.reviewed_at = utc_now()
                session.flush()
            row.review_status = review_status
            row.reviewer = reviewer
            row.reviewed_at = utc_now()
        return self._get_case_revision(revision_id)

    def create_suite_draft(
        self,
        suite_id: str,
        *,
        updated_by: str,
    ) -> SuiteDraftView:
        """从当前 suite revision 创建可变草稿，或为全新 suite 建空草稿。"""

        with self._database.session() as session, session.begin():
            existing = session.get(EvalSuiteDraftRow, suite_id)
            if existing is not None:
                return self._suite_draft_view(session, existing)
            suite = session.get(EvalSuiteRow, suite_id)
            if suite is None:
                session.add(EvalSuiteRow(suite_id=suite_id))
                session.flush()
            base = session.scalar(
                select(EvalSuiteRevisionRow)
                .where(EvalSuiteRevisionRow.suite_id == suite_id)
                .order_by(EvalSuiteRevisionRow.revision_number.desc())
            )
            draft = EvalSuiteDraftRow(
                suite_id=suite_id,
                base_revision_id=None if base is None else base.revision_id,
                name=suite_id if base is None else base.name,
                description="" if base is None else base.description,
                scope_id="custom-selection" if base is None else base.scope_id,
                change_note="",
                updated_by=updated_by,
            )
            session.add(draft)
            session.flush()
            if base is not None:
                case_ids = session.scalars(
                    select(EvalSuiteRevisionCaseRow.case_revision_id)
                    .where(EvalSuiteRevisionCaseRow.suite_revision_id == base.revision_id)
                    .order_by(EvalSuiteRevisionCaseRow.ordinal)
                ).all()
                for ordinal, case_revision_id in enumerate(case_ids, start=1):
                    session.add(
                        EvalSuiteDraftCaseRow(
                            suite_id=suite_id,
                            case_revision_id=case_revision_id,
                            ordinal=ordinal,
                        )
                    )
        return self.get_suite_draft(suite_id)

    def get_suite_draft(self, suite_id: str) -> SuiteDraftView:
        """读取一个 suite draft。"""

        with self._database.session() as session:
            row = session.get(EvalSuiteDraftRow, suite_id)
            if row is None:
                raise KeyError(suite_id)
            return self._suite_draft_view(session, row)

    def save_suite_draft(
        self,
        suite_id: str,
        request: SaveSuiteDraftRequest,
    ) -> SuiteDraftView:
        """保存 suite 元数据和精确 active case revisions 子集。"""

        if len(request.case_revision_ids) != len(set(request.case_revision_ids)):
            raise ValueError("suite case revisions must be unique")
        with self._database.session() as session, session.begin():
            draft = session.get(EvalSuiteDraftRow, suite_id)
            if draft is None:
                raise KeyError(suite_id)
            revisions = session.scalars(
                select(EvalCaseRevisionRow).where(
                    EvalCaseRevisionRow.revision_id.in_(request.case_revision_ids)
                )
            ).all()
            by_id = {row.revision_id: row for row in revisions}
            if set(by_id) != set(request.case_revision_ids):
                raise KeyError("one or more case revisions do not exist")
            if any(row.review_status != "active" for row in revisions):
                raise ValueError("suite may only reference active case revisions")
            case_ids = [row.case_id for row in revisions]
            if len(case_ids) != len(set(case_ids)):
                raise ValueError("suite may contain at most one revision per case")
            draft.name = request.name
            draft.description = request.description
            draft.scope_id = request.scope_id
            draft.change_note = request.change_note
            draft.updated_by = request.updated_by
            session.execute(
                delete(EvalSuiteDraftCaseRow).where(EvalSuiteDraftCaseRow.suite_id == suite_id)
            )
            for ordinal, case_revision_id in enumerate(
                request.case_revision_ids,
                start=1,
            ):
                session.add(
                    EvalSuiteDraftCaseRow(
                        suite_id=suite_id,
                        case_revision_id=case_revision_id,
                        ordinal=ordinal,
                    )
                )
        return self.get_suite_draft(suite_id)

    def publish_suite_draft(
        self,
        suite_id: str,
        *,
        reviewer: str,
    ) -> SuiteRevisionView:
        """发布并启用新 suite revision，同时退休旧 active revision。"""

        reviewed_at = utc_now()
        with self._database.session() as session, session.begin():
            draft = session.get(EvalSuiteDraftRow, suite_id)
            if draft is None:
                raise KeyError(suite_id)
            case_ids = session.scalars(
                select(EvalSuiteDraftCaseRow.case_revision_id)
                .where(EvalSuiteDraftCaseRow.suite_id == suite_id)
                .order_by(EvalSuiteDraftCaseRow.ordinal)
            ).all()
            if not case_ids:
                raise ValueError("suite draft must contain at least one case revision")
            revisions = session.scalars(
                select(EvalCaseRevisionRow).where(EvalCaseRevisionRow.revision_id.in_(case_ids))
            ).all()
            if any(row.review_status != "active" for row in revisions):
                raise ValueError("suite may only publish active case revisions")
            stable_case_ids = [row.case_id for row in revisions]
            if len(stable_case_ids) != len(set(stable_case_ids)):
                raise ValueError("suite may contain at most one revision per case")
            previous_active = session.scalars(
                select(EvalSuiteRevisionRow).where(
                    EvalSuiteRevisionRow.suite_id == suite_id,
                    EvalSuiteRevisionRow.review_status == "active",
                )
            ).all()
            for previous in previous_active:
                previous.review_status = "retired"
                previous.reviewer = reviewer
                previous.reviewed_at = reviewed_at
            session.flush()
            revision_number = (
                session.scalar(
                    select(func.max(EvalSuiteRevisionRow.revision_number)).where(
                        EvalSuiteRevisionRow.suite_id == suite_id
                    )
                )
                or 0
            ) + 1
            revision_id = f"sr_{suite_id}_{uuid4().hex[:12]}"
            session.add(
                EvalSuiteRevisionRow(
                    revision_id=revision_id,
                    suite_id=suite_id,
                    revision_number=revision_number,
                    name=draft.name,
                    description=draft.description,
                    scope_id=draft.scope_id,
                    review_status="active",
                    reviewer=reviewer,
                    reviewed_at=reviewed_at,
                    content_hash=_content_hash(case_ids),
                    change_note=draft.change_note,
                )
            )
            session.flush()
            for ordinal, case_revision_id in enumerate(case_ids, start=1):
                session.add(
                    EvalSuiteRevisionCaseRow(
                        suite_revision_id=revision_id,
                        case_revision_id=case_revision_id,
                        ordinal=ordinal,
                    )
                )
            session.execute(
                delete(EvalSuiteDraftCaseRow).where(EvalSuiteDraftCaseRow.suite_id == suite_id)
            )
            session.delete(draft)
        return self.get_suite_revision(revision_id)

    def create_plan(
        self,
        *,
        suite_revision_id: str | None,
        collection_snapshot_id: str,
        run_type: str,
        mode: str,
        agent_version_set: dict[str, Any],
        model_configuration: dict[str, Any],
        run_policy: dict[str, Any],
        experiment_variable: dict[str, Any],
        checks: list[PreflightCheck],
        plan_cases: list[PlanCaseView],
    ) -> ExecutionPlanView:
        """原子保存不可变 ExecutionPlan 及逐用例冻结目标。"""

        plan_id = f"plan_{uuid4().hex}"
        status = "invalid_plan" if any(check.status == "failed" for check in checks) else "planned"
        with self._database.session() as session, session.begin():
            session.add(
                EvalExecutionPlanRow(
                    plan_id=plan_id,
                    suite_revision_id=suite_revision_id,
                    collection_snapshot_id=collection_snapshot_id,
                    run_type=run_type,
                    mode=mode,
                    agent_version_set_json=_json(agent_version_set),
                    model_config_json=_json(model_configuration),
                    run_policy_json=_json(run_policy),
                    experiment_variable_json=_json(experiment_variable),
                    preflight_result_json=_json(
                        [check.model_dump(mode="json") for check in checks]
                    ),
                    status=status,
                )
            )
            for ordinal, case in enumerate(plan_cases, start=1):
                session.add(
                    EvalPlanCaseRow(
                        plan_id=plan_id,
                        case_revision_id=case.case_revision_id,
                        ordinal=ordinal,
                        n=case.n,
                        k=case.k,
                        source_n=case.source_n,
                        source_k=case.source_k,
                        resolved_targets_json=_json(
                            [target.model_dump(mode="json") for target in case.resolved_targets]
                        ),
                        execution_policy_json=_json(case.execution_policy),
                    )
                )
        return self.get_plan(plan_id)

    def get_plan(self, plan_id: str) -> ExecutionPlanView:
        """重建 ExecutionPlan 的 API 视图。"""

        with self._database.session() as session:
            row = session.get(EvalExecutionPlanRow, plan_id)
            if row is None:
                raise KeyError(plan_id)
            plan_cases: list[PlanCaseView] = []
            case_rows = session.execute(
                select(EvalPlanCaseRow, EvalCaseRevisionRow)
                .join(
                    EvalCaseRevisionRow,
                    EvalCaseRevisionRow.revision_id == EvalPlanCaseRow.case_revision_id,
                )
                .where(EvalPlanCaseRow.plan_id == plan_id)
                .order_by(EvalPlanCaseRow.ordinal)
            ).all()
            for plan_case, case_revision in case_rows:
                plan_cases.append(
                    PlanCaseView(
                        case_revision_id=plan_case.case_revision_id,
                        case_id=case_revision.case_id,
                        question=case_revision.question,
                        n=plan_case.n,
                        k=plan_case.k,
                        source_n=plan_case.source_n,
                        source_k=plan_case.source_k,
                        resolved_targets=[
                            ResolvedTarget.model_validate(item)
                            for item in json.loads(plan_case.resolved_targets_json)
                        ],
                        execution_policy=json.loads(plan_case.execution_policy_json),
                    )
                )
            checks = [
                PreflightCheck.model_validate(item)
                for item in json.loads(row.preflight_result_json)
            ]
            run_policy = json.loads(row.run_policy_json)
            return ExecutionPlanView(
                plan_id=row.plan_id,
                status=cast(Any, row.status),
                suite_revision_id=row.suite_revision_id,
                collection_snapshot_id=row.collection_snapshot_id,
                run_type=cast(Any, row.run_type),
                mode=cast(Any, row.mode),
                concurrency=int(run_policy["concurrency"]),
                agent_version_set=json.loads(row.agent_version_set_json),
                model_configuration=json.loads(row.model_config_json),
                run_policy=run_policy,
                experiment_variable=json.loads(row.experiment_variable_json),
                checks=checks,
                cases=plan_cases,
                created_at=utc_iso(row.created_at),
            )

    def create_run(self, plan_id: str) -> RunView:
        """为一个尚未运行的计划创建 ExecutionRun。"""

        run_id = f"run_{uuid4().hex}"
        with self._database.session() as session, session.begin():
            plan = session.get(EvalExecutionPlanRow, plan_id)
            if plan is None:
                raise KeyError(plan_id)
            if plan.status != "planned":
                raise ValueError("only a planned execution plan can start")
            existing_run = session.scalar(
                select(EvalExecutionRunRow).where(EvalExecutionRunRow.plan_id == plan_id)
            )
            if existing_run is not None:
                raise ValueError("execution plan already has a run")
            session.add(
                EvalExecutionRunRow(
                    run_id=run_id,
                    plan_id=plan_id,
                    status="planned",
                )
            )
        return self.get_run(run_id)

    def active_run(self) -> RunView | None:
        """返回当前未终止的 ExecutionRun；同 sidecar 同时只允许一个。"""

        with self._database.session() as session:
            row = session.scalar(
                select(EvalExecutionRunRow)
                .where(EvalExecutionRunRow.status.in_(("planned", "running", "paused")))
                .order_by(EvalExecutionRunRow.created_at.desc())
            )
            return None if row is None else self._run_view(session, row)

    def get_run(self, run_id: str) -> RunView:
        """读取 ExecutionRun 和全部 attempts。"""

        with self._database.session() as session:
            row = session.get(EvalExecutionRunRow, run_id)
            if row is None:
                raise KeyError(run_id)
            return self._run_view(session, row)

    def list_runs(self, limit: int = 20) -> list[RunView]:
        """返回最近 ExecutionRuns，供开发工作台恢复现场。"""

        with self._database.session() as session:
            rows = session.scalars(
                select(EvalExecutionRunRow)
                .order_by(EvalExecutionRunRow.created_at.desc())
                .limit(limit)
            ).all()
            return [self._run_view(session, row) for row in rows]

    def set_run_status(
        self,
        run_id: str,
        status: RunStatus,
        *,
        pause_reason: str | None = None,
        started: bool = False,
        sealed: bool = False,
    ) -> RunView:
        """更新运行状态；已封存运行不能重新进入执行态。"""

        with self._database.session() as session, session.begin():
            row = session.get(EvalExecutionRunRow, run_id)
            if row is None:
                raise KeyError(run_id)
            if row.status in {"sealed_complete", "sealed_partial"}:
                raise ValueError("sealed execution run is immutable")
            row.status = status
            row.pause_reason = pause_reason
            if started and row.started_at is None:
                row.started_at = utc_now()
            if sealed:
                row.sealed_at = utc_now()
        return self.get_run(run_id)

    def start_attempt(
        self,
        *,
        run_id: str,
        case_revision_id: str,
        replacement_for_attempt_id: str | None = None,
    ) -> str:
        """在调用外部模型前持久化 running attempt，供重启识别未知结果。"""

        attempt_id = f"attempt_{uuid4().hex}"
        with self._database.session() as session, session.begin():
            session.add(
                EvalAttemptRow(
                    attempt_id=attempt_id,
                    run_id=run_id,
                    case_revision_id=case_revision_id,
                    replacement_for_attempt_id=replacement_for_attempt_id,
                    session_id=f"eval-session-{uuid4().hex}",
                    trace_status="missing",
                    execution_status="running",
                    outcome_class="unknown",
                    started_at=utc_now(),
                )
            )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        execution_status: str,
        trace_id: str | None,
        trace_status: str,
        outcome_class: AttemptOutcomeClass,
        termination_reason: str | None,
        error_code: str | None,
        answer: str | None,
        input_tokens: int,
        output_tokens: int,
        model_calls: int,
        tool_calls: int,
        first_token_at: datetime | None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """写入一次 attempt 的最终事实；同一 attempt 不再变更。"""

        with self._database.session() as session, session.begin():
            row = session.get(EvalAttemptRow, attempt_id)
            if row is None:
                raise KeyError(attempt_id)
            if row.execution_status != "running":
                raise ValueError("attempt is already finished")
            row.execution_status = execution_status
            row.trace_id = trace_id
            row.trace_status = trace_status
            row.outcome_class = outcome_class
            row.termination_reason = termination_reason
            row.error_code = error_code
            row.answer = answer
            row.input_tokens = input_tokens
            row.output_tokens = output_tokens
            row.model_calls = model_calls
            row.tool_calls = tool_calls
            row.first_token_at = first_token_at
            row.finished_at = utc_now()
            row.usage_json = _json(usage or {})

    def recover_interrupted_attempts(self) -> None:
        """进程重启时把遗留 running attempt 标成未知，绝不自动重发。"""

        with self._database.session() as session, session.begin():
            running_attempts = session.scalars(
                select(EvalAttemptRow).where(EvalAttemptRow.execution_status == "running")
            ).all()
            for attempt in running_attempts:
                attempt.execution_status = "outcome_unknown"
                attempt.outcome_class = "unknown"
                attempt.error_code = "evaluation_process_restarted"
                attempt.finished_at = utc_now()
            running_runs = session.scalars(
                select(EvalExecutionRunRow).where(
                    EvalExecutionRunRow.status.in_(("planned", "running"))
                )
            ).all()
            for run in running_runs:
                run.status = "paused"
                run.pause_reason = "evaluation_process_restarted"

    def _seed_case(self, session, seed: CaseSeed, reviewed_at: datetime) -> None:
        case_row = session.get(EvalCaseRow, seed.case_id)
        if case_row is None:
            session.add(EvalCaseRow(case_id=seed.case_id))
            session.flush()
        revision = session.get(EvalCaseRevisionRow, seed.revision_id)
        if revision is not None:
            # 历史 revision 及其 criterion 集合都不可变；设计变化只追加新版本。
            return
        session.add(
            EvalCaseRevisionRow(
                revision_id=seed.revision_id,
                case_id=seed.case_id,
                revision_number=1,
                category=seed.category,
                question=seed.question,
                target_bindings_json=_json(seed.target_bindings),
                distractor_metadata_json=_json(seed.distractor_metadata),
                n=seed.n,
                k=seed.k,
                execution_policy_json=_json(seed.execution_policy),
                source_ref=seed.source_ref,
                content_hash=seed.content_hash,
                review_status=seed.review_status,
                reviewer=seed.reviewer,
                reviewed_at=reviewed_at,
            )
        )
        session.flush()
        for criterion in seed.criteria:
            criterion_row = session.get(EvalCriterionRow, criterion.criterion_id)
            if criterion_row is None:
                session.add(
                    EvalCriterionRow(
                        criterion_id=criterion.criterion_id,
                        case_id=seed.case_id,
                    )
                )
                session.flush()
            criterion_revision = session.get(
                EvalCriterionRevisionRow,
                criterion.revision_id,
            )
            if criterion_revision is None:
                session.add(
                    EvalCriterionRevisionRow(
                        revision_id=criterion.revision_id,
                        criterion_id=criterion.criterion_id,
                        revision_number=1,
                        rule_type=criterion.rule_type,
                        ordinal=criterion.ordinal,
                        title=criterion.title,
                        rule_text=criterion.rule_text,
                        evaluator=criterion.evaluator,
                        evaluator_ref=None,
                        source_ref=seed.source_ref,
                        content_hash=_content_hash(criterion.model_dump(mode="json")),
                        review_status="active",
                        reviewer=seed.reviewer,
                        reviewed_at=reviewed_at,
                    )
                )
                session.flush()
            link = session.get(
                EvalCaseRevisionCriterionRow,
                (seed.revision_id, criterion.revision_id),
            )
            if link is None:
                session.add(
                    EvalCaseRevisionCriterionRow(
                        case_revision_id=seed.revision_id,
                        criterion_revision_id=criterion.revision_id,
                        ordinal=criterion.ordinal,
                    )
                )

    def _ensure_judge_ready_versions(
        self,
        session,
        seeds: list[CaseSeed],
        reviewed_at: datetime,
    ) -> None:
        """首次升级时创建 r2 judge-ready revisions 与新的 active suite。"""

        configured_revision_ids: list[str] = []
        for seed in seeds:
            case_revision_id = f"cr_{seed.case_id.replace('-', '_')}_judge_v1"
            configured_revision_ids.append(case_revision_id)
            if session.get(EvalCaseRevisionRow, case_revision_id) is not None:
                continue
            criterion_revision_ids: list[str] = []
            criterion_hashes: list[str] = []
            for criterion in seed.criteria:
                checker = seed.case_id == "cf-003" and criterion.criterion_id.endswith("-r1")
                evaluator = "checker" if checker else "llm_judge"
                evaluator_ref = "truncation_declared" if checker else "judge-rubric-v1"
                revision_id = (
                    f"ccr_{seed.case_id.replace('-', '_')}_"
                    f"{criterion.criterion_id.rsplit('-', 1)[-1]}_judge_v1"
                )
                revision = EvalCriterionRevisionRow(
                    revision_id=revision_id,
                    criterion_id=criterion.criterion_id,
                    revision_number=2,
                    rule_type=criterion.rule_type,
                    ordinal=criterion.ordinal,
                    title=criterion.title,
                    rule_text=criterion.rule_text,
                    evaluator=evaluator,
                    evaluator_ref=evaluator_ref,
                    source_ref=seed.source_ref,
                    content_hash=_content_hash(
                        {
                            **criterion.model_dump(mode="json"),
                            "evaluator": evaluator,
                            "evaluator_ref": evaluator_ref,
                            "judge_ready": "v1",
                        }
                    ),
                    change_note="023A 判官路由：语义 LLM Judge，截断规则使用 checker。",
                    review_status="active",
                    reviewer="023A-seed",
                    reviewed_at=reviewed_at,
                )
                session.add(revision)
                session.flush()
                criterion_revision_ids.append(revision_id)
                criterion_hashes.append(revision.content_hash)
            previous_active = session.scalars(
                select(EvalCaseRevisionRow).where(
                    EvalCaseRevisionRow.case_id == seed.case_id,
                    EvalCaseRevisionRow.review_status == "active",
                )
            ).all()
            for previous in previous_active:
                previous.review_status = "retired"
                previous.reviewer = "023A-seed"
                previous.reviewed_at = reviewed_at
            session.flush()
            session.add(
                EvalCaseRevisionRow(
                    revision_id=case_revision_id,
                    case_id=seed.case_id,
                    revision_number=2,
                    category=seed.category,
                    question=seed.question,
                    target_bindings_json=_json(seed.target_bindings),
                    distractor_metadata_json=_json(seed.distractor_metadata),
                    n=seed.n,
                    k=seed.k,
                    execution_policy_json=_json(seed.execution_policy),
                    source_ref=seed.source_ref,
                    content_hash=_content_hash(
                        {
                            "case": seed.content_hash,
                            "criteria": criterion_hashes,
                            "judge_ready": "v1",
                        }
                    ),
                    change_note="023A 判官就绪修订版。",
                    review_status="active",
                    reviewer="023A-seed",
                    reviewed_at=reviewed_at,
                )
            )
            session.flush()
            for ordinal, criterion_revision_id in enumerate(
                criterion_revision_ids,
                start=1,
            ):
                session.add(
                    EvalCaseRevisionCriterionRow(
                        case_revision_id=case_revision_id,
                        criterion_revision_id=criterion_revision_id,
                        ordinal=ordinal,
                    )
                )

        judge_suite_revision_id = "sr_design03_judge_v1"
        if session.get(EvalSuiteRevisionRow, judge_suite_revision_id) is not None:
            return
        previous_suites = session.scalars(
            select(EvalSuiteRevisionRow).where(
                EvalSuiteRevisionRow.suite_id == DESIGN_SUITE_ID,
                EvalSuiteRevisionRow.review_status == "active",
            )
        ).all()
        for previous in previous_suites:
            previous.review_status = "retired"
            previous.reviewer = "023A-seed"
            previous.reviewed_at = reviewed_at
        session.flush()
        session.add(
            EvalSuiteRevisionRow(
                revision_id=judge_suite_revision_id,
                suite_id=DESIGN_SUITE_ID,
                revision_number=2,
                name="设计基线 · LLM Judge v1",
                description="LLM Judge 为语义判定主力，截断声明使用 checker。",
                scope_id="current-html-15",
                review_status="active",
                reviewer="023A-seed",
                reviewed_at=reviewed_at,
                content_hash=_content_hash(configured_revision_ids),
                change_note="023A judge-ready suite。",
            )
        )
        session.flush()
        for ordinal, case_revision_id in enumerate(configured_revision_ids, start=1):
            session.add(
                EvalSuiteRevisionCaseRow(
                    suite_revision_id=judge_suite_revision_id,
                    case_revision_id=case_revision_id,
                    ordinal=ordinal,
                )
            )

    def _ensure_self_contained_versions(
        self,
        session,
        seeds: list[CaseSeed],
        reviewed_at: datetime,
    ) -> None:
        """创建已人工确认的自包含 criterion 修订版与 active suite。"""

        configured_revision_ids: list[str] = []
        for seed in seeds:
            case_revision_id = f"cr_{seed.case_id.replace('-', '_')}_selfcontained_v1"
            configured_revision_ids.append(case_revision_id)
            if session.get(EvalCaseRevisionRow, case_revision_id) is not None:
                continue
            criterion_revision_ids: list[str] = []
            criterion_hashes: list[str] = []
            for criterion in seed.criteria:
                checker = seed.case_id == "cf-003" and criterion.criterion_id.endswith("-r1")
                evaluator = "checker" if checker else "llm_judge"
                evaluator_ref = "truncation_declared" if checker else "judge-rubric-v1"
                revision_id = (
                    f"ccr_{seed.case_id.replace('-', '_')}_"
                    f"{criterion.criterion_id.rsplit('-', 1)[-1]}_selfcontained_v1"
                )
                revision = EvalCriterionRevisionRow(
                    revision_id=revision_id,
                    criterion_id=criterion.criterion_id,
                    revision_number=3,
                    rule_type=criterion.rule_type,
                    ordinal=criterion.ordinal,
                    title=criterion.title,
                    rule_text=criterion.rule_text,
                    evaluator=evaluator,
                    evaluator_ref=evaluator_ref,
                    source_ref=seed.source_ref,
                    content_hash=_content_hash(
                        {
                            **criterion.model_dump(mode="json"),
                            "evaluator": evaluator,
                            "evaluator_ref": evaluator_ref,
                            "judge_ready": "selfcontained-v1",
                        }
                    ),
                    change_note="判据自包含修订：仅凭答案和当前 criterion 判定。",
                    review_status="active",
                    reviewer="peidong",
                    reviewed_at=reviewed_at,
                )
                session.add(revision)
                session.flush()
                criterion_revision_ids.append(revision_id)
                criterion_hashes.append(revision.content_hash)
            previous_active = session.scalars(
                select(EvalCaseRevisionRow).where(
                    EvalCaseRevisionRow.case_id == seed.case_id,
                    EvalCaseRevisionRow.review_status == "active",
                )
            ).all()
            for previous in previous_active:
                previous.review_status = "retired"
                previous.reviewer = "peidong"
                previous.reviewed_at = reviewed_at
            session.flush()
            session.add(
                EvalCaseRevisionRow(
                    revision_id=case_revision_id,
                    case_id=seed.case_id,
                    revision_number=3,
                    category=seed.category,
                    question=seed.question,
                    target_bindings_json=_json(seed.target_bindings),
                    distractor_metadata_json=_json(seed.distractor_metadata),
                    n=seed.n,
                    k=seed.k,
                    execution_policy_json=_json(seed.execution_policy),
                    source_ref=seed.source_ref,
                    content_hash=_content_hash(
                        {
                            "case": seed.content_hash,
                            "criteria": criterion_hashes,
                            "judge_ready": "selfcontained-v1",
                        }
                    ),
                    change_note="判据自包含审查通过，生成新版用例 revision。",
                    review_status="active",
                    reviewer="peidong",
                    reviewed_at=reviewed_at,
                )
            )
            session.flush()
            for ordinal, criterion_revision_id in enumerate(
                criterion_revision_ids,
                start=1,
            ):
                session.add(
                    EvalCaseRevisionCriterionRow(
                        case_revision_id=case_revision_id,
                        criterion_revision_id=criterion_revision_id,
                        ordinal=ordinal,
                    )
                )

        suite_revision_id = "sr_design03_selfcontained_v1"
        if session.get(EvalSuiteRevisionRow, suite_revision_id) is not None:
            return
        previous_suites = session.scalars(
            select(EvalSuiteRevisionRow).where(
                EvalSuiteRevisionRow.suite_id == DESIGN_SUITE_ID,
                EvalSuiteRevisionRow.review_status == "active",
            )
        ).all()
        for previous in previous_suites:
            previous.review_status = "retired"
            previous.reviewer = "peidong"
            previous.reviewed_at = reviewed_at
        session.flush()
        session.add(
            EvalSuiteRevisionRow(
                revision_id=suite_revision_id,
                suite_id=DESIGN_SUITE_ID,
                revision_number=3,
                name="设计基线 · 自包含判据 v1",
                description="18 个用例、70 条 criterion；仅凭答案和当前 criterion 语义判定。",
                scope_id="current-html-15",
                review_status="active",
                reviewer="peidong",
                reviewed_at=reviewed_at,
                content_hash=_content_hash(configured_revision_ids),
                change_note="判据自包含审查后的 active suite。",
            )
        )
        session.flush()
        for ordinal, case_revision_id in enumerate(configured_revision_ids, start=1):
            session.add(
                EvalSuiteRevisionCaseRow(
                    suite_revision_id=suite_revision_id,
                    case_revision_id=case_revision_id,
                    ordinal=ordinal,
                )
            )

    def _ensure_minimal_judge_versions(
        self,
        session,
        seeds: list[CaseSeed],
        reviewed_at: datetime,
    ) -> None:
        """创建只向主判官发送答案和 criterion 的 active revision v2。"""

        configured_revision_ids: list[str] = []
        for seed in seeds:
            case_revision_id = f"cr_{seed.case_id.replace('-', '_')}_minimal_judge_v2"
            configured_revision_ids.append(case_revision_id)
            if session.get(EvalCaseRevisionRow, case_revision_id) is not None:
                continue
            criterion_revision_ids: list[str] = []
            criterion_hashes: list[str] = []
            for criterion in seed.criteria:
                checker = seed.case_id == "cf-003" and criterion.criterion_id.endswith("-r1")
                evaluator = "checker" if checker else "llm_judge"
                evaluator_ref = "truncation_declared" if checker else "judge-rubric-v2"
                revision_id = (
                    f"ccr_{seed.case_id.replace('-', '_')}_"
                    f"{criterion.criterion_id.rsplit('-', 1)[-1]}_minimal_judge_v2"
                )
                revision = EvalCriterionRevisionRow(
                    revision_id=revision_id,
                    criterion_id=criterion.criterion_id,
                    revision_number=5,
                    rule_type=criterion.rule_type,
                    ordinal=criterion.ordinal,
                    title=criterion.title,
                    rule_text=criterion.rule_text,
                    evaluator=evaluator,
                    evaluator_ref=evaluator_ref,
                    source_ref=seed.source_ref,
                    content_hash=_content_hash(
                        {
                            **criterion.model_dump(mode="json"),
                            "evaluator": evaluator,
                            "evaluator_ref": evaluator_ref,
                            "judge_ready": "minimal-v2",
                        }
                    ),
                    change_note="主判官最小输入 v2：修正设计表分区标题被并入规则文本。",
                    review_status="active",
                    reviewer="023A-seed",
                    reviewed_at=reviewed_at,
                )
                session.add(revision)
                session.flush()
                criterion_revision_ids.append(revision_id)
                criterion_hashes.append(revision.content_hash)
            previous_active = session.scalars(
                select(EvalCaseRevisionRow).where(
                    EvalCaseRevisionRow.case_id == seed.case_id,
                    EvalCaseRevisionRow.review_status == "active",
                )
            ).all()
            for previous in previous_active:
                previous.review_status = "retired"
                previous.reviewer = "023A-seed"
                previous.reviewed_at = reviewed_at
            session.flush()
            session.add(
                EvalCaseRevisionRow(
                    revision_id=case_revision_id,
                    case_id=seed.case_id,
                    revision_number=5,
                    category=seed.category,
                    question=seed.question,
                    target_bindings_json=_json(seed.target_bindings),
                    distractor_metadata_json=_json(seed.distractor_metadata),
                    n=seed.n,
                    k=seed.k,
                    execution_policy_json=_json(seed.execution_policy),
                    source_ref=seed.source_ref,
                    content_hash=_content_hash(
                        {
                            "case": seed.content_hash,
                            "criteria": criterion_hashes,
                            "judge_ready": "minimal-v2",
                        }
                    ),
                    change_note="主判官最小输入 v2，修正规则文本中的分区标题污染。",
                    review_status="active",
                    reviewer="023A-seed",
                    reviewed_at=reviewed_at,
                )
            )
            session.flush()
            for ordinal, criterion_revision_id in enumerate(
                criterion_revision_ids,
                start=1,
            ):
                session.add(
                    EvalCaseRevisionCriterionRow(
                        case_revision_id=case_revision_id,
                        criterion_revision_id=criterion_revision_id,
                        ordinal=ordinal,
                    )
                )

        suite_revision_id = "sr_design03_minimal_judge_v2"
        if session.get(EvalSuiteRevisionRow, suite_revision_id) is not None:
            return
        previous_suites = session.scalars(
            select(EvalSuiteRevisionRow).where(
                EvalSuiteRevisionRow.suite_id == DESIGN_SUITE_ID,
                EvalSuiteRevisionRow.review_status == "active",
            )
        ).all()
        for previous in previous_suites:
            previous.review_status = "retired"
            previous.reviewer = "023A-seed"
            previous.reviewed_at = reviewed_at
        session.flush()
        session.add(
            EvalSuiteRevisionRow(
                revision_id=suite_revision_id,
                suite_id=DESIGN_SUITE_ID,
                revision_number=5,
                name="设计基线 · 最小判官输入 v2",
                description="主判官只接收答案和当前 criterion；规则文本已排除设计表分区标题。",
                scope_id="current-html-15",
                review_status="active",
                reviewer="023A-seed",
                reviewed_at=reviewed_at,
                content_hash=_content_hash(configured_revision_ids),
                change_note="主判官最小输入 suite v2。",
            )
        )
        session.flush()
        for ordinal, case_revision_id in enumerate(configured_revision_ids, start=1):
            session.add(
                EvalSuiteRevisionCaseRow(
                    suite_revision_id=suite_revision_id,
                    case_revision_id=case_revision_id,
                    ordinal=ordinal,
                )
            )

    def _get_case_revision(self, revision_id: str) -> CaseRevisionView:
        with self._database.session() as session:
            row = session.get(EvalCaseRevisionRow, revision_id)
            if row is None:
                raise KeyError(revision_id)
            return self._case_view(session, row)

    @staticmethod
    def _next_case_revision_number(session, case_id: str) -> int:
        return (
            session.scalar(
                select(func.max(EvalCaseRevisionRow.revision_number)).where(
                    EvalCaseRevisionRow.case_id == case_id
                )
            )
            or 0
        ) + 1

    @staticmethod
    def _publish_criterion_draft(
        session,
        *,
        case_id: str,
        draft: EvalCaseDraftCriterionRow,
        reviewer: str,
        source_ref: str,
        reviewed_at: datetime,
    ) -> EvalCriterionRevisionRow:
        if draft.base_revision_id is not None:
            base = session.get(EvalCriterionRevisionRow, draft.base_revision_id)
            if base is not None and (
                base.rule_type,
                base.ordinal,
                base.title,
                base.rule_text,
                base.evaluator,
                base.evaluator_ref,
            ) == (
                draft.rule_type,
                draft.ordinal,
                draft.title,
                draft.rule_text,
                draft.evaluator,
                draft.evaluator_ref,
            ):
                return base
        criterion = session.get(EvalCriterionRow, draft.criterion_id)
        if criterion is None:
            session.add(
                EvalCriterionRow(
                    criterion_id=draft.criterion_id,
                    case_id=case_id,
                )
            )
            session.flush()
        elif criterion.case_id != case_id:
            raise ValueError("criterion belongs to another case")
        revision_number = (
            session.scalar(
                select(func.max(EvalCriterionRevisionRow.revision_number)).where(
                    EvalCriterionRevisionRow.criterion_id == draft.criterion_id
                )
            )
            or 0
        ) + 1
        content = {
            "criterion_id": draft.criterion_id,
            "revision_number": revision_number,
            "rule_type": draft.rule_type,
            "ordinal": draft.ordinal,
            "title": draft.title,
            "rule_text": draft.rule_text,
            "evaluator": draft.evaluator,
            "evaluator_ref": draft.evaluator_ref,
            "source_ref": source_ref,
        }
        revision = EvalCriterionRevisionRow(
            revision_id=f"ccr_{draft.criterion_id.replace('-', '_')}_{uuid4().hex[:10]}",
            criterion_id=draft.criterion_id,
            revision_number=revision_number,
            rule_type=draft.rule_type,
            ordinal=draft.ordinal,
            title=draft.title,
            rule_text=draft.rule_text,
            evaluator=draft.evaluator,
            evaluator_ref=draft.evaluator_ref,
            source_ref=source_ref,
            content_hash=_content_hash(content),
            change_note="",
            review_status="reviewed",
            reviewer=reviewer,
            reviewed_at=reviewed_at,
        )
        session.add(revision)
        session.flush()
        return revision

    def _case_draft_view(self, session, row: EvalCaseDraftRow) -> CaseDraftView:
        criteria = session.scalars(
            select(EvalCaseDraftCriterionRow)
            .where(EvalCaseDraftCriterionRow.case_id == row.case_id)
            .order_by(EvalCaseDraftCriterionRow.ordinal)
        ).all()
        return CaseDraftView(
            case_id=row.case_id,
            base_revision_id=row.base_revision_id,
            next_revision_number=self._next_case_revision_number(session, row.case_id),
            category=row.category,
            question=row.question,
            target_bindings=json.loads(row.target_bindings_json),
            distractor_metadata=json.loads(row.distractor_metadata_json),
            n=row.n,
            k=row.k,
            execution_policy=json.loads(row.execution_policy_json),
            change_note=row.change_note,
            criteria=[
                CaseDraftCriterionView(
                    criterion_id=criterion.criterion_id,
                    base_revision_id=criterion.base_revision_id,
                    rule_type=cast(Any, criterion.rule_type),
                    ordinal=criterion.ordinal,
                    title=criterion.title,
                    rule_text=criterion.rule_text,
                    evaluator=cast(Any, criterion.evaluator),
                    evaluator_ref=criterion.evaluator_ref,
                )
                for criterion in criteria
            ],
            updated_by=row.updated_by,
            updated_at=utc_iso(row.updated_at),
        )

    def _suite_draft_view(self, session, row: EvalSuiteDraftRow) -> SuiteDraftView:
        case_ids = session.scalars(
            select(EvalSuiteDraftCaseRow.case_revision_id)
            .where(EvalSuiteDraftCaseRow.suite_id == row.suite_id)
            .order_by(EvalSuiteDraftCaseRow.ordinal)
        ).all()
        cases: list[CaseRevisionView] = []
        for case_revision_id in case_ids:
            revision = session.get(EvalCaseRevisionRow, case_revision_id)
            if revision is None:
                raise RuntimeError(f"suite draft references missing revision: {case_revision_id}")
            cases.append(self._case_view(session, revision))
        next_revision_number = (
            session.scalar(
                select(func.max(EvalSuiteRevisionRow.revision_number)).where(
                    EvalSuiteRevisionRow.suite_id == row.suite_id
                )
            )
            or 0
        ) + 1
        return SuiteDraftView(
            suite_id=row.suite_id,
            base_revision_id=row.base_revision_id,
            next_revision_number=next_revision_number,
            name=row.name or row.suite_id,
            description=row.description,
            scope_id=row.scope_id,
            change_note=row.change_note,
            cases=cases,
            updated_by=row.updated_by,
            updated_at=utc_iso(row.updated_at),
        )

    def _suite_view(self, session, row: EvalSuiteRevisionRow) -> SuiteRevisionView:
        case_ids = session.scalars(
            select(EvalSuiteRevisionCaseRow.case_revision_id)
            .where(EvalSuiteRevisionCaseRow.suite_revision_id == row.revision_id)
            .order_by(EvalSuiteRevisionCaseRow.ordinal)
        ).all()
        cases: list[CaseRevisionView] = []
        stale_reasons: list[str] = []
        if row.review_status != "active":
            stale_reasons.append("suite_revision_not_active")
        for case_revision_id in case_ids:
            case_revision = session.get(EvalCaseRevisionRow, case_revision_id)
            if case_revision is None:
                raise RuntimeError(f"suite references missing revision: {case_revision_id}")
            cases.append(self._case_view(session, case_revision))
            if case_revision.review_status != "active":
                stale_reasons.append(
                    f"case_revision_not_active:{case_revision.case_id}:{case_revision_id}"
                )
        stable_case_ids = [case.case_id for case in cases]
        if len(stable_case_ids) != len(set(stable_case_ids)):
            stale_reasons.append("duplicate_case_revision")
        return SuiteRevisionView(
            suite_id=row.suite_id,
            revision_id=row.revision_id,
            revision_number=row.revision_number,
            name=row.name,
            description=row.description or "",
            scope_id=row.scope_id,
            review_status=cast(ReviewStatus, row.review_status),
            change_note=row.change_note,
            stale=bool(stale_reasons),
            stale_reasons=stale_reasons,
            case_count=len(cases),
            cases=cases,
        )

    def _case_view(
        self,
        session,
        case_revision: EvalCaseRevisionRow,
    ) -> CaseRevisionView:
        return CaseRevisionView(
            case_id=case_revision.case_id,
            revision_id=case_revision.revision_id,
            revision_number=case_revision.revision_number,
            category=case_revision.category,
            question=case_revision.question,
            target_bindings=json.loads(case_revision.target_bindings_json),
            distractor_metadata=json.loads(case_revision.distractor_metadata_json),
            n=case_revision.n,
            k=case_revision.k,
            execution_policy=json.loads(case_revision.execution_policy_json),
            review_status=cast(ReviewStatus, case_revision.review_status),
            reviewer=case_revision.reviewer,
            source_ref=case_revision.source_ref,
            content_hash=case_revision.content_hash,
            change_note=case_revision.change_note,
            criteria=self._criteria_view(session, case_revision.revision_id),
        )

    @staticmethod
    def _criteria_view(session, case_revision_id: str) -> list[CriterionView]:
        rows = session.execute(
            select(EvalCriterionRevisionRow)
            .join(
                EvalCaseRevisionCriterionRow,
                EvalCaseRevisionCriterionRow.criterion_revision_id
                == EvalCriterionRevisionRow.revision_id,
            )
            .where(EvalCaseRevisionCriterionRow.case_revision_id == case_revision_id)
            .order_by(EvalCaseRevisionCriterionRow.ordinal)
        ).scalars()
        return [
            CriterionView(
                criterion_id=row.criterion_id,
                revision_id=row.revision_id,
                rule_type=cast(Any, row.rule_type),
                ordinal=row.ordinal,
                title=row.title,
                rule_text=row.rule_text,
                evaluator=row.evaluator,
                evaluator_ref=row.evaluator_ref,
                change_note=row.change_note,
            )
            for row in rows
        ]

    def _run_view(self, session, row: EvalExecutionRunRow) -> RunView:
        plan = session.get(EvalExecutionPlanRow, row.plan_id)
        if plan is None:
            raise RuntimeError(f"run references missing plan: {row.plan_id}")
        suite = (
            None
            if plan.suite_revision_id is None
            else session.get(EvalSuiteRevisionRow, plan.suite_revision_id)
        )
        attempts = session.scalars(
            select(EvalAttemptRow)
            .where(EvalAttemptRow.run_id == row.run_id)
            .order_by(EvalAttemptRow.started_at, EvalAttemptRow.attempt_id)
        ).all()
        attempt_views = [
            AttemptView(
                attempt_id=attempt.attempt_id,
                case_revision_id=attempt.case_revision_id,
                replacement_for_attempt_id=attempt.replacement_for_attempt_id,
                session_id=attempt.session_id,
                trace_id=attempt.trace_id,
                trace_status=cast(Any, attempt.trace_status),
                execution_status=attempt.execution_status,
                outcome_class=cast(Any, attempt.outcome_class),
                termination_reason=attempt.termination_reason,
                error_code=attempt.error_code,
                answer=attempt.answer,
                input_tokens=attempt.input_tokens,
                output_tokens=attempt.output_tokens,
                model_calls=attempt.model_calls,
                tool_calls=attempt.tool_calls,
                started_at=utc_iso(attempt.started_at),
                first_token_at=(
                    None if attempt.first_token_at is None else utc_iso(attempt.first_token_at)
                ),
                finished_at=(None if attempt.finished_at is None else utc_iso(attempt.finished_at)),
            )
            for attempt in attempts
        ]
        execution_counts = dict(
            session.execute(
                select(EvalAttemptRow.execution_status, func.count())
                .where(EvalAttemptRow.run_id == row.run_id)
                .group_by(EvalAttemptRow.execution_status)
            ).all()
        )
        totals = {
            "attempts": len(attempt_views),
            "valid_samples": sum(attempt.outcome_class == "product" for attempt in attempt_views),
            "infrastructure_failures": sum(
                attempt.outcome_class == "infrastructure" for attempt in attempt_views
            ),
            "running": int(execution_counts.get("running", 0)),
            "succeeded": int(execution_counts.get("succeeded", 0)),
            "partial": int(execution_counts.get("partial", 0)),
            "failed": int(execution_counts.get("failed", 0)),
            "outcome_unknown": int(execution_counts.get("outcome_unknown", 0)),
            "input_tokens": sum(attempt.input_tokens for attempt in attempts),
            "output_tokens": sum(attempt.output_tokens for attempt in attempts),
            "model_calls": sum(attempt.model_calls for attempt in attempts),
        }
        return RunView(
            run_id=row.run_id,
            plan_id=row.plan_id,
            plan_mode=cast(Any, plan.mode),
            run_type=cast(Any, plan.run_type),
            suite_id=None if suite is None else suite.suite_id,
            suite_name=None if suite is None else (suite.name or suite.suite_id),
            suite_revision_id=plan.suite_revision_id,
            suite_revision_number=None if suite is None else suite.revision_number,
            status=cast(RunStatus, row.status),
            pause_reason=row.pause_reason,
            created_at=utc_iso(row.created_at),
            started_at=None if row.started_at is None else utc_iso(row.started_at),
            sealed_at=None if row.sealed_at is None else utc_iso(row.sealed_at),
            updated_at=utc_iso(row.updated_at),
            totals=totals,
            attempts=attempt_views,
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(value: Any) -> str:
    import hashlib

    return hashlib.sha256(_json(value).encode()).hexdigest()
