"""5.1 统一全量／增量状态机、确定性校验和地图构造。"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from app.dual_entry_index.induction import (
    CONSOLIDATION_OUTPUT_TOKENS_PER_DRAFT,
    CONSOLIDATION_PROMPT_VERSION,
    CONSOLIDATION_SYSTEM_PROMPT,
    CONTEXT_ASSEMBLY_VERSION,
    MAX_CANDIDATE_GROUPS_PER_JUDGEMENT_REQUEST,
    SUMMARY_PROMPT_VERSION,
    SUMMARY_SYSTEM_PROMPT,
    TERM_SENSE_PROMPT_VERSION,
    TERM_SENSE_SYSTEM_PROMPT,
    InductionFailure,
    InductionUsage,
    JudgementBatchResult,
    LLMTermSenseInducer,
)
from app.dual_entry_index.models import (
    ArtifactDiagnostic,
    CandidateGroup,
    CandidateGroupJudgement,
    CandidateOccurrence,
    CompilationDiagnostics,
    CompileContract,
    ConceptSense,
    DiagnosticRecord,
    LinkBasis,
    ScanBlockStatus,
    ScanResult,
    SenseDecision,
    SnapshotData,
    SourceBlock,
    SourceBlockIdentity,
    Stage51Artifact,
    StageMetric,
    TermDecision,
    TermRecord,
    TermSenseDraft,
    TermSenseLink,
)
from app.dual_entry_index.normalization import (
    canonical_hash,
    deterministic_identifier,
    normalize_lexical_key,
)
from app.dual_entry_index.reconciliation import (
    ReconciliationError,
    build_reconciliation_plan,
)
from app.dual_entry_index.scanner import CandidateScanner, ScanContractError

COMPILER_VERSION = "dual-entry-stage51-v15"
INDUCTION_CONTRACT_VERSION = "stage51-induction-v14"
COMPILE_CONTRACT_VERSION = "stage51-compile-v14"
StageName = Literal[
    "scan",
    "term_judgement",
    "consolidation",
    "materialization",
    "total_compile",
]
STAGE_ORDER: tuple[StageName, ...] = (
    "scan",
    "term_judgement",
    "consolidation",
    "materialization",
    "total_compile",
)


class CompilationFailure(RuntimeError):
    """5.1 编译无法满足输入或发布不变量时抛出的失败。"""


class Stage51Compiler:
    """以基础地图、快照差异和统一变更集生成下一代 5.1 地图。"""

    def __init__(
        self,
        inducer: LLMTermSenseInducer,
        *,
        max_input_chars: int,
        max_output_tokens: int,
    ) -> None:
        self._inducer = inducer
        self._max_input_chars = max_input_chars
        self._max_output_tokens = max_output_tokens

    async def compile(
        self,
        *,
        snapshot: SnapshotData,
        scanner: CandidateScanner,
        base_artifact: Stage51Artifact | None,
        concurrency: int,
        progress: Callable[[str], None] | None = None,
    ) -> tuple[Stage51Artifact, CompilationDiagnostics]:
        """执行一次全量或增量 5.1 编译。"""

        compile_started = time.perf_counter()
        current_contract = self._compile_contract(scanner, snapshot)
        blocks_by_id = {block.block_id: block for block in snapshot.blocks}
        current_block_index = tuple(
            SourceBlockIdentity(block_id=block.block_id, content_hash=block.content_hash)
            for block in snapshot.blocks
        )
        full_rebuild = base_artifact is None or not self._contracts_compatible(
            base_artifact.compile_contract,
            current_contract,
        )
        added_block_ids, deleted_block_ids = self._calculate_block_delta(
            base_artifact,
            current_block_index,
        )
        same_snapshot = (
            base_artifact is not None
            and base_artifact.snapshot_id == snapshot.snapshot_id
            and base_artifact.snapshot_manifest_hash == snapshot.manifest_hash
        )
        if (
            not full_rebuild
            and same_snapshot
            and base_artifact is not None
            and not added_block_ids
            and not deleted_block_ids
        ):
            self._notify(progress, "基础地图与当前快照一致，复用现有地图")
            reused_metrics = tuple(
                StageMetric(
                    stage=stage,
                    wall_seconds=(time.perf_counter() - compile_started)
                    if stage == "total_compile"
                    else 0.0,
                    requests=0,
                    attempts=0,
                    failed_attempts=0,
                    retry_attempts=0,
                    input_tokens=0,
                    output_tokens=0,
                    cache_hit_tokens=0,
                    cache_miss_tokens=0,
                )
                for stage in STAGE_ORDER
            )
            return base_artifact, CompilationDiagnostics(
                snapshot_id=snapshot.snapshot_id,
                map_id=base_artifact.map_id,
                candidate_occurrence_count=0,
                candidate_group_count=0,
                block_count=len(snapshot.blocks),
                added_block_ids=(),
                deleted_block_ids=(),
                rescanned_block_ids=(),
                skipped_block_ids=(),
                candidate_group_judgements=(),
                llm_requests=0,
                llm_attempts=0,
                failed_llm_attempts=0,
                retry_attempts=0,
                input_tokens=0,
                output_tokens=0,
                cache_hit_tokens=0,
                cache_miss_tokens=0,
                actual_model_ids=base_artifact.compile_contract.actual_model_ids,
                stage_metrics=reused_metrics,
                records=(
                    DiagnosticRecord(
                        code="map_reused",
                        message="基础地图与当前快照及编译契约一致",
                    ),
                ),
            )
        if full_rebuild:
            added_block_ids = tuple(block.block_id for block in snapshot.blocks)
            deleted_block_ids = tuple()

        # 重扫已有块时，旧义项会先因块支持被临时剔除；模型仍需要看到基础地图中的
        # 活动义项身份，才能判断重扫结果是否应复用同一概念，而不是静默新建重复义项。
        existing_links = (
            () if full_rebuild or base_artifact is None else base_artifact.term_sense_links
        )
        existing_senses = (
            {}
            if full_rebuild or base_artifact is None
            else {sense.concept_sense_id: sense for sense in base_artifact.concept_senses}
        )

        self._notify(
            progress,
            "扫描候选影响闭包："
            f"added_blocks={len(added_block_ids)} "
            f"deleted_blocks={len(deleted_block_ids)}",
        )
        scan_started = time.perf_counter()
        try:
            (
                scan_result,
                touched_keys,
                rescanned_block_ids,
                pruned_terms,
                pruned_links,
                pruned_senses,
            ) = self._scan_affected_closure(
                scanner=scanner,
                snapshot=snapshot,
                base_artifact=None if full_rebuild else base_artifact,
                added_block_ids=added_block_ids,
                deleted_block_ids=deleted_block_ids,
            )
        except ScanContractError as error:
            raise CompilationFailure(
                f"term_extraction_failed:scan_contract_failed:{error}"
            ) from error
        scan_seconds = time.perf_counter() - scan_started
        occurrence_by_id = {
            occurrence.occurrence_id: occurrence for occurrence in scan_result.occurrences
        }

        model_groups = tuple(
            group for group in scan_result.candidate_groups if group.model_judgement_required
        )
        lexical_judgements = self._lexical_exclusion_judgements(scan_result.candidate_groups)
        self._notify(
            progress,
            "模型判断候选组："
            f"occurrences={len(scan_result.occurrences)} "
            f"groups={len(scan_result.candidate_groups)} "
            f"model_groups={len(model_groups)} "
            f"lexically_excluded={len(lexical_judgements)}",
        )
        term_judgement_started = time.perf_counter()
        try:
            if model_groups:
                model_judgement_result = await self._inducer.judge_candidate_groups(
                    blocks_by_id=blocks_by_id,
                    candidate_groups=model_groups,
                    occurrence_by_id=occurrence_by_id,
                    existing_senses_by_key=self._existing_senses_by_key(
                        existing_links,
                        existing_senses,
                    ),
                    concurrency=concurrency,
                    progress=(
                        lambda completed, total: self._notify(
                            progress, f"模型判断候选组进度：{completed}/{total}"
                        )
                    ),
                )
            else:
                model_judgement_result = JudgementBatchResult((), InductionUsage())
            judgement_result = JudgementBatchResult(
                judgements=model_judgement_result.judgements + lexical_judgements,
                usage=model_judgement_result.usage,
                skipped_block_ids=model_judgement_result.skipped_block_ids,
            )
            term_judgement_seconds = time.perf_counter() - term_judgement_started
        except InductionFailure as error:
            raise CompilationFailure(f"term_extraction_failed:{error}") from error

        skipped_block_stages: dict[str, set[str]] = defaultdict(set)
        for block_id in judgement_result.skipped_block_ids:
            skipped_block_stages[block_id].add("term_judgement")
        skipped_block_ids = tuple(sorted(skipped_block_stages))

        judgement_by_id = {
            judgement.candidate_group_id: judgement for judgement in judgement_result.judgements
        }
        candidate_group_by_id = {
            group.candidate_group_id: group for group in scan_result.candidate_groups
        }
        judgement_by_occurrence = self._expand_group_judgements(
            scan_result.candidate_groups,
            judgement_by_id,
        )
        term_support, term_display = self._term_state(
            pruned_terms,
            scan_result.occurrences,
            judgement_by_occurrence,
        )
        new_drafts = self._build_initial_drafts(
            occurrences=scan_result.occurrences,
            judgements=judgement_by_occurrence,
        )
        drafts = self._merge_existing_identity_drafts(
            self._base_sense_drafts(pruned_links, pruned_senses) + new_drafts
        )
        drafts = self._mark_changed_existing_sense_summaries(
            drafts,
            base_artifact=None if full_rebuild else base_artifact,
        )
        consolidation_started = time.perf_counter()
        (
            drafts,
            consolidation_usage,
            skipped_consolidation_draft_ids,
        ) = await self._consolidate_touched_drafts(
            drafts=drafts,
            touched_keys=touched_keys,
            snapshot=snapshot,
            occurrence_by_id=occurrence_by_id,
            concurrency=concurrency,
            progress=progress,
        )
        skipped_consolidation_draft_id_set = set(skipped_consolidation_draft_ids)
        stale_summary_draft_ids = tuple(
            sorted(
                draft.draft_id
                for draft in drafts
                if draft.draft_id in skipped_consolidation_draft_id_set
                and draft.refresh_summary_required
            )
        )
        stale_summary_draft_id_set = set(stale_summary_draft_ids)
        materializable_drafts = tuple(
            draft for draft in drafts if draft.draft_id not in stale_summary_draft_id_set
        )
        consolidation_seconds = time.perf_counter() - consolidation_started
        materialization_started = time.perf_counter()
        concepts, links, sense_lineage = self._materialize_graph(
            drafts=materializable_drafts,
            base_links=pruned_links,
            occurrence_by_id=occurrence_by_id,
            judgements=judgement_by_occurrence,
            induction_contract_version=current_contract.induction_contract_version,
        )
        materialized_terms = tuple(
            TermRecord(
                normalized_key=key,
                display_text=term_display[key],
                support_block_ids=tuple(sorted(block_ids)),
            )
            for key, block_ids in sorted(term_support.items())
            if block_ids
        )
        base_for_reconciliation = None if full_rebuild else base_artifact
        affected_term_keys = set(touched_keys)
        for draft in drafts:
            if set(draft.normalized_keys) & touched_keys:
                affected_term_keys.update(draft.normalized_keys)
        try:
            reconciliation_plan = build_reconciliation_plan(
                base_artifact=base_for_reconciliation,
                target_snapshot_id=snapshot.snapshot_id,
                induction_contract_version=current_contract.induction_contract_version,
                terms=materialized_terms,
                links=links,
                concepts=concepts,
                sense_lineage=sense_lineage,
                affected_term_keys=affected_term_keys,
            )
            terms, links, concepts = reconciliation_plan.apply(base_for_reconciliation)
            self._validate_product(
                snapshot,
                terms,
                concepts,
                links,
                current_contract.induction_contract_version,
            )
        except ReconciliationError as error:
            raise CompilationFailure(f"reconciliation_plan_invalid:{error}") from error

        diagnostic_records = self._diagnostic_records(
            judgement_result.judgements,
            scan_result.excluded_noise_counts,
            candidate_group_by_id,
        )
        skipped_occurrence_ids_by_block = {
            block_id: tuple(
                sorted(
                    {
                        occurrence_id
                        for group in scan_result.candidate_groups
                        if group.block_id == block_id
                        for occurrence_id in group.occurrence_ids
                    }
                )
            )
            for block_id in skipped_block_ids
        }
        skipped_records = tuple(
            DiagnosticRecord(
                code="candidate_closure_incomplete",
                message="原文块超过输入资源护栏，部分概念候选未完成判断",
                payload={
                    "block_id": block_id,
                    "candidate_closure_complete": False,
                    "skipped_stages": sorted(skipped_block_stages[block_id]),
                },
            )
            for block_id in skipped_block_ids
        )
        artifact_diagnostics = (
            tuple(
                self._artifact_diagnostics(
                    judgement_result.judgements,
                    candidate_group_by_id,
                )
            )
            + tuple(
                ArtifactDiagnostic(
                    code="candidate_closure_incomplete",
                    message=(
                        "原文块在"
                        + "、".join(sorted(skipped_block_stages[block_id]))
                        + "阶段超过输入资源护栏，候选闭包不完整"
                    ),
                    block_id=block_id,
                    occurrence_ids=skipped_occurrence_ids_by_block[block_id],
                )
                for block_id in skipped_block_ids
            )
            + tuple(
                ArtifactDiagnostic(
                    code="candidate_closure_incomplete",
                    message="概念义项归并上下文超过输入资源护栏，候选闭包不完整",
                    normalized_key=normalized_key,
                )
                for normalized_key in sorted(
                    {
                        normalized_key
                        for draft in drafts
                        if draft.draft_id in skipped_consolidation_draft_ids
                        for normalized_key in draft.normalized_keys
                    }
                )
            )
        )
        stats = self._stats(
            terms,
            concepts,
            links,
            scan_result.occurrences,
            scan_result.candidate_groups,
            judgement_result.judgements,
        )
        stats["candidate_closure_incomplete_blocks"] = len(skipped_block_ids)
        stats["candidate_closure_incomplete_term_judgement_blocks"] = len(
            judgement_result.skipped_block_ids
        )
        stats["candidate_closure_incomplete_drafts"] = len(skipped_consolidation_draft_ids)
        stats["stale_summary_suppressed_drafts"] = len(stale_summary_draft_ids)
        stats["rescanned_blocks"] = len(rescanned_block_ids)
        stats["reconciliation_operations"] = len(reconciliation_plan.operations)
        usage = judgement_result.usage.add(consolidation_usage)
        observed_model_ids = self._validated_actual_model_ids(usage)
        contributing_model_ids = self._contributing_actual_model_ids(
            base_artifact=base_for_reconciliation,
            observed_model_ids=observed_model_ids,
        )
        current_contract = self._contract_with_actual_model_ids(
            current_contract,
            contributing_model_ids,
        )
        artifact_without_identity = Stage51Artifact(
            map_id="pending",
            snapshot_id=snapshot.snapshot_id,
            snapshot_manifest_hash=snapshot.manifest_hash,
            base_map_id=(
                base_artifact.map_id if not full_rebuild and base_artifact is not None else None
            ),
            compilation_mode="full" if full_rebuild else "incremental",
            compile_contract=current_contract,
            source_blocks=current_block_index,
            terms=terms,
            term_sense_links=links,
            concept_senses=concepts,
            diagnostics=artifact_diagnostics,
            stats=stats,
        )
        map_hash = artifact_without_identity.calculate_map_hash()
        map_id = f"km51_{map_hash[:16]}"
        artifact = artifact_without_identity.model_copy(
            update={"map_id": map_id, "map_hash": map_hash}
        )
        materialization_seconds = time.perf_counter() - materialization_started
        stage_metrics = (
            self._stage_metric("scan", scan_seconds, InductionUsage()),
            self._stage_metric("term_judgement", term_judgement_seconds, judgement_result.usage),
            self._stage_metric("consolidation", consolidation_seconds, consolidation_usage),
            self._stage_metric(
                "materialization",
                materialization_seconds,
                InductionUsage(),
            ),
            self._stage_metric(
                "total_compile",
                time.perf_counter() - compile_started,
                usage,
            ),
        )
        diagnostics = CompilationDiagnostics(
            snapshot_id=snapshot.snapshot_id,
            map_id=map_id,
            candidate_occurrence_count=len(scan_result.occurrences),
            candidate_group_count=len(scan_result.candidate_groups),
            block_count=len(snapshot.blocks),
            added_block_ids=added_block_ids,
            deleted_block_ids=deleted_block_ids,
            rescanned_block_ids=rescanned_block_ids,
            skipped_block_ids=skipped_block_ids,
            candidate_group_judgements=judgement_result.judgements,
            llm_requests=usage.requests,
            llm_attempts=usage.attempts,
            failed_llm_attempts=usage.failed_attempts,
            retry_attempts=usage.retry_attempts,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
            cache_miss_tokens=usage.cache_miss_tokens,
            actual_model_ids=contributing_model_ids,
            stage_metrics=stage_metrics,
            records=diagnostic_records
            + skipped_records
            + tuple(
                DiagnosticRecord(
                    code="candidate_closure_incomplete",
                    message="概念义项归并上下文超过输入资源护栏，候选闭包不完整",
                    payload={"normalized_key": normalized_key},
                )
                for normalized_key in sorted(
                    {
                        normalized_key
                        for draft in drafts
                        if draft.draft_id in skipped_consolidation_draft_ids
                        for normalized_key in draft.normalized_keys
                    }
                )
            ),
        )
        return artifact, diagnostics

    def _scan_affected_closure(
        self,
        *,
        scanner: CandidateScanner,
        snapshot: SnapshotData,
        base_artifact: Stage51Artifact | None,
        added_block_ids: tuple[str, ...],
        deleted_block_ids: tuple[str, ...],
    ) -> tuple[
        ScanResult,
        set[str],
        tuple[str, ...],
        dict[str, TermRecord],
        list[TermSenseLink],
        dict[str, ConceptSense],
    ]:
        """计算扫描与基础状态剔除的共同影响闭包，直到两者都不再扩张。"""

        blocks_by_id = {block.block_id: block for block in snapshot.blocks}
        ordered_block_ids = tuple(block.block_id for block in snapshot.blocks)
        added = set(added_block_ids)
        scanned: set[str] = set()
        recomputed_existing: set[str] = set()
        occurrences: list[CandidateOccurrence] = []
        candidate_groups: list[CandidateGroup] = []
        block_statuses: list[ScanBlockStatus] = []
        excluded_noise_counts: dict[str, int] = {}
        lexical_exclusion_counts: dict[str, int] = {}
        touched_keys: set[str] = set()

        while True:
            invalid_block_ids = set(deleted_block_ids) | recomputed_existing
            pruned_terms, pruned_links, pruned_senses = self._prune_base_state(
                base_artifact,
                invalid_block_ids,
            )
            touched_keys.update(self._keys_with_removed_support(base_artifact, invalid_block_ids))
            affected_keys = self._expand_affected_keys(
                touched_keys,
                () if base_artifact is None else base_artifact.term_sense_links,
            )
            affected_block_ids = set(added)
            for term in pruned_terms.values():
                if term.normalized_key in affected_keys:
                    affected_block_ids.update(term.support_block_ids)
            for link in pruned_links:
                if link.normalized_key in affected_keys:
                    affected_block_ids.update(link.support_block_ids)
            pending = tuple(
                block_id
                for block_id in ordered_block_ids
                if block_id in affected_block_ids and block_id not in scanned
            )
            if not pending:
                occurrences.sort(
                    key=lambda item: (
                        item.block_ordinal,
                        item.start,
                        item.end,
                        item.normalized_key,
                    )
                )
                candidate_groups.sort(
                    key=lambda item: (
                        item.block_ordinal,
                        item.first_start,
                        item.normalized_key,
                        item.candidate_group_id,
                    )
                )
                return (
                    ScanResult(
                        contract=scanner.contract,
                        occurrences=tuple(occurrences),
                        candidate_groups=tuple(candidate_groups),
                        block_statuses=tuple(block_statuses),
                        excluded_noise_counts=dict(sorted(excluded_noise_counts.items())),
                        lexical_exclusion_counts=dict(sorted(lexical_exclusion_counts.items())),
                        candidate_set_hash=canonical_hash(
                            [item.model_dump(mode="json") for item in occurrences]
                        ),
                    ),
                    affected_keys,
                    tuple(
                        block_id
                        for block_id in ordered_block_ids
                        if block_id in recomputed_existing
                    ),
                    pruned_terms,
                    pruned_links,
                    pruned_senses,
                )

            for block_id in pending:
                result = scanner.scan(snapshot.snapshot_id, (blocks_by_id[block_id],))
                self._validate_block_scan_result(
                    snapshot=snapshot,
                    scanner=scanner,
                    block=blocks_by_id[block_id],
                    result=result,
                )
                scanned.add(block_id)
                if block_id not in added:
                    recomputed_existing.add(block_id)
                occurrences.extend(result.occurrences)
                candidate_groups.extend(result.candidate_groups)
                block_statuses.extend(result.block_statuses)
                for reason, count in result.excluded_noise_counts.items():
                    excluded_noise_counts[reason] = excluded_noise_counts.get(reason, 0) + count
                for reason, count in result.lexical_exclusion_counts.items():
                    lexical_exclusion_counts[reason] = (
                        lexical_exclusion_counts.get(reason, 0) + count
                    )
                touched_keys.update(occurrence.normalized_key for occurrence in result.occurrences)

    @staticmethod
    def _validate_block_scan_result(
        *,
        snapshot: SnapshotData,
        scanner: CandidateScanner,
        block: SourceBlock,
        result: ScanResult,
    ) -> None:
        """校验扫描结果中的状态、原文范围、派生身份和候选组闭包。"""

        block_id = block.block_id
        if result.contract != scanner.contract:
            raise ScanContractError(
                f"scan result contract differs from frozen scanner contract: {block_id}"
            )
        if len(result.block_statuses) != 1:
            raise ScanContractError(
                f"scan result must contain exactly one block status: {block_id}"
            )
        block_status = result.block_statuses[0]
        if (
            block_status.block_id != block_id
            or block_status.content_hash != block.content_hash
            or block_status.status != "succeeded"
            or block_status.error_code is not None
        ):
            raise ScanContractError(f"scan block did not succeed: {block_id}")
        if block_status.candidate_count != len(result.occurrences):
            raise ScanContractError(
                f"scan block candidate count does not match occurrences: {block_id}"
            )

        occurrence_by_id = {
            occurrence.occurrence_id: occurrence for occurrence in result.occurrences
        }
        if len(occurrence_by_id) != len(result.occurrences):
            raise ScanContractError(f"scan occurrence IDs are duplicated: {block_id}")
        expected_occurrence_order = tuple(
            sorted(
                result.occurrences,
                key=lambda item: (
                    item.block_ordinal,
                    item.start,
                    item.end,
                    item.normalized_key,
                ),
            )
        )
        if result.occurrences != expected_occurrence_order:
            raise ScanContractError(
                f"scan occurrences are not deterministically ordered: {block_id}"
            )
        for occurrence in result.occurrences:
            if (
                occurrence.snapshot_id != snapshot.snapshot_id
                or occurrence.doc_id != block.doc_id
                or occurrence.block_id != block_id
                or occurrence.block_index != block.block_index
                or occurrence.block_ordinal != block.block_ordinal
                or occurrence.block_content_hash != block.content_hash
                or occurrence.scan_contract_hash != scanner.contract.contract_hash
            ):
                raise ScanContractError(f"scan occurrence identity is inconsistent: {block_id}")
            if (
                occurrence.end > len(block.text)
                or occurrence.display_text != block.text[occurrence.start : occurrence.end]
                or normalize_lexical_key(occurrence.display_text) != occurrence.normalized_key
            ):
                raise ScanContractError(f"scan occurrence text range is inconsistent: {block_id}")
            expected_occurrence_id = deterministic_identifier(
                "occ",
                (
                    occurrence.snapshot_id,
                    occurrence.block_id,
                    occurrence.block_content_hash,
                    occurrence.start,
                    occurrence.end,
                    occurrence.normalized_key,
                    occurrence.scan_contract_hash,
                ),
            )
            if occurrence.occurrence_id != expected_occurrence_id:
                raise ScanContractError(f"scan occurrence ID is not canonical: {block_id}")

        grouped_occurrence_ids = tuple(
            occurrence_id
            for group in result.candidate_groups
            for occurrence_id in group.occurrence_ids
        )
        if sorted(grouped_occurrence_ids) != sorted(occurrence_by_id):
            raise ScanContractError(
                f"scan candidate groups do not cover occurrences exactly once: {block_id}"
            )
        expected_group_order = tuple(
            sorted(
                result.candidate_groups,
                key=lambda item: (
                    item.block_ordinal,
                    item.first_start,
                    item.normalized_key,
                    item.candidate_group_id,
                ),
            )
        )
        if result.candidate_groups != expected_group_order:
            raise ScanContractError(f"scan candidate groups are not deterministic: {block_id}")
        for group in result.candidate_groups:
            if (
                group.snapshot_id != snapshot.snapshot_id
                or group.doc_id != block.doc_id
                or group.block_id != block_id
                or group.block_index != block.block_index
                or group.block_ordinal != block.block_ordinal
                or group.block_content_hash != block.content_hash
                or group.scan_contract_hash != scanner.contract.contract_hash
            ):
                raise ScanContractError(f"scan group identity is inconsistent: {block_id}")
            try:
                members = [occurrence_by_id[item] for item in group.occurrence_ids]
            except KeyError as error:
                raise ScanContractError(
                    f"scan group references an unknown occurrence: {block_id}"
                ) from error
            ordered_members = sorted(
                members,
                key=lambda item: (item.start, item.end, item.occurrence_id),
            )
            first = ordered_members[0]
            if (
                group.normalized_key != first.normalized_key
                or group.display_text != first.display_text
                or group.first_start != first.start
                or group.occurrence_ids != tuple(item.occurrence_id for item in ordered_members)
                or any(
                    member.normalized_key != group.normalized_key
                    or member.block_id != group.block_id
                    or member.block_content_hash != group.block_content_hash
                    for member in members
                )
            ):
                raise ScanContractError(f"scan group text identity is inconsistent: {block_id}")
            expected_group_id = deterministic_identifier(
                "cg51",
                (
                    group.snapshot_id,
                    group.block_id,
                    group.block_content_hash,
                    group.normalized_key,
                    group.scan_contract_hash,
                ),
            )
            if group.candidate_group_id != expected_group_id:
                raise ScanContractError(f"scan group ID is not canonical: {block_id}")
            expected_model_required = any(member.model_judgement_required for member in members)
            expected_exclusion_reasons = (
                ()
                if expected_model_required
                else tuple(
                    sorted(
                        {
                            member.lexical_exclusion_reason
                            for member in members
                            if member.lexical_exclusion_reason is not None
                        }
                    )
                )
            )
            if (
                group.model_judgement_required != expected_model_required
                or group.lexical_exclusion_reasons != expected_exclusion_reasons
            ):
                raise ScanContractError(f"scan group exclusion state is inconsistent: {block_id}")

        expected_candidate_hash = canonical_hash(
            [item.model_dump(mode="json") for item in result.occurrences]
        )
        if result.candidate_set_hash != expected_candidate_hash:
            raise ScanContractError(f"scan candidate set hash is inconsistent: {block_id}")
        expected_lexical_exclusion_counts: dict[str, int] = {}
        for occurrence in result.occurrences:
            reason = occurrence.lexical_exclusion_reason
            if reason is not None:
                expected_lexical_exclusion_counts[reason] = (
                    expected_lexical_exclusion_counts.get(reason, 0) + 1
                )
        if result.lexical_exclusion_counts != expected_lexical_exclusion_counts:
            raise ScanContractError(f"scan lexical exclusion counts are inconsistent: {block_id}")
        if any(
            not reason or count < 0
            for counts in (
                result.excluded_noise_counts,
                result.lexical_exclusion_counts,
            )
            for reason, count in counts.items()
        ):
            raise ScanContractError(f"scan exclusion counts are invalid: {block_id}")

    @staticmethod
    def _expand_affected_keys(
        touched_keys: set[str],
        links: Sequence[TermSenseLink],
    ) -> set[str]:
        """沿概念义项—术语关联扩展影响键，使同一概念的别名一起进入工作集。"""

        result = set(touched_keys)
        while True:
            affected_sense_ids = {
                link.concept_sense_id for link in links if link.normalized_key in result
            }
            expanded = result | {
                link.normalized_key for link in links if link.concept_sense_id in affected_sense_ids
            }
            if expanded == result:
                return result
            result = expanded

    def _compile_contract(
        self,
        scanner: CandidateScanner,
        snapshot: SnapshotData,
    ) -> CompileContract:
        max_retries = int(getattr(self._inducer, "max_retries", 0))
        document_versions = tuple(
            sorted(snapshot.documents, key=lambda item: (item.doc_id, item.doc_version))
        )
        induction_payload = {
            "contract_version": INDUCTION_CONTRACT_VERSION,
            "model_id": self._inducer.model_id,
            "term_sense_prompt_version": TERM_SENSE_PROMPT_VERSION,
            "consolidation_prompt_version": CONSOLIDATION_PROMPT_VERSION,
            "summary_prompt_version": SUMMARY_PROMPT_VERSION,
            "system_prompt_hash": canonical_hash(
                {
                    "term_sense": TERM_SENSE_SYSTEM_PROMPT,
                    "consolidation": CONSOLIDATION_SYSTEM_PROMPT,
                    "summary": SUMMARY_SYSTEM_PROMPT,
                }
            ),
            "context_assembly_version": CONTEXT_ASSEMBLY_VERSION,
            "max_input_chars": self._max_input_chars,
            "max_output_tokens": self._max_output_tokens,
            "consolidation_output_tokens_per_draft": (CONSOLIDATION_OUTPUT_TOKENS_PER_DRAFT),
            "max_retries": max_retries,
        }
        contract_without_hashes = CompileContract(
            contract_id="pending",
            contract_hash="pending",
            compatibility_hash="pending",
            contract_version=COMPILE_CONTRACT_VERSION,
            compiler_version=COMPILER_VERSION,
            snapshot_id=snapshot.snapshot_id,
            snapshot_manifest_hash=snapshot.manifest_hash,
            document_versions=document_versions,
            scan_contract_hash=scanner.contract.contract_hash,
            induction_contract_version=INDUCTION_CONTRACT_VERSION,
            induction_contract_hash=canonical_hash(induction_payload),
            model_id=self._inducer.model_id,
            term_sense_prompt_version=TERM_SENSE_PROMPT_VERSION,
            consolidation_prompt_version=CONSOLIDATION_PROMPT_VERSION,
            summary_prompt_version=SUMMARY_PROMPT_VERSION,
            context_assembly_version=CONTEXT_ASSEMBLY_VERSION,
            max_candidate_groups_per_judgement_request=(MAX_CANDIDATE_GROUPS_PER_JUDGEMENT_REQUEST),
            max_input_chars=self._max_input_chars,
            max_output_tokens=self._max_output_tokens,
            consolidation_output_tokens_per_draft=(CONSOLIDATION_OUTPUT_TOKENS_PER_DRAFT),
            max_retries=max_retries,
        )
        contract_with_compatibility = contract_without_hashes.model_copy(
            update={"compatibility_hash": contract_without_hashes.calculate_compatibility_hash()}
        )
        contract_hash = contract_with_compatibility.calculate_contract_hash()
        return contract_with_compatibility.model_copy(
            update={
                "contract_id": f"cc51_{contract_hash[:16]}",
                "contract_hash": contract_hash,
            }
        )

    @staticmethod
    def _contracts_compatible(
        previous: CompileContract,
        current: CompileContract,
    ) -> bool:
        return previous.compatibility_hash == current.compatibility_hash

    @staticmethod
    def _calculate_block_delta(
        base_artifact: Stage51Artifact | None,
        current_blocks: Sequence[SourceBlockIdentity],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if base_artifact is None:
            return tuple(block.block_id for block in current_blocks), tuple()
        previous = {block.block_id: block.content_hash for block in base_artifact.source_blocks}
        current = {block.block_id: block.content_hash for block in current_blocks}
        identity_conflicts = tuple(
            block_id
            for block_id, content_hash in current.items()
            if block_id in previous and previous[block_id] != content_hash
        )
        if identity_conflicts:
            raise CompilationFailure(
                "snapshot_block_identity_conflict:" + ",".join(sorted(identity_conflicts))
            )
        added = tuple(block.block_id for block in current_blocks if block.block_id not in previous)
        deleted = tuple(block_id for block_id in previous if block_id not in current)
        return added, deleted

    @staticmethod
    def _prune_base_state(
        base_artifact: Stage51Artifact | None,
        invalid_block_ids: set[str],
    ) -> tuple[dict[str, TermRecord], list[TermSenseLink], dict[str, ConceptSense]]:
        if base_artifact is None:
            return {}, [], {}
        terms: dict[str, TermRecord] = {}
        for term in base_artifact.terms:
            support = set(term.support_block_ids) - invalid_block_ids
            if support:
                terms[term.normalized_key] = term.model_copy(
                    update={"support_block_ids": tuple(sorted(support))}
                )
        links: list[TermSenseLink] = []
        for link in base_artifact.term_sense_links:
            support = set(link.support_block_ids) - invalid_block_ids
            if support:
                links.append(link.model_copy(update={"support_block_ids": tuple(sorted(support))}))
        active_sense_ids = {link.concept_sense_id for link in links}
        senses = {
            sense.concept_sense_id: sense
            for sense in base_artifact.concept_senses
            if sense.concept_sense_id in active_sense_ids
        }
        support_by_sense: dict[str, set[str]] = defaultdict(set)
        for link in links:
            support_by_sense[link.concept_sense_id].update(link.support_block_ids)
        senses = {
            sense_id: sense.model_copy(
                update={"support_block_ids": tuple(sorted(support_by_sense[sense_id]))}
            )
            for sense_id, sense in senses.items()
        }
        return terms, links, senses

    @staticmethod
    def _existing_senses_by_key(
        links: Sequence[TermSenseLink],
        senses: Mapping[str, ConceptSense],
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for link in links:
            identity = (link.normalized_key, link.concept_sense_id)
            sense = senses.get(link.concept_sense_id)
            if identity in seen or sense is None:
                continue
            seen.add(identity)
            result[link.normalized_key].append(
                {
                    "concept_sense_id": sense.concept_sense_id,
                    "summary": sense.summary,
                    "summary_basis": sense.summary_basis.value,
                    "support_block_ids": list(sense.support_block_ids),
                }
            )
        return {key: tuple(value) for key, value in result.items()}

    @staticmethod
    def _expand_group_judgements(
        groups: Sequence[CandidateGroup],
        judgements: Mapping[str, CandidateGroupJudgement],
    ) -> dict[str, CandidateGroupJudgement]:
        """把组级判断展开到位置标识；位置只用于本地证据图校验。"""

        result: dict[str, CandidateGroupJudgement] = {}
        for group in groups:
            judgement = judgements[group.candidate_group_id]
            for occurrence_id in group.occurrence_ids:
                if occurrence_id in result:
                    raise CompilationFailure("occurrence_belongs_to_multiple_candidate_groups")
                result[occurrence_id] = judgement
        return result

    @staticmethod
    def _term_state(
        base_terms: Mapping[str, TermRecord],
        occurrences: Sequence[CandidateOccurrence],
        judgements: Mapping[str, CandidateGroupJudgement],
    ) -> tuple[dict[str, set[str]], dict[str, str]]:
        support = {key: set(term.support_block_ids) for key, term in base_terms.items()}
        display = {key: term.display_text for key, term in base_terms.items()}
        for occurrence in occurrences:
            if judgements[occurrence.occurrence_id].term_decision is not TermDecision.ACCEPTED:
                continue
            support.setdefault(occurrence.normalized_key, set()).add(occurrence.block_id)
            display.setdefault(occurrence.normalized_key, occurrence.display_text)
        return support, display

    @staticmethod
    def _base_sense_drafts(
        links: Sequence[TermSenseLink],
        senses: Mapping[str, ConceptSense],
    ) -> list[TermSenseDraft]:
        links_by_sense: dict[str, list[TermSenseLink]] = defaultdict(list)
        for link in links:
            links_by_sense[link.concept_sense_id].append(link)
        return [
            TermSenseDraft(
                draft_id=f"base:{sense_id}",
                normalized_keys=tuple(
                    sorted({link.normalized_key for link in links_by_sense.get(sense_id, ())})
                ),
                occurrence_ids=tuple(),
                support_block_ids=sense.support_block_ids,
                summary=sense.summary,
                summary_basis=sense.summary_basis,
                existing_sense_id=sense_id,
                previous_sense_ids=(sense_id,),
                refresh_summary_required=True,
                source="base_map",
            )
            for sense_id, sense in sorted(senses.items())
        ]

    @staticmethod
    def _merge_existing_identity_drafts(drafts: Sequence[TermSenseDraft]) -> list[TermSenseDraft]:
        grouped: dict[str, list[TermSenseDraft]] = defaultdict(list)
        result: list[TermSenseDraft] = []
        for draft in drafts:
            if draft.existing_sense_id:
                grouped[draft.existing_sense_id].append(draft)
            else:
                result.append(draft)
        for sense_id, members in sorted(grouped.items()):
            result.append(
                TermSenseDraft(
                    draft_id=f"base:{sense_id}",
                    normalized_keys=tuple(
                        sorted({key for item in members for key in item.normalized_keys})
                    ),
                    occurrence_ids=tuple(
                        sorted({occ for item in members for occ in item.occurrence_ids})
                    ),
                    support_block_ids=tuple(
                        sorted({block for item in members for block in item.support_block_ids})
                    ),
                    summary=members[0].summary,
                    summary_basis=members[0].summary_basis,
                    existing_sense_id=sense_id,
                    previous_sense_ids=tuple(
                        sorted(
                            {
                                previous_id
                                for item in members
                                for previous_id in item.previous_sense_ids
                            }
                        )
                    ),
                    refresh_summary_required=any(item.refresh_summary_required for item in members),
                    source="base_map"
                    if any(item.source == "base_map" for item in members)
                    else "model",
                )
            )
        return result

    @staticmethod
    def _mark_changed_existing_sense_summaries(
        drafts: Sequence[TermSenseDraft],
        *,
        base_artifact: Stage51Artifact | None,
    ) -> list[TermSenseDraft]:
        """支持集合变化时强制重写摘要，即使旧义项曾在剔除阶段失去全部直接支持。"""

        if base_artifact is None:
            return list(drafts)
        previous_support = {
            sense.concept_sense_id: sense.support_block_ids
            for sense in base_artifact.concept_senses
        }
        return [
            draft.model_copy(update={"refresh_summary_required": True})
            if draft.existing_sense_id is not None
            and previous_support.get(draft.existing_sense_id) != draft.support_block_ids
            else draft
            for draft in drafts
        ]

    @staticmethod
    def _build_initial_drafts(
        *,
        occurrences: Sequence[CandidateOccurrence],
        judgements: Mapping[str, CandidateGroupJudgement],
    ) -> list[TermSenseDraft]:
        grouped: dict[tuple[str, str], list[CandidateOccurrence]] = defaultdict(list)
        for occurrence in occurrences:
            judgement = judgements[occurrence.occurrence_id]
            if judgement.sense_decision is not SenseDecision.RESOLVED:
                continue
            grouped[(occurrence.block_id, occurrence.normalized_key)].append(occurrence)

        drafts: list[TermSenseDraft] = []
        for (_, normalized_key), members in sorted(grouped.items()):
            existing_ids: set[str] = set()
            for occurrence in members:
                judgement = judgements[occurrence.occurrence_id]
                if judgement.existing_sense_id:
                    existing_ids.add(judgement.existing_sense_id)
            occurrence_ids = tuple(sorted(item.occurrence_id for item in members))
            drafts.append(
                TermSenseDraft(
                    draft_id=deterministic_identifier("draft", occurrence_ids),
                    normalized_keys=(normalized_key,),
                    occurrence_ids=occurrence_ids,
                    support_block_ids=tuple(sorted({item.block_id for item in members})),
                    existing_sense_id=next(iter(existing_ids)) if len(existing_ids) == 1 else None,
                    previous_sense_ids=tuple(sorted(existing_ids)),
                    source="model",
                    refresh_summary_required=False,
                )
            )
        return drafts

    async def _consolidate_touched_drafts(
        self,
        *,
        drafts: Sequence[TermSenseDraft],
        touched_keys: set[str],
        snapshot: SnapshotData,
        occurrence_by_id: Mapping[str, CandidateOccurrence],
        concurrency: int,
        progress: Callable[[str], None] | None,
    ) -> tuple[list[TermSenseDraft], InductionUsage, tuple[str, ...]]:
        untouched = [draft for draft in drafts if not (set(draft.normalized_keys) & touched_keys)]
        working = [draft for draft in drafts if set(draft.normalized_keys) & touched_keys]
        if not working:
            return untouched, InductionUsage(), ()
        blocks_by_id = {block.block_id: block for block in snapshot.blocks}
        self._notify(progress, f"义项全局归并：drafts={len(working)}")
        try:
            result = await self._inducer.consolidate_drafts(
                drafts=working,
                evidence_by_draft_id=self._evidence_by_draft(
                    working,
                    blocks_by_id,
                    occurrence_by_id,
                ),
                concurrency=concurrency,
            )
        except InductionFailure as error:
            raise CompilationFailure(f"term_extraction_failed:{error}") from error
        return (
            untouched + list(result.drafts),
            result.usage,
            result.skipped_draft_ids,
        )

    @staticmethod
    def _stage_metric(
        stage: StageName,
        wall_seconds: float,
        usage: InductionUsage,
    ) -> StageMetric:
        return StageMetric(
            stage=stage,
            wall_seconds=max(0.0, wall_seconds),
            requests=usage.requests,
            attempts=usage.attempts,
            failed_attempts=usage.failed_attempts,
            retry_attempts=usage.retry_attempts,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
            cache_miss_tokens=usage.cache_miss_tokens,
            actual_model_ids=usage.actual_model_ids,
            failure_details=usage.failure_details,
        )

    @staticmethod
    def _validated_actual_model_ids(usage: InductionUsage) -> tuple[str, ...]:
        """校验一次编译只使用一个供应商实际模型，并返回可追溯标识。"""

        actual_model_ids = usage.actual_model_ids
        if len(actual_model_ids) > 1:
            raise CompilationFailure(
                "model_identity_changed_during_compile:" + ",".join(actual_model_ids)
            )
        return actual_model_ids

    @staticmethod
    def _contributing_actual_model_ids(
        *,
        base_artifact: Stage51Artifact | None,
        observed_model_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """合并基础地图与本次响应的模型来源，并阻断静默换模型。"""

        base_model_ids = (
            () if base_artifact is None else base_artifact.compile_contract.actual_model_ids
        )
        if base_model_ids and observed_model_ids and base_model_ids != observed_model_ids:
            raise CompilationFailure(
                "model_identity_changed_from_base:"
                f"{','.join(base_model_ids) or '-'}->{','.join(observed_model_ids) or '-'}"
            )
        return tuple(sorted(set(base_model_ids) | set(observed_model_ids)))

    @staticmethod
    def _contract_with_actual_model_ids(
        contract: CompileContract,
        actual_model_ids: tuple[str, ...],
    ) -> CompileContract:
        """把运行后观测到的模型来源写入完整契约，同时保持兼容哈希口径。"""

        with_actual_model_ids = contract.model_copy(update={"actual_model_ids": actual_model_ids})
        with_compatibility_hash = with_actual_model_ids.model_copy(
            update={"compatibility_hash": with_actual_model_ids.calculate_compatibility_hash()}
        )
        contract_hash = with_compatibility_hash.calculate_contract_hash()
        return with_compatibility_hash.model_copy(
            update={
                "contract_id": f"cc51_{contract_hash[:16]}",
                "contract_hash": contract_hash,
            }
        )

    @staticmethod
    def _connected_draft_components(
        drafts: Sequence[TermSenseDraft],
    ) -> list[list[TermSenseDraft]]:
        parent = list(range(len(drafts)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        by_key: dict[str, list[int]] = defaultdict(list)
        for index, draft in enumerate(drafts):
            for key in draft.normalized_keys:
                by_key[key].append(index)
        for indexes in by_key.values():
            for index in indexes[1:]:
                union(indexes[0], index)
        grouped: dict[int, list[TermSenseDraft]] = defaultdict(list)
        for index, draft in enumerate(drafts):
            grouped[find(index)].append(draft)
        return [
            sorted(component, key=lambda item: item.draft_id)
            for _, component in sorted(grouped.items())
        ]

    @staticmethod
    def _evidence_by_draft(
        drafts: Sequence[TermSenseDraft],
        blocks_by_id: Mapping[str, SourceBlock],
        occurrence_by_id: Mapping[str, CandidateOccurrence],
    ) -> dict[str, tuple[str, ...]]:
        evidence: dict[str, tuple[str, ...]] = {}
        for draft in drafts:
            snippets = [
                blocks_by_id[block_id].text
                for block_id in draft.support_block_ids
                if block_id in blocks_by_id
            ]
            evidence[draft.draft_id] = tuple(dict.fromkeys(snippets))
        return evidence

    @staticmethod
    def _context_snippet(text: str, start: int, end: int, radius: int = 240) -> str:
        return text[max(0, start - radius) : min(len(text), end + radius)]

    @staticmethod
    def _keys_with_removed_support(
        base_artifact: Stage51Artifact | None,
        invalid_block_ids: set[str],
    ) -> set[str]:
        if base_artifact is None:
            return set()
        affected = {
            term.normalized_key
            for term in base_artifact.terms
            if set(term.support_block_ids) & invalid_block_ids
        }
        affected.update(
            link.normalized_key
            for link in base_artifact.term_sense_links
            if set(link.support_block_ids) & invalid_block_ids
        )
        return affected

    @staticmethod
    def _materialize_graph(
        *,
        drafts: Sequence[TermSenseDraft],
        base_links: Sequence[TermSenseLink],
        occurrence_by_id: Mapping[str, CandidateOccurrence],
        judgements: Mapping[str, CandidateGroupJudgement],
        induction_contract_version: str,
    ) -> tuple[
        tuple[ConceptSense, ...],
        tuple[TermSenseLink, ...],
        dict[str, tuple[str, ...]],
    ]:
        direct_support: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        occurrence_owner: dict[str, str] = {}
        for draft in drafts:
            for occurrence_id in draft.occurrence_ids:
                previous_owner = occurrence_owner.setdefault(occurrence_id, draft.draft_id)
                if previous_owner != draft.draft_id:
                    raise CompilationFailure("occurrence_belongs_to_multiple_senses")
                occurrence = occurrence_by_id.get(occurrence_id)
                judgement = judgements.get(occurrence_id)
                if occurrence is None or judgement is None:
                    raise CompilationFailure("sense_references_unknown_occurrence")
                if judgement.term_decision is not TermDecision.ACCEPTED:
                    raise CompilationFailure("sense_references_rejected_term")
                if judgement.sense_decision is SenseDecision.RESOLVED:
                    direct_support[draft.draft_id][occurrence.normalized_key].add(
                        occurrence.block_id
                    )
                else:
                    raise CompilationFailure("sense_contains_unresolved_occurrence")

        base_links_by_sense: dict[str, list[TermSenseLink]] = defaultdict(list)
        for link in base_links:
            base_links_by_sense[link.concept_sense_id].append(link)
        for draft in drafts:
            for previous_sense_id in draft.previous_sense_ids:
                for link in base_links_by_sense.get(previous_sense_id, ()):
                    direct_support[draft.draft_id][link.normalized_key].update(
                        link.support_block_ids
                    )

        concepts: list[ConceptSense] = []
        links: list[TermSenseLink] = []
        sense_lineage: dict[str, tuple[str, ...]] = {}
        for draft in sorted(drafts, key=lambda item: item.draft_id):
            draft_links: list[TermSenseLink] = []
            for normalized_key in sorted(draft.normalized_keys):
                support_blocks = tuple(
                    sorted(direct_support[draft.draft_id].get(normalized_key, ()))
                )
                if not support_blocks:
                    continue
                draft_links.append(
                    TermSenseLink(
                        normalized_key=normalized_key,
                        concept_sense_id="pending",
                        support_block_ids=support_blocks,
                        basis_type=LinkBasis.DIRECT_USE,
                    )
                )
            if not draft_links:
                continue
            support_block_ids = tuple(
                sorted({block_id for link in draft_links for block_id in link.support_block_ids})
            )
            if draft.summary is None or draft.summary_basis is None:
                raise CompilationFailure("sense_draft_has_no_final_summary")
            identity_payload = {
                "summary": draft.summary,
                "summary_basis": draft.summary_basis.value,
                "normalized_keys": sorted(draft.normalized_keys),
                "support_block_ids": support_block_ids,
                "induction_contract_version": induction_contract_version,
            }
            concept_sense_id = deterministic_identifier(
                "sense51",
                (canonical_hash(identity_payload),),
            )
            concepts.append(
                ConceptSense(
                    concept_sense_id=concept_sense_id,
                    summary=draft.summary,
                    summary_basis=draft.summary_basis,
                    support_block_ids=support_block_ids,
                    induction_contract_version=induction_contract_version,
                )
            )
            sense_lineage[concept_sense_id] = draft.previous_sense_ids
            links.extend(
                link.model_copy(update={"concept_sense_id": concept_sense_id})
                for link in draft_links
            )
        links.sort(
            key=lambda item: (
                item.normalized_key,
                item.concept_sense_id,
                item.basis_type.value,
            )
        )
        concepts.sort(key=lambda item: item.concept_sense_id)
        return tuple(concepts), tuple(links), sense_lineage

    @staticmethod
    def _validate_product(
        snapshot: SnapshotData,
        terms: Sequence[TermRecord],
        concepts: Sequence[ConceptSense],
        links: Sequence[TermSenseLink],
        induction_contract_version: str,
    ) -> None:
        blocks_by_id = {block.block_id: block for block in snapshot.blocks}
        terms_by_key = {term.normalized_key: term for term in terms}
        concepts_by_id = {sense.concept_sense_id: sense for sense in concepts}
        normalized_text_by_block = {
            block_id: normalize_lexical_key(block.text) for block_id, block in blocks_by_id.items()
        }
        if len(terms_by_key) != len(terms) or len(concepts_by_id) != len(concepts):
            raise CompilationFailure("published_identity_is_duplicated")

        for term in terms:
            if not term.normalized_key or not term.support_block_ids:
                raise CompilationFailure("term_has_no_support")
            if normalize_lexical_key(term.normalized_key) != term.normalized_key:
                raise CompilationFailure("term_normalized_key_is_invalid")
            if any(block_id not in blocks_by_id for block_id in term.support_block_ids):
                raise CompilationFailure("term_references_unknown_block")
            if any(
                term.normalized_key not in normalized_text_by_block[block_id]
                for block_id in term.support_block_ids
            ):
                raise CompilationFailure("term_support_block_does_not_contain_term")

        seen_links: set[tuple[str, str, LinkBasis]] = set()
        support_by_sense: dict[str, set[str]] = defaultdict(set)
        for link in links:
            identity = (link.normalized_key, link.concept_sense_id, link.basis_type)
            if identity in seen_links:
                raise CompilationFailure("term_sense_link_is_duplicated")
            seen_links.add(identity)
            term = terms_by_key.get(link.normalized_key)
            sense = concepts_by_id.get(link.concept_sense_id)
            if term is None or sense is None or not link.support_block_ids:
                raise CompilationFailure("term_sense_link_is_dangling")
            if not set(link.support_block_ids) <= set(term.support_block_ids):
                raise CompilationFailure("term_sense_link_exceeds_term_support")
            for block_id in link.support_block_ids:
                block = blocks_by_id.get(block_id)
                if block is None:
                    raise CompilationFailure("term_sense_link_references_unknown_block")
                if (
                    link.basis_type is LinkBasis.DIRECT_USE
                    and link.normalized_key not in (normalized_text_by_block[block_id])
                ):
                    raise CompilationFailure("direct_link_term_not_found_in_block")
            support_by_sense[link.concept_sense_id].update(link.support_block_ids)

        for sense in concepts:
            if not sense.summary.strip() or not sense.support_block_ids:
                raise CompilationFailure("concept_sense_has_no_summary_or_support")
            if sense.induction_contract_version != induction_contract_version:
                raise CompilationFailure("concept_sense_induction_contract_mismatch")
            if any(block_id not in blocks_by_id for block_id in sense.support_block_ids):
                raise CompilationFailure("concept_sense_references_unknown_block")
            if support_by_sense.get(sense.concept_sense_id) != set(sense.support_block_ids):
                raise CompilationFailure("concept_sense_support_closure_mismatch")
        if set(support_by_sense) != set(concepts_by_id):
            raise CompilationFailure("concept_sense_without_link_would_be_published")

    @staticmethod
    def _artifact_diagnostics(
        judgements: Sequence[CandidateGroupJudgement],
        groups: Mapping[str, CandidateGroup],
    ) -> list[ArtifactDiagnostic]:
        diagnostics: list[ArtifactDiagnostic] = []
        for judgement in judgements:
            group = groups[judgement.candidate_group_id]
            if judgement.term_decision is TermDecision.NOISE:
                continue
            if judgement.term_decision is TermDecision.UNCERTAIN:
                diagnostics.append(
                    ArtifactDiagnostic(
                        code="term_uncertain",
                        message="术语资格判断不确定",
                        block_id=group.block_id,
                        occurrence_ids=group.occurrence_ids,
                    )
                )
            elif judgement.sense_decision is SenseDecision.MENTION_ONLY:
                diagnostics.append(
                    ArtifactDiagnostic(
                        code="concept_sense_mention_only",
                        message="术语成立，但当前上下文不足以形成概念义项",
                        block_id=group.block_id,
                        occurrence_ids=group.occurrence_ids,
                    )
                )
            elif judgement.sense_decision is SenseDecision.UNCERTAIN:
                diagnostics.append(
                    ArtifactDiagnostic(
                        code="concept_sense_uncertain",
                        message="当前上下文不足以确定概念义项",
                        block_id=group.block_id,
                        occurrence_ids=group.occurrence_ids,
                    )
                )
        return diagnostics

    @staticmethod
    def _lexical_exclusion_judgements(
        groups: Sequence[CandidateGroup],
    ) -> tuple[CandidateGroupJudgement, ...]:
        """把确定性词法排除转换为组级噪声判断，同时保留候选身份和原因。"""

        return tuple(
            CandidateGroupJudgement(
                candidate_group_id=group.candidate_group_id,
                term_decision=TermDecision.NOISE,
                sense_decision=SenseDecision.UNCERTAIN,
                reason=("确定性词法排除：" + "、".join(group.lexical_exclusion_reasons)),
            )
            for group in groups
            if not group.model_judgement_required
        )

    @staticmethod
    def _diagnostic_records(
        judgements: Sequence[CandidateGroupJudgement],
        excluded_noise_counts: Mapping[str, int],
        groups: Mapping[str, CandidateGroup],
    ) -> tuple[DiagnosticRecord, ...]:
        records = [
            DiagnosticRecord(
                code=f"scan_noise_excluded_{reason}",
                message="候选扫描前屏蔽的噪声片段",
                payload={"count": count},
            )
            for reason, count in sorted(excluded_noise_counts.items())
        ]
        for judgement in judgements:
            group = groups[judgement.candidate_group_id]
            if judgement.term_decision is TermDecision.NOISE and not group.model_judgement_required:
                code = "term_noise_lexical"
            elif judgement.term_decision is TermDecision.NOISE:
                code = "term_noise"
            elif judgement.term_decision is TermDecision.UNCERTAIN:
                code = "term_uncertain"
            elif judgement.sense_decision is SenseDecision.MENTION_ONLY:
                code = "concept_sense_mention_only"
            elif judgement.sense_decision is SenseDecision.UNCERTAIN:
                code = "concept_sense_uncertain"
            else:
                continue
            payload = judgement.model_dump(mode="json")
            payload["block_id"] = group.block_id
            payload["occurrence_ids"] = list(group.occurrence_ids)
            records.append(
                DiagnosticRecord(
                    code=code,
                    message=judgement.reason,
                    payload=payload,
                )
            )
        return tuple(records)

    @staticmethod
    def _stats(
        terms: Sequence[TermRecord],
        concepts: Sequence[ConceptSense],
        links: Sequence[TermSenseLink],
        occurrences: Sequence[CandidateOccurrence],
        groups: Sequence[CandidateGroup],
        judgements: Sequence[CandidateGroupJudgement],
    ) -> dict[str, int | float | str]:
        concept_ids_by_term: dict[str, set[str]] = defaultdict(set)
        term_keys_by_concept: dict[str, set[str]] = defaultdict(set)
        for link in links:
            concept_ids_by_term[link.normalized_key].add(link.concept_sense_id)
            term_keys_by_concept[link.concept_sense_id].add(link.normalized_key)
        group_by_id = {group.candidate_group_id: group for group in groups}

        def occurrence_count(predicate: Callable[[CandidateGroupJudgement], bool]) -> int:
            return sum(
                len(group_by_id[item.candidate_group_id].occurrence_ids)
                for item in judgements
                if predicate(item)
            )

        return {
            "candidate_occurrences": len(occurrences),
            "candidate_groups": len(groups),
            "model_judged_groups": sum(group.model_judgement_required for group in groups),
            "lexically_excluded_groups": sum(
                not group.model_judgement_required for group in groups
            ),
            "terms": len(terms),
            "concept_senses": len(concepts),
            "term_sense_links": len(links),
            "accepted_term_groups": sum(
                item.term_decision is TermDecision.ACCEPTED for item in judgements
            ),
            "noise_term_groups": sum(
                item.term_decision is TermDecision.NOISE for item in judgements
            ),
            "uncertain_term_groups": sum(
                item.term_decision is TermDecision.UNCERTAIN for item in judgements
            ),
            "resolved_groups": sum(
                item.sense_decision is SenseDecision.RESOLVED for item in judgements
            ),
            "mention_only_groups": sum(
                item.sense_decision is SenseDecision.MENTION_ONLY for item in judgements
            ),
            "uncertain_sense_groups": sum(
                item.sense_decision is SenseDecision.UNCERTAIN for item in judgements
            ),
            "accepted_term_occurrences": occurrence_count(
                lambda item: item.term_decision is TermDecision.ACCEPTED
            ),
            "noise_term_occurrences": occurrence_count(
                lambda item: item.term_decision is TermDecision.NOISE
            ),
            "uncertain_term_occurrences": occurrence_count(
                lambda item: item.term_decision is TermDecision.UNCERTAIN
            ),
            "resolved_occurrences": occurrence_count(
                lambda item: item.sense_decision is SenseDecision.RESOLVED
            ),
            "mention_only_occurrences": occurrence_count(
                lambda item: item.sense_decision is SenseDecision.MENTION_ONLY
            ),
            "uncertain_sense_occurrences": occurrence_count(
                lambda item: item.sense_decision is SenseDecision.UNCERTAIN
            ),
            "one_term_multiple_senses": sum(
                len(concept_ids) > 1 for concept_ids in concept_ids_by_term.values()
            ),
            "one_sense_multiple_terms": sum(
                len(keys) > 1 for keys in term_keys_by_concept.values()
            ),
        }

    @staticmethod
    def _notify(progress: Callable[[str], None] | None, message: str) -> None:
        if progress is not None:
            progress(message)
