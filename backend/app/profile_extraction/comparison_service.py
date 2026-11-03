from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select

from app.profile_extraction.errors import ProfileExtractionError
from app.profile_extraction.models import (
    ComparisonItemDecision,
    DocumentChecklist,
    DocumentChecklistItem,
    ProfileComparison,
    ProfileComparisonConcludeRequest,
    ProfileComparisonCreateRequest,
    ProfileComparisonItemDecisionRequest,
)
from app.profile_extraction.util import (
    json_dumps,
    json_loads_object_list,
    json_loads_string_list,
    new_identifier,
    normalize_name,
    ordered_unique,
    stable_hash,
)
from app.storage.database import StorageDatabase
from app.storage.models import utc_iso, utc_now
from app.storage.profile_extraction_models import (
    DocumentChecklistRow,
    PreviewCandidateRow,
    ProfileComparisonRow,
    ProfilePreviewRunRow,
)

_PROFILE_SIDES = ("a", "b")
_PRIORITIES = ("must", "should", "nice_to_have")


class ComparisonService:
    """在冻结清单上比较两个 completed Preview Run。"""

    def __init__(
        self,
        data_root: Path,
        *,
        database: StorageDatabase | None = None,
    ) -> None:
        self._database = database or StorageDatabase(data_root)

    def create(self, request: ProfileComparisonCreateRequest) -> ProfileComparison:
        """校验 run／checklist 条件并生成逐文档比较 draft。"""

        if request.run_a_id == request.run_b_id:
            raise ProfileExtractionError(
                "comparison_runs_identical",
                "A Profile comparison requires two different Preview Runs",
                422,
            )
        with self._database.session() as session, session.begin():
            run_a = session.get(ProfilePreviewRunRow, request.run_a_id)
            run_b = session.get(ProfilePreviewRunRow, request.run_b_id)
            if run_a is None or run_b is None:
                raise ProfileExtractionError(
                    "preview_run_not_found",
                    "One or both Preview Runs do not exist",
                    404,
                )
            self._validate_runs(run_a, run_b)
            checklists = self._load_checklists(session, request.checklist_ids, run_a)
            candidates_a = self._load_candidates(session, run_a.run_id)
            candidates_b = self._load_candidates(session, run_b.run_id)
            document_results = self._document_results(
                checklists=checklists,
                candidates_a=candidates_a,
                candidates_b=candidates_b,
            )
            checklist_refs = [
                {
                    "checklist_id": checklist.checklist_id,
                    "checklist_hash": checklist.checklist_hash,
                }
                for checklist in checklists
            ]
            now = utc_now()
            comparison_id = new_identifier("pc")
            comparison_hash = self._comparison_hash(
                comparison_id=comparison_id,
                run_a=run_a,
                run_b=run_b,
                checklist_refs=checklist_refs,
                document_results=document_results,
                outcome=None,
                outcome_note="",
            )
            row = ProfileComparisonRow(
                comparison_id=comparison_id,
                snapshot_id=run_a.snapshot_id,
                run_a_id=run_a.run_id,
                run_b_id=run_b.run_id,
                profile_a_id=run_a.profile_id,
                profile_b_id=run_b.profile_id,
                checklist_refs_json=json_dumps(checklist_refs),
                status="draft",
                document_results_json=json_dumps(document_results),
                outcome=None,
                outcome_note="",
                comparison_hash=comparison_hash,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return self._comparison(row)

    def get(self, comparison_id: str) -> ProfileComparison:
        """读取一个 Profile Comparison。"""

        with self._database.session() as session:
            row = session.get(ProfileComparisonRow, comparison_id)
            if row is None:
                raise ProfileExtractionError(
                    "profile_comparison_not_found",
                    "Profile comparison does not exist",
                    404,
                )
            return self._comparison(row)

    def list(self) -> list[ProfileComparison]:
        """按创建时间倒序读取 Profile Comparison 历史。"""

        with self._database.session() as session:
            rows = session.scalars(
                select(ProfileComparisonRow).order_by(ProfileComparisonRow.created_at.desc())
            ).all()
            return [self._comparison(row) for row in rows]

    def apply_item_decisions(
        self,
        comparison_id: str,
        request: ProfileComparisonItemDecisionRequest,
    ) -> ProfileComparison:
        """应用用户对 uncertain 清单项的人工决定。"""

        with self._database.session() as session, session.begin():
            row = session.get(ProfileComparisonRow, comparison_id)
            if row is None:
                raise ProfileExtractionError(
                    "profile_comparison_not_found",
                    "Profile comparison does not exist",
                    404,
                )
            if row.status != "draft":
                raise ProfileExtractionError(
                    "profile_comparison_immutable",
                    "Concluded comparisons cannot be modified",
                    409,
                )
            document_results = json_loads_object_list(row.document_results_json)
            candidate_ids_by_side = {
                "a": self._candidate_ids_for_run(session, row.run_a_id),
                "b": self._candidate_ids_for_run(session, row.run_b_id),
            }
            for decision in request.decisions:
                self._apply_decision(
                    document_results,
                    decision,
                    candidate_ids_by_side[decision.profile_side],
                )
            self._recompute_metrics(document_results)
            row.document_results_json = json_dumps(document_results)
            row.comparison_hash = self._comparison_hash(
                comparison_id=row.comparison_id,
                run_a=session.get(ProfilePreviewRunRow, row.run_a_id),
                run_b=session.get(ProfilePreviewRunRow, row.run_b_id),
                checklist_refs=self._checklist_refs(row),
                document_results=document_results,
                outcome=None,
                outcome_note="",
            )
            session.flush()
            return self._comparison(row)

    def conclude(
        self,
        comparison_id: str,
        request: ProfileComparisonConcludeRequest,
    ) -> ProfileComparison:
        """完成 must 门禁后提交用户总体结论。"""

        with self._database.session() as session, session.begin():
            row = session.get(ProfileComparisonRow, comparison_id)
            if row is None:
                raise ProfileExtractionError(
                    "profile_comparison_not_found",
                    "Profile comparison does not exist",
                    404,
                )
            if row.status != "draft":
                raise ProfileExtractionError(
                    "profile_comparison_immutable",
                    "Profile comparison is already concluded",
                    409,
                )
            document_results = json_loads_object_list(row.document_results_json)
            docs_by_id = {str(result["doc_id"]): result for result in document_results}
            unknown_docs = set(request.document_outcomes) - set(docs_by_id)
            if unknown_docs:
                raise ProfileExtractionError(
                    "comparison_document_unknown",
                    "Document outcome references an unknown comparison document",
                    422,
                )
            blocking_docs = [
                doc_id for doc_id, result in docs_by_id.items() if self._has_unresolved_must(result)
            ]
            missing_reasons = [
                doc_id
                for doc_id in blocking_docs
                if not request.must_override_reasons.get(doc_id, "").strip()
            ]
            if missing_reasons:
                raise ProfileExtractionError(
                    "must_override_reason_required",
                    "Every document with unresolved must items requires an override reason",
                    422,
                )
            for doc_id, result in docs_by_id.items():
                result["user_outcome"] = request.document_outcomes.get(doc_id)
                result["must_override_reason"] = request.must_override_reasons.get(doc_id)
            now = utc_now()
            row.status = "concluded"
            row.outcome = request.outcome
            row.outcome_note = request.outcome_note
            row.completed_at = now
            row.document_results_json = json_dumps(document_results)
            row.comparison_hash = self._comparison_hash(
                comparison_id=row.comparison_id,
                run_a=session.get(ProfilePreviewRunRow, row.run_a_id),
                run_b=session.get(ProfilePreviewRunRow, row.run_b_id),
                checklist_refs=self._checklist_refs(row),
                document_results=document_results,
                outcome=request.outcome,
                outcome_note=request.outcome_note,
            )
            session.flush()
            return self._comparison(row)

    @staticmethod
    def _validate_runs(run_a: ProfilePreviewRunRow, run_b: ProfilePreviewRunRow) -> None:
        if run_a.status != "completed" or run_b.status != "completed":
            raise ProfileExtractionError(
                "comparison_requires_completed_runs",
                "Both Preview Runs must be completed",
                409,
            )
        controlled_fields = (
            "snapshot_id",
            "document_scope_json",
            "document_versions_json",
            "candidate_pipeline_version",
            "name_normalization_version",
            "compiler_version",
            "prompt_version",
            "provider_profile_json",
            "window_policy_json",
        )
        mismatches = [
            field for field in controlled_fields if getattr(run_a, field) != getattr(run_b, field)
        ]
        if mismatches:
            raise ProfileExtractionError(
                "comparison_inputs_mismatch",
                f"Preview Runs are not comparable; mismatched fields: {', '.join(mismatches)}",
                409,
            )

    @staticmethod
    def _load_checklists(
        session: Any,
        checklist_ids: list[str],
        run: ProfilePreviewRunRow,
    ) -> list[DocumentChecklist]:
        unique_ids = ordered_unique(checklist_ids)
        if len(unique_ids) != len(checklist_ids):
            raise ProfileExtractionError(
                "comparison_checklist_duplicate",
                "Checklist IDs must be unique",
                422,
            )
        rows = [session.get(DocumentChecklistRow, checklist_id) for checklist_id in unique_ids]
        if any(row is None for row in rows):
            raise ProfileExtractionError(
                "checklist_not_found",
                "One or more checklists do not exist",
                404,
            )
        typed_rows = [row for row in rows if row is not None]
        if any(row.status != "frozen" for row in typed_rows):
            raise ProfileExtractionError(
                "comparison_requires_frozen_checklists",
                "Every checklist must be frozen",
                409,
            )
        document_versions = json.loads(run.document_versions_json)
        document_scope = set(json_loads_string_list(run.document_scope_json))
        if {row.doc_id for row in typed_rows} != document_scope:
            raise ProfileExtractionError(
                "comparison_checklist_scope_mismatch",
                "Checklists must cover exactly the Preview Run document scope",
                409,
            )
        if len(typed_rows) != len(document_scope):
            raise ProfileExtractionError(
                "comparison_checklist_duplicate_document",
                "Each document must have exactly one checklist",
                409,
            )
        for row in typed_rows:
            if row.snapshot_id != run.snapshot_id:
                raise ProfileExtractionError(
                    "comparison_checklist_snapshot_mismatch",
                    "Checklist snapshot does not match the Preview Run",
                    409,
                )
            if row.doc_version != document_versions.get(row.doc_id):
                raise ProfileExtractionError(
                    "comparison_checklist_version_mismatch",
                    "Checklist document version does not match the Preview Run",
                    409,
                )
        return [
            DocumentChecklist(
                checklist_id=row.checklist_id,
                snapshot_id=row.snapshot_id,
                doc_id=row.doc_id,
                doc_version=row.doc_version,
                status=row.status,  # type: ignore[arg-type]
                evaluation_note=row.evaluation_note,
                items=[
                    DocumentChecklistItem.model_validate(item)
                    for item in json_loads_object_list(row.items_json)
                ],
                checklist_hash=row.checklist_hash,
                created_at=utc_iso(row.created_at),
                updated_at=utc_iso(row.updated_at),
                frozen_at=utc_iso(row.frozen_at) if row.frozen_at else None,
            )
            for row in typed_rows
        ]

    @staticmethod
    def _load_candidates(
        session: Any,
        run_id: str,
    ) -> dict[str, list[PreviewCandidateRow]]:
        rows = session.scalars(
            select(PreviewCandidateRow)
            .where(PreviewCandidateRow.run_id == run_id)
            .order_by(PreviewCandidateRow.doc_id, PreviewCandidateRow.ordinal)
        ).all()
        result: dict[str, list[PreviewCandidateRow]] = {}
        for row in rows:
            result.setdefault(row.doc_id, []).append(row)
        return result

    def _document_results(
        self,
        *,
        checklists: list[DocumentChecklist],
        candidates_a: dict[str, list[PreviewCandidateRow]],
        candidates_b: dict[str, list[PreviewCandidateRow]],
    ) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        for checklist in sorted(checklists, key=lambda item: item.doc_id):
            a_rows = candidates_a.get(checklist.doc_id, [])
            b_rows = candidates_b.get(checklist.doc_id, [])
            a_matches, a_extra = self._match_items(checklist, a_rows)
            b_matches, b_extra = self._match_items(checklist, b_rows)
            item_results = [
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "priority": item.priority,
                    "profile_a": a_matches[item.item_id],
                    "profile_b": b_matches[item.item_id],
                }
                for item in checklist.items
            ]
            metric_items = [
                {
                    "item_id": item.item_id,
                    "name": item.name,
                    "priority": item.priority,
                }
                for item in checklist.items
            ]
            profile_a_metrics = self._metrics(metric_items, item_results, "profile_a")
            profile_b_metrics = self._metrics(metric_items, item_results, "profile_b")
            profile_a_metrics["extra_candidate_count"] = len(a_extra)
            profile_b_metrics["extra_candidate_count"] = len(b_extra)
            result: dict[str, object] = {
                "doc_id": checklist.doc_id,
                "doc_version": checklist.doc_version,
                "checklist_id": checklist.checklist_id,
                "checklist_hash": checklist.checklist_hash,
                "profile_a": profile_a_metrics,
                "profile_b": profile_b_metrics,
                "item_results": item_results,
                "profile_a_extra_candidate_ids": a_extra,
                "profile_b_extra_candidate_ids": b_extra,
                "user_outcome": None,
                "must_override_reason": None,
                "decisions": [],
            }
            results.append(result)
        return results

    @staticmethod
    def _match_items(
        checklist: DocumentChecklist,
        candidates: list[PreviewCandidateRow],
    ) -> tuple[dict[str, dict[str, object]], list[str]]:
        matches: dict[str, list[PreviewCandidateRow]] = {
            item.item_id: [] for item in checklist.items
        }
        candidate_hits: dict[str, list[str]] = {
            candidate.candidate_id: [] for candidate in candidates
        }
        for item in checklist.items:
            item_variants = {normalize_name(value) for value in [item.name, *item.aliases]}
            item_evidence = set(item.source_block_ids)
            for candidate in candidates:
                candidate_variants = {
                    normalize_name(value)
                    for value in [
                        candidate.name,
                        *json_loads_string_list(candidate.aliases_json),
                    ]
                }
                candidate_evidence = set(json_loads_string_list(candidate.source_block_ids_json))
                if not (item_variants & candidate_variants or item_evidence & candidate_evidence):
                    continue
                matches[item.item_id].append(candidate)
                candidate_hits[candidate.candidate_id].append(item.item_id)
        result: dict[str, dict[str, object]] = {}
        for item in checklist.items:
            matched_candidates = matches[item.item_id]
            multiple_candidates = len(matched_candidates) > 1
            candidate_ids = [candidate.candidate_id for candidate in matched_candidates]
            matched = False
            uncertain = multiple_candidates
            evidence_ids: list[str] = []
            for candidate in matched_candidates:
                item_variants = {normalize_name(value) for value in [item.name, *item.aliases]}
                candidate_variants = {
                    normalize_name(value)
                    for value in [
                        candidate.name,
                        *json_loads_string_list(candidate.aliases_json),
                    ]
                }
                evidence_ids.extend(json_loads_string_list(candidate.source_block_ids_json))
                has_name_match = bool(item_variants & candidate_variants)
                has_evidence_match = bool(
                    set(item.source_block_ids)
                    & set(json_loads_string_list(candidate.source_block_ids_json))
                )
                if has_name_match and has_evidence_match:
                    matched = True
                elif has_name_match or has_evidence_match:
                    uncertain = True
                if len(candidate_hits[candidate.candidate_id]) > 1:
                    uncertain = True
            status = (
                "matched"
                if matched and not uncertain
                else ("uncertain_match" if matched_candidates else "missing")
            )
            result[item.item_id] = {
                "status": status,
                "candidate_ids": candidate_ids,
                "evidence_block_ids": ordered_unique(evidence_ids),
                "user_override": None,
            }
        extra = [candidate_id for candidate_id, item_ids in candidate_hits.items() if not item_ids]
        return result, extra

    @staticmethod
    def _metrics(
        items: Sequence[Mapping[str, object]],
        item_results: list[dict[str, object]],
        side: Literal["profile_a", "profile_b"],
    ) -> dict[str, object]:
        by_id = {str(result["item_id"]): result for result in item_results}
        metrics: dict[str, object] = {
            "must": {"matched": 0, "missing": 0, "uncertain": 0},
            "should": {"matched": 0, "missing": 0, "uncertain": 0},
            "nice_to_have": {"matched": 0, "missing": 0, "uncertain": 0},
            "noise_hits": 0,
            "extra_candidate_count": 0,
        }
        for item in items:
            item_id = str(item["item_id"])
            priority = str(item["priority"])
            item_result = by_id[item_id]
            side_result = item_result.get(side)
            if not isinstance(side_result, dict):
                continue
            status = str(side_result["status"])
            if priority == "noise":
                if status == "matched":
                    metrics["noise_hits"] = _as_int(metrics["noise_hits"]) + 1
                continue
            bucket = metrics[priority]
            if not isinstance(bucket, MutableMapping):
                continue
            if status == "matched":
                bucket["matched"] = _as_int(bucket["matched"]) + 1
            elif status == "missing":
                bucket["missing"] = _as_int(bucket["missing"]) + 1
            else:
                bucket["uncertain"] = _as_int(bucket["uncertain"]) + 1
        return metrics

    @staticmethod
    def _has_unresolved_must(result: dict[str, object]) -> bool:
        for side in _PROFILE_SIDES:
            metrics = result[f"profile_{side}"]
            if not isinstance(metrics, dict):
                continue
            must = metrics["must"]
            if isinstance(must, dict) and (
                _as_int(must.get("missing", 0)) > 0 or _as_int(must.get("uncertain", 0)) > 0
            ):
                return True
        return False

    def _apply_decision(
        self,
        document_results: list[dict[str, object]],
        decision: ComparisonItemDecision,
        allowed_candidate_ids: dict[str, set[str]],
    ) -> None:
        for document in document_results:
            doc_id = str(document["doc_id"])
            items = document.get("item_results", [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("item_id") != decision.item_id:
                    continue
                if (
                    decision.decision == "matched"
                    and decision.candidate_id not in allowed_candidate_ids.get(doc_id, set())
                ):
                    raise ProfileExtractionError(
                        "comparison_candidate_not_found",
                        "The selected candidate does not belong to this Preview Run document",
                        422,
                    )
                side_key = f"profile_{decision.profile_side}"
                side = item.get(side_key)
                if not isinstance(side, dict):
                    continue
                if side.get("status") != "uncertain_match":
                    raise ProfileExtractionError(
                        "comparison_item_not_uncertain",
                        "Only uncertain matches require an item decision",
                        409,
                    )
                if decision.decision == "matched" and decision.candidate_id not in {
                    str(candidate_id) for candidate_id in side.get("candidate_ids", [])
                }:
                    raise ProfileExtractionError(
                        "comparison_candidate_not_proposed",
                        "The selected candidate was not proposed for this checklist item",
                        422,
                    )
                side["status"] = decision.decision
                if decision.decision == "matched":
                    side["candidate_ids"] = ordered_unique(
                        [
                            *(
                                side.get("candidate_ids", [])
                                if isinstance(side.get("candidate_ids"), list)
                                else []
                            ),
                            str(decision.candidate_id),
                        ]
                    )
                else:
                    side["candidate_ids"] = []
                    side["evidence_block_ids"] = []
                side["user_override"] = {
                    "decision": decision.decision,
                    "candidate_id": decision.candidate_id,
                    "reason": decision.reason,
                }
                decisions = document.get("decisions")
                if isinstance(decisions, list):
                    decisions.append(
                        {
                            "item_id": decision.item_id,
                            "profile_side": decision.profile_side,
                            "decision": decision.decision,
                            "candidate_id": decision.candidate_id,
                            "reason": decision.reason,
                        }
                    )
                return
        raise ProfileExtractionError(
            "comparison_item_not_found",
            "Comparison item does not exist",
            404,
        )

    @staticmethod
    def _recompute_metrics(document_results: list[dict[str, object]]) -> None:
        for document in document_results:
            raw_items = document.get("item_results", [])
            if not isinstance(raw_items, list):
                continue
            items = [
                {
                    "item_id": str(item["item_id"]),
                    "name": str(item["name"]),
                    "priority": item["priority"],
                }
                for item in raw_items
                if isinstance(item, dict)
            ]
            for side in ("profile_a", "profile_b"):
                document[side] = ComparisonService._metrics(
                    items,
                    [item for item in raw_items if isinstance(item, dict)],
                    side,  # type: ignore[arg-type]
                )
            for side in ("a", "b"):
                extra_key = f"profile_{side}_extra_candidate_ids"
                extra = document.get(extra_key)
                metrics = document.get(f"profile_{side}")
                if isinstance(extra, list) and isinstance(metrics, MutableMapping):
                    metrics["extra_candidate_count"] = len(extra)

    @staticmethod
    def _candidate_ids_for_run(session: Any, run_id: str) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        rows = session.execute(
            select(PreviewCandidateRow.doc_id, PreviewCandidateRow.candidate_id).where(
                PreviewCandidateRow.run_id == run_id
            )
        ).all()
        for doc_id, candidate_id in rows:
            result.setdefault(str(doc_id), set()).add(str(candidate_id))
        return result

    @staticmethod
    def _checklist_refs(row: ProfileComparisonRow) -> list[dict[str, str]]:
        return json_loads_object_list(row.checklist_refs_json)  # type: ignore[return-value]

    @staticmethod
    def _comparison_hash(
        *,
        comparison_id: str,
        run_a: ProfilePreviewRunRow | None,
        run_b: ProfilePreviewRunRow | None,
        checklist_refs: Sequence[Mapping[str, object]],
        document_results: Sequence[Mapping[str, object]],
        outcome: str | None,
        outcome_note: str,
    ) -> str:
        if run_a is None or run_b is None:
            raise ProfileExtractionError(
                "preview_run_not_found",
                "Preview Run does not exist",
                404,
            )
        return stable_hash(
            {
                "comparison_id": comparison_id,
                "run_a": {
                    "run_id": run_a.run_id,
                    "profile_hash": run_a.profile_hash,
                    "preview_fingerprint": run_a.preview_fingerprint,
                },
                "run_b": {
                    "run_id": run_b.run_id,
                    "profile_hash": run_b.profile_hash,
                    "preview_fingerprint": run_b.preview_fingerprint,
                },
                "checklist_refs": checklist_refs,
                "document_results": document_results,
                "outcome": outcome,
                "outcome_note": outcome_note,
            }
        )

    @staticmethod
    def _comparison(row: ProfileComparisonRow) -> ProfileComparison:
        return ProfileComparison(
            comparison_id=row.comparison_id,
            snapshot_id=row.snapshot_id,
            run_a_id=row.run_a_id,
            run_b_id=row.run_b_id,
            profile_a_id=row.profile_a_id,
            profile_b_id=row.profile_b_id,
            checklist_refs=json_loads_object_list(row.checklist_refs_json),  # type: ignore[arg-type]
            status=row.status,  # type: ignore[arg-type]
            document_results=json_loads_object_list(row.document_results_json),
            outcome=row.outcome,  # type: ignore[arg-type]
            outcome_note=row.outcome_note,
            comparison_hash=row.comparison_hash,
            created_at=utc_iso(row.created_at),
            completed_at=utc_iso(row.completed_at) if row.completed_at else None,
        )


def _as_int(value: object) -> int:
    """把内部计数 JSON 值安全转成整数。"""

    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return 0
