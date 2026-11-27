from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.dual_entry_index.artifacts import (
    ArtifactError,
    load_active_stage51_artifact,
    load_stage51_artifact,
    publish_stage51_artifact,
)
from app.dual_entry_index.compiler import CompilationFailure, Stage51Compiler
from app.dual_entry_index.induction import (
    CONSOLIDATION_OUTPUT_TOKENS_PER_DRAFT,
    CONSOLIDATION_SYSTEM_PROMPT,
    RETRY_FEEDBACK_RESERVE_CHARS,
    SUMMARY_SYSTEM_PROMPT,
    TERM_SENSE_SYSTEM_PROMPT,
    ConsolidationBatchResult,
    InductionFailure,
    InductionUsage,
    JsonChatResult,
    JudgementBatchResult,
    LLMTermSenseInducer,
)
from app.dual_entry_index.models import (
    ArtifactDiagnostic,
    CandidateGroup,
    CandidateGroupJudgement,
    CandidateOccurrence,
    ConceptSense,
    DocumentVersionIdentity,
    LinkBasis,
    ObjectStatus,
    SenseDecision,
    SnapshotData,
    SourceBlock,
    Stage51Artifact,
    SummaryBasis,
    TermDecision,
    TermRecord,
    TermSenseDraft,
    TermSenseLink,
)
from app.dual_entry_index.reconciliation import (
    ReconciliationAction,
    ReconciliationError,
    ReconciliationObjectKind,
    build_reconciliation_plan,
)
from app.dual_entry_index.scanner import (
    CandidateScanner,
    ScanContractError,
    current_scan_contract,
)
from app.dual_entry_index.snapshot import (
    SnapshotLoader,
    SnapshotLoadError,
    select_snapshot_documents,
)


class _FakeInducer:
    model_id = "fake-stage51-model"

    def __init__(
        self,
        accepted_keys: set[str],
        *,
        actual_model_id: str | None = None,
    ) -> None:
        self._accepted_keys = accepted_keys
        self._actual_model_id = actual_model_id or self.model_id
        self.seen_normalized_keys: list[str] = []
        self.seen_consolidation_drafts: list[tuple[TermSenseDraft, ...]] = []

    async def judge_candidate_groups(
        self,
        *,
        blocks_by_id: Mapping[str, SourceBlock],
        candidate_groups: Sequence[CandidateGroup],
        occurrence_by_id: Mapping[str, CandidateOccurrence],
        existing_senses_by_key: Mapping[str, Sequence[dict[str, object]]],
        concurrency: int,
        progress: object = None,
    ) -> JudgementBatchResult:
        del blocks_by_id, occurrence_by_id, concurrency, progress
        self.seen_normalized_keys.extend(group.normalized_key for group in candidate_groups)
        judgements = []
        for group in candidate_groups:
            if group.normalized_key in self._accepted_keys:
                existing = existing_senses_by_key.get(group.normalized_key, ())
                existing_sense_id = str(existing[0]["concept_sense_id"]) if existing else None
                judgements.append(
                    CandidateGroupJudgement(
                        candidate_group_id=group.candidate_group_id,
                        term_decision=TermDecision.ACCEPTED,
                        sense_decision=SenseDecision.RESOLVED,
                        existing_sense_id=existing_sense_id,
                        reason="原文说明了隔离作用",
                    )
                )
            else:
                judgements.append(
                    CandidateGroupJudgement(
                        candidate_group_id=group.candidate_group_id,
                        term_decision=TermDecision.NOISE,
                        sense_decision=SenseDecision.UNCERTAIN,
                        reason="不是稳定术语",
                    )
                )
        return JudgementBatchResult(
            tuple(judgements),
            InductionUsage(
                requests=1,
                attempts=1,
                input_tokens=100,
                output_tokens=20,
                actual_model_ids=(self._actual_model_id,),
            ),
        )

    async def consolidate_drafts(
        self,
        *,
        drafts: Sequence[TermSenseDraft],
        evidence_by_draft_id: Mapping[str, Sequence[str]],
        concurrency: int,
        progress: object = None,
    ) -> ConsolidationBatchResult:
        del evidence_by_draft_id, concurrency, progress
        self.seen_consolidation_drafts.append(tuple(drafts))
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

        by_key: dict[str, list[int]] = {}
        for index, draft in enumerate(drafts):
            for key in draft.normalized_keys:
                by_key.setdefault(key, []).append(index)
        for indexes in by_key.values():
            for index in indexes[1:]:
                union(indexes[0], index)
        components: dict[int, list[TermSenseDraft]] = {}
        for index, draft in enumerate(drafts):
            components.setdefault(find(index), []).append(draft)
        return ConsolidationBatchResult(
            tuple(
                TermSenseDraft(
                    draft_id=f"consolidated:{find(index)}",
                    normalized_keys=tuple(
                        sorted({key for member in members for key in member.normalized_keys})
                    ),
                    occurrence_ids=tuple(
                        sorted({item for member in members for item in member.occurrence_ids})
                    ),
                    support_block_ids=tuple(
                        sorted({item for member in members for item in member.support_block_ids})
                    ),
                    summary=next(
                        (member.summary for member in members if member.summary is not None),
                        "归并阶段生成的测试概念摘要",
                    ),
                    summary_basis=next(
                        (
                            member.summary_basis
                            for member in members
                            if member.summary_basis is not None
                        ),
                        SummaryBasis.CONTEXT_INFERRED,
                    ),
                    existing_sense_id=next(
                        iter(
                            {
                                member.existing_sense_id
                                for member in members
                                if member.existing_sense_id
                            }
                        ),
                        None,
                    ),
                    previous_sense_ids=tuple(
                        sorted({item for member in members for item in member.previous_sense_ids})
                    ),
                    refresh_summary_required=False,
                    source="consolidation",
                )
                for index, members in sorted(components.items())
            ),
            InductionUsage(),
        )


class _SkippingConsolidationInducer(_FakeInducer):
    """模拟资源护栏跳过整个归并分量，同时保留输入草稿。"""

    async def consolidate_drafts(
        self,
        *,
        drafts: Sequence[TermSenseDraft],
        evidence_by_draft_id: Mapping[str, Sequence[str]],
        concurrency: int,
        progress: object = None,
    ) -> ConsolidationBatchResult:
        del evidence_by_draft_id, concurrency, progress
        self.seen_consolidation_drafts.append(tuple(drafts))
        return ConsolidationBatchResult(
            drafts=tuple(drafts),
            usage=InductionUsage(),
            skipped_draft_ids=tuple(sorted(draft.draft_id for draft in drafts)),
        )


class _ModelDriftInducer(_FakeInducer):
    """模拟同一次编译中的多个请求返回不同供应商模型标识。"""

    async def judge_candidate_groups(
        self,
        *,
        blocks_by_id: Mapping[str, SourceBlock],
        candidate_groups: Sequence[CandidateGroup],
        occurrence_by_id: Mapping[str, CandidateOccurrence],
        existing_senses_by_key: Mapping[str, Sequence[dict[str, object]]],
        concurrency: int,
        progress: object = None,
    ) -> JudgementBatchResult:
        result = await super().judge_candidate_groups(
            blocks_by_id=blocks_by_id,
            candidate_groups=candidate_groups,
            occurrence_by_id=occurrence_by_id,
            existing_senses_by_key=existing_senses_by_key,
            concurrency=concurrency,
            progress=progress,
        )
        return JudgementBatchResult(
            judgements=result.judgements,
            usage=InductionUsage(
                requests=result.usage.requests,
                attempts=result.usage.attempts,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                actual_model_ids=("model-a", "model-b"),
            ),
            skipped_block_ids=result.skipped_block_ids,
        )


class _QueuedJsonClient:
    model_id = "fake-json-model"

    def __init__(self, responses: Sequence[JsonChatResult]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        del system_prompt, max_output_tokens
        self.prompts.append(user_prompt)
        if not self._responses:
            raise AssertionError("unexpected model request")
        return self._responses.pop(0)


class _FailingJsonClient:
    model_id = "fake-failing-model"

    def __init__(self, failure: str) -> None:
        self._failure = failure
        self.prompts: list[str] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        del system_prompt, max_output_tokens
        self.prompts.append(user_prompt)
        raise InductionFailure(self._failure)


class _ConcurrentConsolidationClient:
    model_id = "fake-concurrent-model"

    def __init__(self) -> None:
        self.active_requests = 0
        self.max_active_requests = 0
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        del max_output_tokens
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        payload = json.loads(user_prompt)
        self.active_requests += 1
        self.max_active_requests = max(self.max_active_requests, self.active_requests)
        try:
            await asyncio.sleep(0.05)
        finally:
            self.active_requests -= 1
        if system_prompt == SUMMARY_SYSTEM_PROMPT:
            drafts = payload["drafts"]
            response_payload = {
                "summaries": [
                    {
                        "i": draft["i"],
                        "summary": f"纯摘要 {draft['i']}",
                        "basis": "context_inferred",
                        "reason": "单义项摘要",
                    }
                    for draft in drafts
                ]
            }
        else:
            response_payload = {
                "components": [
                    {
                        "component": component["component"],
                        "groups": [
                            {
                                "indices": [draft["i"] for draft in component["drafts"]],
                                "summary": "并发归并后的概念",
                                "basis": "context_inferred",
                                "reason": "同一分量",
                            }
                        ],
                    }
                    for component in payload["components"]
                ]
            }
        return JsonChatResult(
            payload=response_payload,
            actual_model_id=self.model_id,
            input_tokens=10,
            output_tokens=5,
        )


class _AdaptiveCoverageClient:
    model_id = "fake-adaptive-model"

    def __init__(self) -> None:
        self.request_sizes: list[int] = []

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        max_output_tokens: int,
    ) -> JsonChatResult:
        del system_prompt, max_output_tokens
        payload = json.loads(user_prompt)
        candidates = payload["block"]["candidates"]
        self.request_sizes.append(len(candidates))
        covered = range(len(candidates)) if len(candidates) <= 2 else range(1)
        return JsonChatResult(
            payload={
                "decisions": [
                    {
                        "i": index,
                        "term": "accepted",
                        "sense": "resolved",
                        "reason": "测试覆盖",
                    }
                    for index in covered
                ],
            },
            actual_model_id=self.model_id,
            input_tokens=10,
            output_tokens=5,
        )


def _occurrence(
    key: str,
    *,
    start: int,
    block: SourceBlock,
    occurrence_id: str | None = None,
) -> CandidateOccurrence:
    return CandidateOccurrence(
        occurrence_id=occurrence_id or f"occ_{key}_{start}",
        snapshot_id=block.snapshot_id,
        normalized_key=key,
        display_text=key,
        doc_id=block.doc_id,
        block_id=block.block_id,
        block_index=block.block_index,
        block_ordinal=block.block_ordinal,
        block_content_hash=block.content_hash,
        start=start,
        end=start + len(key),
        scan_contract_hash="test",
    )


def _occurrence_map(
    *occurrences: CandidateOccurrence,
) -> dict[str, CandidateOccurrence]:
    return {item.occurrence_id: item for item in occurrences}


def _candidate_group(
    key: str,
    *,
    block: SourceBlock,
    occurrences: Sequence[CandidateOccurrence],
    model_judgement_required: bool = True,
    lexical_exclusion_reasons: tuple[str, ...] = (),
) -> CandidateGroup:
    first = occurrences[0]
    return CandidateGroup(
        candidate_group_id=f"cg_{block.block_id}_{key}",
        snapshot_id=block.snapshot_id,
        doc_id=block.doc_id,
        block_id=block.block_id,
        block_index=block.block_index,
        block_ordinal=block.block_ordinal,
        block_content_hash=block.content_hash,
        normalized_key=key,
        display_text=key,
        occurrence_ids=tuple(item.occurrence_id for item in occurrences),
        first_start=first.start,
        scan_contract_hash="test",
        model_judgement_required=model_judgement_required,
        lexical_exclusion_reasons=lexical_exclusion_reasons,
    )


def _block(
    text: str,
    *,
    block_id: str = "block_1",
    ordinal: int = 0,
    snapshot_id: str = "snapshot_1",
) -> SourceBlock:
    return SourceBlock(
        snapshot_id=snapshot_id,
        doc_id="doc_1",
        doc_version=1,
        doc_title="Agent architecture",
        block_id=block_id,
        block_index=ordinal + 1,
        block_ordinal=ordinal,
        text=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        section_path=["Execution"],
    )


def _snapshot(
    *blocks: SourceBlock,
    snapshot_id: str = "snapshot_1",
    manifest_hash: str = "a" * 64,
) -> SnapshotData:
    snapshot_blocks = tuple(
        block.model_copy(update={"snapshot_id": snapshot_id}) for block in blocks
    )
    document_versions = tuple(
        DocumentVersionIdentity(doc_id=doc_id, doc_version=doc_version)
        for doc_id, doc_version in sorted(
            {block.doc_id: block.doc_version for block in snapshot_blocks}.items()
        )
    )
    return SnapshotData(
        snapshot_id=snapshot_id,
        manifest_hash=manifest_hash,
        root=str(Path.cwd()),
        documents=document_versions,
        blocks=snapshot_blocks,
    )


def _json_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_manifest_with_hash(path: Path, manifest: dict[str, object]) -> None:
    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    persisted = {**payload, "manifest_hash": _json_hash(payload)}
    path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")


def _snapshot_fixture(
    root: Path,
    *,
    text: str = "Worktree keeps Agent state isolated.",
    declared_block_count: int = 1,
    table_context: dict[str, object] | None = None,
) -> tuple[Path, Path]:
    """写入一个最小但完整的 collection 快照，返回清单和块产物路径。"""

    doc_id = "doc_fixture"
    doc_version = 1
    document_root = root / "documents" / doc_id / "v0001"
    document_root.mkdir(parents=True)
    extracted_path = document_root / "extracted.json"
    block_set_path = document_root / "blocks.json"

    extracted = {
        "nodes": [
            {"ordinal": 1, "kind": "paragraph"},
            {"ordinal": 2, "kind": "code"},
        ]
    }
    extracted_bytes = json.dumps(extracted, ensure_ascii=False).encode("utf-8")
    extracted_path.write_bytes(extracted_bytes)

    block_set = {
        "doc_id": doc_id,
        "doc_version": doc_version,
        "blocks": [
            {
                "block_id": "block_fixture",
                "block_index": 1,
                "text": text,
                "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "section_path": ["Isolation"],
                "source_node_ordinals": [1, 2],
                "table_context": table_context,
            }
        ],
    }
    block_set_bytes = json.dumps(block_set, ensure_ascii=False).encode("utf-8")
    block_set_path.write_bytes(block_set_bytes)

    manifest = {
        "manifest_version": "s6-v1",
        "snapshot_id": "snapshot_fixture",
        "status": "ready",
        "completeness": "complete",
        "documents": [
            {
                "doc_id": doc_id,
                "doc_version": doc_version,
                "title": "Agent architecture",
                "block_count": declared_block_count,
                "table_block_count": 1 if table_context is not None else 0,
                "artifact_refs": [
                    {
                        "kind": "extracted",
                        "snapshot_ref": extracted_path.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(extracted_bytes).hexdigest(),
                        "byte_length": len(extracted_bytes),
                    },
                    {
                        "kind": "block_set",
                        "snapshot_ref": block_set_path.relative_to(root).as_posix(),
                        "sha256": hashlib.sha256(block_set_bytes).hexdigest(),
                        "byte_length": len(block_set_bytes),
                    },
                ],
            }
        ],
    }
    manifest_path = root / "manifest.json"
    _write_manifest_with_hash(manifest_path, manifest)
    return manifest_path, block_set_path


def _compiler(accepted_keys: set[str]) -> Stage51Compiler:
    return Stage51Compiler(
        _FakeInducer(accepted_keys),  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )


def test_scanner_is_deterministic_and_excludes_non_lexical_noise() -> None:
    scanner = CandidateScanner()
    text = (
        "Worktree 隔离 Agent 会话。访问 https://example.com 或 a@b.com。"
        "run_id 为 sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef。"
    )
    block = _block(text)

    first = scanner.scan("snapshot_1", (block,))
    second = scanner.scan("snapshot_1", (block,))

    assert first.candidate_set_hash == second.candidate_set_hash
    assert first.excluded_noise_counts == {"email": 1, "hash": 1, "url": 1}
    assert any(item.normalized_key == "worktree" for item in first.occurrences)
    assert any(item.normalized_key == "run_id" for item in first.occurrences)
    assert all(item.display_text == text[item.start : item.end] for item in first.occurrences)
    assert all("example.com" not in item.display_text for item in first.occurrences)


def test_snapshot_loader_preserves_document_table_and_source_node_context(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _snapshot_fixture(
        root,
        table_context={
            "caption": "Containment boundaries",
            "headers": ["Layer", "Boundary"],
            "group_index": 1,
            "group_count": 1,
        },
    )

    snapshot = SnapshotLoader().load(root)

    assert snapshot.snapshot_id == "snapshot_fixture"
    assert snapshot.documents == (DocumentVersionIdentity(doc_id="doc_fixture", doc_version=1),)
    assert snapshot.blocks[0].table_caption == "Containment boundaries"
    assert snapshot.blocks[0].table_headers == ["Layer", "Boundary"]
    assert snapshot.blocks[0].source_node_types == ("code", "paragraph")


def test_snapshot_loader_rejects_manifest_rewrite_without_identity_update(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    manifest_path, _ = _snapshot_fixture(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["status"] = "building"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SnapshotLoadError, match="manifest_hash does not match content"):
        SnapshotLoader().load(root)


def test_snapshot_loader_rejects_block_artifact_hash_and_length_drift(
    tmp_path: Path,
) -> None:
    hash_root = tmp_path / "hash-snapshot"
    hash_root.mkdir()
    _, block_set_path = _snapshot_fixture(hash_root)
    block_set = json.loads(block_set_path.read_text(encoding="utf-8"))
    block_set["blocks"][0]["text"] = block_set["blocks"][0]["text"].replace("Worktree", "worktree")
    block_set_path.write_text(json.dumps(block_set, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SnapshotLoadError, match="block_set artifact sha256"):
        SnapshotLoader().load(hash_root)

    length_root = tmp_path / "length-snapshot"
    length_root.mkdir()
    _, length_block_path = _snapshot_fixture(length_root)
    length_block_path.write_bytes(length_block_path.read_bytes() + b" ")

    with pytest.raises(SnapshotLoadError, match="block_set artifact byte_length"):
        SnapshotLoader().load(length_root)


def test_snapshot_loader_rejects_block_content_hash_drift_after_artifact_rewrite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    manifest_path, block_set_path = _snapshot_fixture(root)
    block_set = json.loads(block_set_path.read_text(encoding="utf-8"))
    block_set["blocks"][0]["text"] = "Worktree stores Agent state."
    block_set_bytes = json.dumps(block_set, ensure_ascii=False).encode("utf-8")
    block_set_path.write_bytes(block_set_bytes)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    block_set_ref = next(
        artifact
        for artifact in manifest["documents"][0]["artifact_refs"]
        if artifact["kind"] == "block_set"
    )
    block_set_ref["sha256"] = hashlib.sha256(block_set_bytes).hexdigest()
    block_set_ref["byte_length"] = len(block_set_bytes)
    _write_manifest_with_hash(manifest_path, manifest)

    with pytest.raises(SnapshotLoadError, match="block content hash does not match text"):
        SnapshotLoader().load(root)


def test_snapshot_loader_rejects_document_block_count_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    root.mkdir()
    _snapshot_fixture(root, declared_block_count=2)

    with pytest.raises(SnapshotLoadError, match="block count does not match manifest"):
        SnapshotLoader().load(root)


def test_empty_document_version_still_enters_compile_contract() -> None:
    snapshot = SnapshotData(
        snapshot_id="snapshot_empty",
        manifest_hash="a" * 64,
        root=str(Path.cwd()),
        documents=(DocumentVersionIdentity(doc_id="doc_empty", doc_version=3),),
        blocks=(),
    )

    contract = _compiler(set())._compile_contract(CandidateScanner(), snapshot)

    assert contract.document_versions == (
        DocumentVersionIdentity(doc_id="doc_empty", doc_version=3),
    )


def test_snapshot_rejects_a_block_from_an_undeclared_document() -> None:
    with pytest.raises(ValidationError, match="not declared by manifest"):
        SnapshotData(
            snapshot_id="snapshot_1",
            manifest_hash="a" * 64,
            root=str(Path.cwd()),
            documents=(),
            blocks=(_block("Worktree keeps Agent state isolated."),),
        )


def test_probe_document_selection_must_cover_the_complete_snapshot() -> None:
    second = _block(
        "Checkpoint restores Agent state.",
        block_id="block_2",
        ordinal=1,
    ).model_copy(update={"doc_id": "doc_2"})
    snapshot = _snapshot(_block("Worktree keeps Agent state isolated."), second)

    with pytest.raises(SnapshotLoadError, match="cover the entire snapshot"):
        select_snapshot_documents(snapshot, ["doc_1"])
    assert select_snapshot_documents(snapshot, ["doc_1", "doc_2"]) == snapshot

    empty_snapshot = SnapshotData(
        snapshot_id="snapshot_empty",
        manifest_hash="a" * 64,
        root=str(Path.cwd()),
        documents=(DocumentVersionIdentity(doc_id="doc_empty", doc_version=1),),
        blocks=(),
    )
    assert select_snapshot_documents(empty_snapshot, ["doc_empty"]) == empty_snapshot


def test_compiler_rejects_tampered_scan_result_before_model_judgement() -> None:
    scanner = CandidateScanner()
    block = _block("Agent keeps agent state isolated.")
    snapshot = _snapshot(block)
    result = scanner.scan(snapshot.snapshot_id, snapshot.blocks)
    first_occurrence = result.occurrences[0]
    first_group = result.candidate_groups[0]

    Stage51Compiler._validate_block_scan_result(
        snapshot=snapshot,
        scanner=scanner,
        block=block,
        result=result,
    )

    bad_range_result = result.model_copy(
        update={
            "occurrences": (
                first_occurrence.model_copy(update={"display_text": "tampered"}),
                *result.occurrences[1:],
            )
        }
    )
    with pytest.raises(ScanContractError, match="text range"):
        Stage51Compiler._validate_block_scan_result(
            snapshot=snapshot,
            scanner=scanner,
            block=block,
            result=bad_range_result,
        )

    bad_id_result = result.model_copy(
        update={
            "occurrences": (
                first_occurrence.model_copy(update={"occurrence_id": "occ_tampered"}),
                *result.occurrences[1:],
            )
        }
    )
    with pytest.raises(ScanContractError, match="occurrence ID is not canonical"):
        Stage51Compiler._validate_block_scan_result(
            snapshot=snapshot,
            scanner=scanner,
            block=block,
            result=bad_id_result,
        )

    bad_count_result = result.model_copy(
        update={
            "block_statuses": (result.block_statuses[0].model_copy(update={"candidate_count": 0}),)
        }
    )
    with pytest.raises(ScanContractError, match="candidate count"):
        Stage51Compiler._validate_block_scan_result(
            snapshot=snapshot,
            scanner=scanner,
            block=block,
            result=bad_count_result,
        )

    bad_group_result = result.model_copy(
        update={
            "candidate_groups": (
                first_group.model_copy(update={"candidate_group_id": "cg_tampered"}),
                *result.candidate_groups[1:],
            )
        }
    )
    with pytest.raises(ScanContractError, match="group ID is not canonical"):
        Stage51Compiler._validate_block_scan_result(
            snapshot=snapshot,
            scanner=scanner,
            block=block,
            result=bad_group_result,
        )

    unordered_result = result.model_copy(
        update={"occurrences": tuple(reversed(result.occurrences))}
    )
    with pytest.raises(ScanContractError, match="not deterministically ordered"):
        Stage51Compiler._validate_block_scan_result(
            snapshot=snapshot,
            scanner=scanner,
            block=block,
            result=unordered_result,
        )

    bad_hash_result = result.model_copy(update={"candidate_set_hash": "0" * 64})
    with pytest.raises(ScanContractError, match="candidate set hash"):
        Stage51Compiler._validate_block_scan_result(
            snapshot=snapshot,
            scanner=scanner,
            block=block,
            result=bad_hash_result,
        )


def test_scanner_keeps_ascii_fragments_adjacent_to_chinese() -> None:
    scanner = CandidateScanner()
    text = "Agent运行时保持隔离，未知词Xyz引用GPT-4o。"

    scan_result = scanner.scan("snapshot_1", (_block(text),))
    keys = {item.normalized_key for item in scan_result.occurrences}

    assert {"agent", "运行", "隔离", "xyz", "gpt-4o"} <= keys
    assert all(item.display_text == text[item.start : item.end] for item in scan_result.occurrences)


def test_scan_contract_hash_changes_with_boundary_rule_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_hash = current_scan_contract().contract_hash
    monkeypatch.setattr(
        "app.dual_entry_index.scanner._CHINESE_BOUNDARY_WORDS",
        frozenset({"例如", "例如新增边界词"}),
    )

    changed_hash = current_scan_contract().contract_hash

    assert changed_hash != original_hash


def test_scanner_keeps_high_precision_lexical_exclusions_for_audit_only() -> None:
    scanner = CandidateScanner()
    text = "More capable models can't run outside."
    scan_result = scanner.scan("snapshot_1", (_block(text),))
    by_key = {item.normalized_key: item for item in scan_result.occurrences}

    assert by_key["more"].model_judgement_required is False
    assert by_key["more"].lexical_exclusion_reason == "english_function_word"
    assert by_key["outside"].model_judgement_required is False
    assert by_key["ca"].model_judgement_required is False
    assert by_key["n't"].model_judgement_required is False
    assert by_key["capable"].model_judgement_required is True
    assert by_key["models"].model_judgement_required is True
    assert by_key["more capable models can't run outside"].model_judgement_required is True
    assert scan_result.lexical_exclusion_counts == {
        "english_contraction_fragment": 2,
        "english_function_word": 2,
    }


def test_scanner_collapses_same_block_term_once_and_keeps_all_occurrences() -> None:
    scanner = CandidateScanner()
    block = _block("Agent keeps agent state isolated.")

    scan_result = scanner.scan("snapshot_1", (block,))

    agent_occurrences = [item for item in scan_result.occurrences if item.normalized_key == "agent"]
    agent_groups = [item for item in scan_result.candidate_groups if item.normalized_key == "agent"]
    assert len(agent_occurrences) == 2
    assert len(agent_groups) == 1
    assert agent_groups[0].occurrence_ids == tuple(item.occurrence_id for item in agent_occurrences)


def test_scanner_keeps_same_term_in_different_blocks_as_distinct_groups() -> None:
    scanner = CandidateScanner()
    blocks = (
        _block("Agent runtime.", block_id="block_1", ordinal=0),
        _block("Agent tools.", block_id="block_2", ordinal=1),
    )

    scan_result = scanner.scan("snapshot_1", blocks)

    agent_groups = [item for item in scan_result.candidate_groups if item.normalized_key == "agent"]
    assert len(agent_groups) == 2
    assert {item.block_id for item in agent_groups} == {"block_1", "block_2"}


def test_compiler_replaces_lexical_exclusions_with_deterministic_noise_judgements() -> None:
    text = "More capable models can't run."
    inducer = _FakeInducer({"models"})
    compiler = Stage51Compiler(
        inducer,  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )

    artifact, diagnostics = asyncio.run(
        compiler.compile(
            snapshot=_snapshot(_block(text)),
            scanner=CandidateScanner(),
            base_artifact=None,
            concurrency=1,
        )
    )

    assert "more" not in inducer.seen_normalized_keys
    assert "ca" not in inducer.seen_normalized_keys
    assert "n't" not in inducer.seen_normalized_keys
    assert "run" in inducer.seen_normalized_keys
    assert artifact.stats["lexically_excluded_groups"] == 3
    assert any(record.code == "term_noise_lexical" for record in diagnostics.records)


def test_compile_publishes_a_traceable_concept_and_preserves_same_block_on_noop() -> None:
    scanner = CandidateScanner()
    snapshot = _snapshot(_block("Worktree keeps Agent session state isolated."))

    artifact, diagnostics = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )

    assert len(artifact.terms) == 1
    assert artifact.terms[0].normalized_key == "worktree"
    assert len(artifact.concept_senses) == 1
    assert artifact.concept_senses[0].support_block_ids == ("block_1",)
    assert all(link.support_block_ids == ("block_1",) for link in artifact.term_sense_links)
    assert diagnostics.candidate_occurrence_count > 0
    assert diagnostics.candidate_group_count > 0
    assert tuple(metric.stage for metric in diagnostics.stage_metrics) == (
        "scan",
        "term_judgement",
        "consolidation",
        "materialization",
        "total_compile",
    )
    assert all(metric.wall_seconds >= 0 for metric in diagnostics.stage_metrics)

    unchanged, unchanged_diagnostics = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=artifact,
            concurrency=1,
        )
    )
    assert unchanged.map_id == artifact.map_id
    assert unchanged_diagnostics.llm_requests == 0
    assert unchanged_diagnostics.actual_model_ids == artifact.compile_contract.actual_model_ids
    assert unchanged_diagnostics.stage_metrics[-1].stage == "total_compile"


def test_new_sense_draft_reaches_consolidation_without_a_local_summary() -> None:
    inducer = _FakeInducer({"worktree"})
    artifact, _ = asyncio.run(
        Stage51Compiler(
            inducer,  # type: ignore[arg-type]
            max_input_chars=20_000,
            max_output_tokens=2_000,
        ).compile(
            snapshot=_snapshot(_block("Worktree isolates execution context.")),
            scanner=CandidateScanner(),
            base_artifact=None,
            concurrency=1,
        )
    )

    first_drafts = inducer.seen_consolidation_drafts[0]
    assert first_drafts
    assert all(draft.summary is None for draft in first_drafts)
    assert all(draft.summary_basis is None for draft in first_drafts)
    assert artifact.concept_senses[0].summary == "归并阶段生成的测试概念摘要"


def test_compile_rejects_actual_model_drift_before_publication() -> None:
    compiler = Stage51Compiler(
        _ModelDriftInducer({"worktree"}),  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )

    with pytest.raises(
        CompilationFailure,
        match="model_identity_changed_during_compile:model-a,model-b",
    ):
        asyncio.run(
            compiler.compile(
                snapshot=_snapshot(_block("Worktree isolates execution context.")),
                scanner=CandidateScanner(),
                base_artifact=None,
                concurrency=1,
            )
        )


def test_incremental_compile_rejects_actual_model_change_and_inherits_without_calls() -> None:
    scanner = CandidateScanner()
    first = _snapshot(_block("Worktree isolates execution context.", block_id="block_1"))
    base, _ = asyncio.run(
        Stage51Compiler(
            _FakeInducer({"worktree"}, actual_model_id="actual-model-a"),  # type: ignore[arg-type]
            max_input_chars=20_000,
            max_output_tokens=2_000,
        ).compile(
            snapshot=first,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    assert base.compile_contract.actual_model_ids == ("actual-model-a",)

    changed_model_compiler = Stage51Compiler(
        _FakeInducer({"worktree"}, actual_model_id="actual-model-b"),  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )
    with pytest.raises(
        CompilationFailure,
        match="model_identity_changed_from_base:actual-model-a->actual-model-b",
    ):
        asyncio.run(
            changed_model_compiler.compile(
                snapshot=_snapshot(
                    _block("Worktree isolates execution context.", block_id="block_1"),
                    _block(
                        "Worktree stores a checkpoint.",
                        block_id="block_2",
                        ordinal=1,
                    ),
                ),
                scanner=scanner,
                base_artifact=base,
                concurrency=1,
            )
        )

    empty, empty_diagnostics = asyncio.run(
        Stage51Compiler(
            _FakeInducer(set(), actual_model_id="unused-model"),  # type: ignore[arg-type]
            max_input_chars=20_000,
            max_output_tokens=2_000,
        ).compile(
            snapshot=_snapshot(),
            scanner=scanner,
            base_artifact=base,
            concurrency=1,
        )
    )

    assert empty.compile_contract.actual_model_ids == ("actual-model-a",)
    assert empty_diagnostics.actual_model_ids == ("actual-model-a",)


def test_map_diagnostics_participate_in_immutable_map_identity() -> None:
    scanner = CandidateScanner()
    artifact, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=_snapshot(_block("Worktree isolates execution context.")),
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    diagnostic = ArtifactDiagnostic(
        code="candidate_closure_incomplete",
        message="测试闭包不完整",
        block_id="block_1",
    )
    artifact_without_diagnostics = artifact.model_copy(
        update={"map_id": "pending", "map_hash": "", "diagnostics": ()}
    )
    artifact_with_diagnostics = artifact_without_diagnostics.model_copy(
        update={"diagnostics": (diagnostic,)}
    )
    changed_hash = artifact_with_diagnostics.calculate_map_hash()
    changed = artifact_with_diagnostics.model_copy(
        update={"map_id": f"km51_{changed_hash[:16]}", "map_hash": changed_hash}
    )

    assert changed.map_hash != artifact.map_hash
    assert changed.map_id != artifact.map_id


def test_reconciliation_plan_is_typed_and_replays_to_the_target_state() -> None:
    scanner = CandidateScanner()
    snapshot = _snapshot(_block("Worktree isolates execution context."))
    base, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    old_sense = base.concept_senses[0]
    new_sense = old_sense.model_copy(
        update={
            "concept_sense_id": "sense_updated",
            "summary": "更新后的工作区隔离概念",
        }
    )
    new_link = base.term_sense_links[0].model_copy(
        update={"concept_sense_id": new_sense.concept_sense_id}
    )
    plan = build_reconciliation_plan(
        base_artifact=base,
        target_snapshot_id=snapshot.snapshot_id,
        induction_contract_version="stage51-test-v1",
        terms=base.terms,
        links=(new_link,),
        concepts=(new_sense,),
        sense_lineage={new_sense.concept_sense_id: (old_sense.concept_sense_id,)},
        affected_term_keys={"worktree"},
    )

    assert all(
        operation.induction_contract_version == "stage51-test-v1" for operation in plan.operations
    )
    assert any(
        operation.object_kind is ReconciliationObjectKind.CONCEPT_SENSE
        and operation.action is ReconciliationAction.UPDATE
        for operation in plan.operations
    )
    assert plan.apply(base) == (base.terms, (new_link,), (new_sense,))


def test_reconciliation_link_sort_uses_structured_key_not_concatenated_identity() -> None:
    concept = ConceptSense(
        concept_sense_id="sense_1",
        summary="共同概念",
        summary_basis=SummaryBasis.CONTEXT_INFERRED,
        support_block_ids=("block_1",),
        induction_contract_version="test",
    )
    links = (
        TermSenseLink(
            normalized_key="a",
            concept_sense_id=concept.concept_sense_id,
            support_block_ids=("block_1",),
            basis_type=LinkBasis.DIRECT_USE,
        ),
        TermSenseLink(
            normalized_key="a-b",
            concept_sense_id=concept.concept_sense_id,
            support_block_ids=("block_1",),
            basis_type=LinkBasis.DIRECT_USE,
        ),
    )
    terms = tuple(
        TermRecord(
            normalized_key=link.normalized_key,
            display_text=link.normalized_key,
            support_block_ids=link.support_block_ids,
        )
        for link in links
    )

    plan = build_reconciliation_plan(
        base_artifact=None,
        target_snapshot_id="snapshot_1",
        induction_contract_version="test",
        terms=terms,
        links=links,
        concepts=(concept,),
        sense_lineage={concept.concept_sense_id: ()},
        affected_term_keys={"a", "a-b"},
    )

    _, replayed_links, _ = plan.apply(None)
    assert replayed_links == links


def test_reconciliation_rejects_concept_changes_outside_the_affected_scope() -> None:
    scanner = CandidateScanner()
    snapshot = _snapshot(_block("Worktree isolates execution context."))
    base, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    changed_sense = base.concept_senses[0].model_copy(
        update={"summary": "闭包之外不应发生的摘要修改"}
    )

    with pytest.raises(
        ReconciliationError,
        match="reconciliation_modified_object_outside_affected_scope:concept_sense",
    ):
        build_reconciliation_plan(
            base_artifact=base,
            target_snapshot_id=snapshot.snapshot_id,
            induction_contract_version="stage51-test-v1",
            terms=base.terms,
            links=base.term_sense_links,
            concepts=(changed_sense,),
            sense_lineage={},
            affected_term_keys={"unrelated"},
        )


def test_published_term_support_must_contain_the_term_surface() -> None:
    snapshot = _snapshot(_block("Unrelated text."))
    term = TermRecord(
        normalized_key="worktree",
        display_text="worktree",
        support_block_ids=("block_1",),
    )

    with pytest.raises(CompilationFailure, match="term_support_block_does_not_contain_term"):
        Stage51Compiler._validate_product(snapshot, (term,), (), (), "test")


def test_same_block_id_with_changed_content_is_a_snapshot_integrity_error() -> None:
    scanner = CandidateScanner()
    original = _snapshot(_block("Worktree keeps Agent state isolated."))
    artifact, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=original,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    changed = _snapshot(
        _block("Worktree now stores Agent state."),
        snapshot_id="snapshot_2",
        manifest_hash="b" * 64,
    )
    with pytest.raises(CompilationFailure, match="snapshot_block_identity_conflict:block_1"):
        asyncio.run(
            _compiler({"worktree"}).compile(
                snapshot=changed,
                scanner=scanner,
                base_artifact=artifact,
                concurrency=1,
            )
        )


def test_old_contract_version_forces_full_rebuild_instead_of_incremental_reuse() -> None:
    scanner = CandidateScanner()
    snapshot = _snapshot(_block("Worktree keeps Agent state isolated."))
    base, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    old_contract = base.compile_contract.model_copy(
        update={
            "contract_version": "stage51-compile-v4",
            "compiler_version": "dual-entry-stage51-v5",
        }
    )
    old_contract = old_contract.model_copy(
        update={"compatibility_hash": old_contract.calculate_compatibility_hash()}
    )
    old_contract = old_contract.model_copy(
        update={"contract_hash": old_contract.calculate_contract_hash()}
    )
    old_contract = old_contract.model_copy(
        update={"contract_id": f"cc51_{old_contract.contract_hash[:16]}"}
    )
    old_base = base.model_copy(update={"compile_contract": old_contract})

    rebuilt, diagnostics = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=old_base,
            concurrency=1,
        )
    )

    assert rebuilt.compilation_mode == "full"
    assert rebuilt.base_map_id is None
    assert diagnostics.added_block_ids == ("block_1",)


def test_cross_snapshot_increment_refreshes_the_full_support_summary() -> None:
    scanner = CandidateScanner()
    inducer = _FakeInducer({"worktree"})
    compiler = Stage51Compiler(
        inducer,  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )
    first = _snapshot(
        _block("Worktree keeps Agent state isolated.", block_id="block_1"),
        snapshot_id="snapshot_1",
    )
    artifact, _ = asyncio.run(
        compiler.compile(
            snapshot=first,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    second = _snapshot(
        _block(
            "Worktree keeps Agent state isolated.",
            block_id="block_1",
            snapshot_id="snapshot_2",
        ),
        _block(
            "Worktree stores a recoverable checkpoint.",
            block_id="block_2",
            ordinal=1,
            snapshot_id="snapshot_2",
        ),
        snapshot_id="snapshot_2",
        manifest_hash="b" * 64,
    )

    updated, diagnostics = asyncio.run(
        compiler.compile(
            snapshot=second,
            scanner=scanner,
            base_artifact=artifact,
            concurrency=1,
        )
    )

    assert diagnostics.added_block_ids == ("block_2",)
    assert diagnostics.deleted_block_ids == ()
    assert diagnostics.rescanned_block_ids == ("block_1",)
    assert updated.compilation_mode == "incremental"
    assert updated.base_map_id == artifact.map_id
    assert (
        updated.compile_contract.compatibility_hash == artifact.compile_contract.compatibility_hash
    )
    assert updated.compile_contract.contract_hash != artifact.compile_contract.contract_hash
    assert updated.compile_contract.max_candidate_groups_per_judgement_request == 48
    assert (
        updated.compile_contract.consolidation_output_tokens_per_draft
        == CONSOLIDATION_OUTPUT_TOKENS_PER_DRAFT
    )
    assert updated.terms[0].support_block_ids == ("block_1", "block_2")
    assert len(updated.concept_senses) == 1
    assert updated.concept_senses[0].support_block_ids == ("block_1", "block_2")
    refreshed = inducer.seen_consolidation_drafts[-1]
    assert len(refreshed) == 1
    assert refreshed[0].support_block_ids == ("block_1", "block_2")
    assert refreshed[0].refresh_summary_required is True

    third = _snapshot(
        _block(
            "Worktree stores a recoverable checkpoint.",
            block_id="block_2",
            ordinal=0,
            snapshot_id="snapshot_3",
        ),
        snapshot_id="snapshot_3",
        manifest_hash="c" * 64,
    )
    reduced, reduced_diagnostics = asyncio.run(
        compiler.compile(
            snapshot=third,
            scanner=scanner,
            base_artifact=updated,
            concurrency=1,
        )
    )

    assert reduced_diagnostics.deleted_block_ids == ("block_1",)
    assert reduced_diagnostics.rescanned_block_ids == ("block_2",)
    assert reduced.terms[0].support_block_ids == ("block_2",)
    assert reduced.concept_senses[0].support_block_ids == ("block_2",)
    reduced_refresh = inducer.seen_consolidation_drafts[-1]
    assert len(reduced_refresh) == 1
    assert reduced_refresh[0].support_block_ids == ("block_2",)
    assert reduced_refresh[0].refresh_summary_required is True


def test_affected_concept_closure_keeps_every_linked_term_key() -> None:
    scanner = CandidateScanner()
    inducer = _FakeInducer({"alpha", "beta"})
    compiler = Stage51Compiler(
        inducer,  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )
    base_block = _block("Alpha beta share one explicit boundary.", block_id="block_1")
    base_artifact, _ = asyncio.run(
        compiler.compile(
            snapshot=_snapshot(base_block),
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    shared_sense = ConceptSense(
        concept_sense_id="sense_shared_aliases",
        summary="Alpha 与 beta 共同指向的边界概念",
        summary_basis=SummaryBasis.CONTEXT_INFERRED,
        support_block_ids=("block_1",),
        induction_contract_version=(base_artifact.compile_contract.induction_contract_version),
    )
    shared_links = tuple(
        TermSenseLink(
            normalized_key=link.normalized_key,
            concept_sense_id=shared_sense.concept_sense_id,
            support_block_ids=link.support_block_ids,
            basis_type=LinkBasis.DIRECT_USE,
        )
        for link in base_artifact.term_sense_links
    )
    base_with_alias = base_artifact.model_copy(
        update={
            "concept_senses": (shared_sense,),
            "term_sense_links": shared_links,
        }
    )
    target = _snapshot(
        _block(
            "Alpha beta share one explicit boundary.",
            block_id="block_1",
            snapshot_id="snapshot_2",
        ),
        _block(
            "Alpha controls the execution boundary.",
            block_id="block_2",
            ordinal=1,
            snapshot_id="snapshot_2",
        ),
        snapshot_id="snapshot_2",
        manifest_hash="b" * 64,
    )

    updated, _ = asyncio.run(
        compiler.compile(
            snapshot=target,
            scanner=scanner,
            base_artifact=base_with_alias,
            concurrency=1,
        )
    )

    assert {link.normalized_key for link in updated.term_sense_links} == {
        "alpha",
        "beta",
    }
    assert updated.term_sense_links[0].concept_sense_id == (
        updated.term_sense_links[1].concept_sense_id
    )


def test_deleted_alias_support_still_rescans_the_concepts_other_aliases() -> None:
    scanner = CandidateScanner()
    inducer = _FakeInducer({"alpha", "beta"})
    compiler = Stage51Compiler(
        inducer,  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )
    base_artifact, _ = asyncio.run(
        compiler.compile(
            snapshot=_snapshot(
                _block("Alpha marks one boundary.", block_id="block_1", ordinal=0),
                _block("Beta marks the same boundary.", block_id="block_2", ordinal=1),
            ),
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    shared_sense = ConceptSense(
        concept_sense_id="sense_shared_boundary",
        summary="Alpha 与 beta 共同指向的边界概念",
        summary_basis=SummaryBasis.CONTEXT_INFERRED,
        support_block_ids=("block_1", "block_2"),
        induction_contract_version=base_artifact.compile_contract.induction_contract_version,
    )
    shared_links = tuple(
        link.model_copy(update={"concept_sense_id": shared_sense.concept_sense_id})
        for link in base_artifact.term_sense_links
    )
    base_with_alias = base_artifact.model_copy(
        update={
            "concept_senses": (shared_sense,),
            "term_sense_links": shared_links,
        }
    )

    updated, diagnostics = asyncio.run(
        compiler.compile(
            snapshot=_snapshot(
                _block(
                    "Beta marks the same boundary.",
                    block_id="block_2",
                    ordinal=1,
                    snapshot_id="snapshot_2",
                ),
                snapshot_id="snapshot_2",
                manifest_hash="b" * 64,
            ),
            scanner=scanner,
            base_artifact=base_with_alias,
            concurrency=1,
        )
    )

    assert diagnostics.deleted_block_ids == ("block_1",)
    assert diagnostics.rescanned_block_ids == ("block_2",)
    assert {term.normalized_key for term in updated.terms} == {"beta"}
    assert {link.normalized_key for link in updated.term_sense_links} == {"beta"}
    assert updated.concept_senses[0].support_block_ids == ("block_2",)


def test_deleted_only_support_removes_term_and_concept() -> None:
    scanner = CandidateScanner()
    original = _snapshot(
        _block("Worktree keeps Agent state isolated.", block_id="block_1", ordinal=0),
        _block("Other notes.", block_id="block_2", ordinal=1),
    )
    artifact, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=original,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    reduced = _snapshot(_block("Other notes.", block_id="block_2", ordinal=1))
    updated, diagnostics = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=reduced,
            scanner=scanner,
            base_artifact=artifact,
            concurrency=1,
        )
    )

    assert diagnostics.deleted_block_ids == ("block_1",)
    assert updated.terms == ()
    assert updated.concept_senses == ()
    assert updated.term_sense_links == ()


def test_deleting_the_last_document_publishes_an_empty_map(tmp_path: Path) -> None:
    scanner = CandidateScanner()
    artifact, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=_snapshot(_block("Worktree keeps Agent state isolated.")),
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )

    empty, diagnostics = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=_snapshot(snapshot_id="snapshot_2", manifest_hash="b" * 64),
            scanner=scanner,
            base_artifact=artifact,
            concurrency=1,
        )
    )

    assert diagnostics.block_count == 0
    assert diagnostics.deleted_block_ids == ("block_1",)
    assert empty.compilation_mode == "incremental"
    assert empty.source_blocks == ()
    assert empty.terms == ()
    assert empty.concept_senses == ()
    assert empty.term_sense_links == ()
    output = publish_stage51_artifact(
        tmp_path,
        empty,
        diagnostics,
        _snapshot(
            snapshot_id="snapshot_2",
            manifest_hash="b" * 64,
        ),
    )
    assert load_stage51_artifact(output) == empty
    assert load_active_stage51_artifact(tmp_path) == empty


def test_full_and_incremental_compile_have_equivalent_semantics_with_fixed_responses() -> None:
    scanner = CandidateScanner()
    first_block = _block("Worktree keeps Agent state isolated.", block_id="block_1")
    second_block = _block(
        "Worktree stores a recoverable checkpoint.",
        block_id="block_2",
        ordinal=1,
    )
    target_snapshot = _snapshot(first_block, second_block)

    full_artifact, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=target_snapshot,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    base_snapshot = _snapshot(first_block)
    base_artifact, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=base_snapshot,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    incremental_artifact, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=target_snapshot,
            scanner=scanner,
            base_artifact=base_artifact,
            concurrency=1,
        )
    )

    def semantic_state(artifact: Stage51Artifact) -> tuple[object, object, object]:
        return (
            tuple((term.normalized_key, term.support_block_ids) for term in artifact.terms),
            tuple(
                (
                    sense.summary,
                    sense.summary_basis.value,
                    sense.support_block_ids,
                )
                for sense in artifact.concept_senses
            ),
            tuple(
                (
                    link.normalized_key,
                    link.basis_type.value,
                    link.support_block_ids,
                )
                for link in artifact.term_sense_links
            ),
        )

    assert semantic_state(incremental_artifact) == semantic_state(full_artifact)


def test_oversized_block_is_skipped_and_marked_as_incomplete_closure() -> None:
    client = _QueuedJsonClient(())
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=10,
        max_output_tokens=100,
        max_retries=0,
    )
    compiler = Stage51Compiler(
        inducer,
        max_input_chars=10,
        max_output_tokens=100,
    )

    artifact, diagnostics = asyncio.run(
        compiler.compile(
            snapshot=_snapshot(_block("Worktree keeps Agent state isolated.")),
            scanner=CandidateScanner(),
            base_artifact=None,
            concurrency=1,
        )
    )

    assert diagnostics.skipped_block_ids == ("block_1",)
    assert artifact.terms == ()
    assert artifact.concept_senses == ()
    assert any(
        diagnostic.code == "candidate_closure_incomplete" for diagnostic in artifact.diagnostics
    )
    assert artifact.stats["candidate_closure_incomplete_blocks"] == 1


def test_publication_is_atomic_and_detects_tampering(tmp_path: Path) -> None:
    scanner = CandidateScanner()
    snapshot = _snapshot(_block("Worktree keeps Agent state isolated."))
    artifact, diagnostics = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    output = publish_stage51_artifact(tmp_path, artifact, diagnostics, snapshot)
    assert load_stage51_artifact(output).map_id == artifact.map_id
    assert load_active_stage51_artifact(tmp_path).map_id == artifact.map_id
    reused_artifact, reused_diagnostics = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=snapshot,
            scanner=scanner,
            base_artifact=artifact,
            concurrency=1,
        )
    )
    republished = publish_stage51_artifact(
        tmp_path,
        reused_artifact,
        reused_diagnostics,
        snapshot,
    )
    assert republished == output
    assert load_active_stage51_artifact(tmp_path).map_id == artifact.map_id

    pointer_path = tmp_path / "active-map.json"
    original_pointer = pointer_path.read_text(encoding="utf-8")
    tampered_pointer = json.loads(original_pointer)
    tampered_pointer["compile_contract_id"] = "cc51_0000000000000000"
    pointer_path.write_text(
        json.dumps(tampered_pointer, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="compile identity"):
        load_active_stage51_artifact(tmp_path)
    pointer_path.write_text(original_pointer, encoding="utf-8")

    invalid_artifact = artifact.model_copy(update={"map_hash": "0" * 64})
    with pytest.raises(ArtifactError, match="map_hash"):
        publish_stage51_artifact(tmp_path, invalid_artifact, diagnostics, snapshot)
    assert load_active_stage51_artifact(tmp_path).map_id == artifact.map_id

    invalid_contract_artifact = artifact.model_copy(
        update={
            "compile_contract": artifact.compile_contract.model_copy(
                update={"compatibility_hash": "0" * 64}
            )
        }
    )
    with pytest.raises(ArtifactError, match="compatibility hash"):
        publish_stage51_artifact(
            tmp_path,
            invalid_contract_artifact,
            diagnostics,
            snapshot,
        )
    assert load_active_stage51_artifact(tmp_path).map_id == artifact.map_id

    retired_artifact = artifact.model_copy(
        update={"terms": (artifact.terms[0].model_copy(update={"status": ObjectStatus.RETIRED}),)}
    )
    retired_map_hash = retired_artifact.calculate_map_hash()
    retired_artifact = retired_artifact.model_copy(
        update={
            "map_hash": retired_map_hash,
            "map_id": f"km51_{retired_map_hash[:16]}",
        }
    )
    with pytest.raises(ArtifactError, match="term is not active"):
        publish_stage51_artifact(
            tmp_path,
            retired_artifact,
            diagnostics,
            snapshot,
        )
    assert load_active_stage51_artifact(tmp_path).map_id == artifact.map_id

    artifact_path = output / "artifact.json"
    tampered = artifact_path.read_text(encoding="utf-8").replace(
        '"snapshot_manifest_hash": "' + "a" * 64 + '"',
        '"snapshot_manifest_hash": "' + "b" * 64 + '"',
    )
    artifact_path.write_text(tampered, encoding="utf-8")
    with pytest.raises(ArtifactError):
        load_stage51_artifact(output)


def test_same_occurrence_cannot_enter_two_concept_drafts() -> None:
    scanner = CandidateScanner()
    snapshot = _snapshot(_block("Worktree keeps Agent state isolated."))
    scan_result = scanner.scan("snapshot_1", snapshot.blocks)
    occurrence = next(item for item in scan_result.occurrences if item.normalized_key == "worktree")
    judgement = CandidateGroupJudgement(
        candidate_group_id=f"cg_{occurrence.block_id}_{occurrence.normalized_key}",
        term_decision=TermDecision.ACCEPTED,
        sense_decision=SenseDecision.RESOLVED,
        reason="测试",
    )
    drafts = (
        TermSenseDraft(
            draft_id="draft_1",
            normalized_keys=("worktree",),
            occurrence_ids=(occurrence.occurrence_id,),
            support_block_ids=("block_1",),
            summary="一个义项",
            summary_basis=SummaryBasis.CONTEXT_INFERRED,
            source="model",
        ),
        TermSenseDraft(
            draft_id="draft_2",
            normalized_keys=("worktree",),
            occurrence_ids=(occurrence.occurrence_id,),
            support_block_ids=("block_1",),
            summary="另一个义项",
            summary_basis=SummaryBasis.CONTEXT_INFERRED,
            source="model",
        ),
    )
    with pytest.raises(CompilationFailure, match="multiple_senses"):
        Stage51Compiler._materialize_graph(
            drafts=drafts,
            base_links=(),
            occurrence_by_id={occurrence.occurrence_id: occurrence},
            judgements={occurrence.occurrence_id: judgement},
            induction_contract_version="test",
        )


def test_compact_judgement_protocol_maps_flat_decisions_back_to_full_ids() -> None:
    block = _block("Alpha beta")
    first = _occurrence("alpha", start=0, block=block)
    second = _occurrence("beta", start=6, block=block)
    first_group = _candidate_group("alpha", block=block, occurrences=(first,))
    second_group = _candidate_group("beta", block=block, occurrences=(second,))
    client = _QueuedJsonClient(
        (
            JsonChatResult(
                payload={
                    "decisions": [
                        {
                            "i": 0,
                            "term": "noise",
                            "sense": "not_applicable",
                            "reason": "通用词",
                        },
                        {
                            "i": 1,
                            "term": "accepted",
                            "sense": "resolved",
                            "reason": "上下文足以识别",
                        },
                    ],
                },
                actual_model_id="fake-json-model",
                input_tokens=40,
                output_tokens=8,
                cache_hit_tokens=30,
                cache_miss_tokens=10,
            ),
        )
    )
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=10_000,
        max_output_tokens=1_000,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.judge_candidate_groups(
            blocks_by_id={block.block_id: block},
            candidate_groups=(first_group, second_group),
            occurrence_by_id=_occurrence_map(first, second),
            existing_senses_by_key={},
            concurrency=1,
        )
    )

    assert result.judgements[0].candidate_group_id == first_group.candidate_group_id
    assert result.judgements[0].term_decision is TermDecision.NOISE
    assert result.judgements[1].candidate_group_id == second_group.candidate_group_id
    assert result.judgements[1].term_decision is TermDecision.ACCEPTED
    assert first.occurrence_id not in client.prompts[0]
    assert second.occurrence_id not in client.prompts[0]
    assert '"start"' not in client.prompts[0]
    assert '"end"' not in client.prompts[0]
    assert result.usage.cache_hit_tokens == 30
    assert result.usage.cache_miss_tokens == 10


def test_local_judgement_indices_reject_out_of_range_duplicate_and_missing() -> None:
    block = _block("Alpha beta")
    occurrences = (_occurrence("alpha", start=0, block=block),)
    groups = (_candidate_group("alpha", block=block, occurrences=occurrences),)
    valid_item = {
        "i": 0,
        "term": "accepted",
        "sense": "resolved",
        "reason": "上下文明确",
    }
    invalid_payloads = (
        {
            "decisions": [
                {
                    "i": 1,
                    "term": "accepted",
                    "sense": "resolved",
                    "reason": "越界",
                }
            ],
        },
        {
            "decisions": [valid_item, valid_item],
        },
        {"decisions": []},
        {
            "decisions": [
                {
                    "i": 0,
                    "term": "noise",
                    "sense": "uncertain",
                    "reason": "非术语的义项必须标记不适用",
                }
            ]
        },
        {
            "decisions": [
                {
                    **valid_item,
                    "label": "本阶段禁止返回的标签",
                }
            ]
        },
    )

    for payload in invalid_payloads:
        client = _QueuedJsonClient(
            (
                JsonChatResult(
                    payload=payload,
                    actual_model_id="fake-json-model",
                    input_tokens=1,
                    output_tokens=1,
                ),
            )
        )
        inducer = LLMTermSenseInducer(
            client,
            max_input_chars=10_000,
            max_output_tokens=1_000,
            max_retries=0,
        )
        with pytest.raises(InductionFailure) as exc_info:
            asyncio.run(
                inducer.judge_candidate_groups(
                    blocks_by_id={block.block_id: block},
                    candidate_groups=groups,
                    occurrence_by_id=_occurrence_map(*occurrences),
                    existing_senses_by_key={},
                    concurrency=1,
                )
            )
        assert "term_judgement:block_id=" in str(exc_info.value)


def test_retry_usage_includes_failed_response_tokens_and_cache_counts() -> None:
    block = _block("Worktree")
    occurrence = _occurrence("worktree", start=0, block=block, occurrence_id="occ_worktree")
    group = _candidate_group("worktree", block=block, occurrences=(occurrence,))
    client = _QueuedJsonClient(
        (
            JsonChatResult(
                payload={},
                actual_model_id="fake-json-model",
                input_tokens=11,
                output_tokens=2,
                cache_hit_tokens=7,
                cache_miss_tokens=4,
            ),
            JsonChatResult(
                payload={
                    "decisions": [
                        {
                            "i": 0,
                            "term": "accepted",
                            "sense": "resolved",
                            "reason": "上下文明确",
                        }
                    ],
                },
                actual_model_id="fake-json-model",
                input_tokens=13,
                output_tokens=3,
                cache_hit_tokens=8,
                cache_miss_tokens=5,
            ),
        )
    )
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=10_000,
        max_output_tokens=1_000,
        max_retries=1,
    )

    result = asyncio.run(
        inducer.judge_candidate_groups(
            blocks_by_id={block.block_id: block},
            candidate_groups=(group,),
            occurrence_by_id=_occurrence_map(occurrence),
            existing_senses_by_key={},
            concurrency=1,
        )
    )

    assert result.usage.requests == 1
    assert result.usage.attempts == 2
    assert result.usage.failed_attempts == 1
    assert result.usage.retry_attempts == 1
    assert result.usage.input_tokens == 24
    assert result.usage.output_tokens == 5
    assert result.usage.cache_hit_tokens == 15
    assert result.usage.cache_miss_tokens == 9
    assert result.usage.actual_model_ids == ("fake-json-model",)
    assert len(result.usage.failure_details) == 1
    assert "term_judgement:block_id=" in result.usage.failure_details[0]
    assert "judgement_decisions_missing" in result.usage.failure_details[0]


def test_retry_feedback_keeps_the_prompt_within_the_input_budget() -> None:
    client = _QueuedJsonClient(
        (
            JsonChatResult(
                payload={},
                actual_model_id="fake-json-model",
                input_tokens=0,
                output_tokens=0,
            ),
            JsonChatResult(
                payload={"ok": True},
                actual_model_id="fake-json-model",
                input_tokens=0,
                output_tokens=0,
            ),
        )
    )
    prompt = '{"task":"retry-budget"}'
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=len(prompt) + RETRY_FEEDBACK_RESERVE_CHARS,
        max_output_tokens=1_000,
        max_retries=1,
    )

    def validator(payload: dict[str, object]) -> None:
        if payload.get("ok") is not True:
            raise InductionFailure("test_protocol_error")

    asyncio.run(
        inducer._request_with_retry(
            system_prompt=TERM_SENSE_SYSTEM_PROMPT,
            user_prompt=prompt,
            validator=validator,
            diagnostic_scope="test_retry_budget",
        )
    )

    assert len(client.prompts) == 2
    assert all(len(value) <= inducer._max_input_chars for value in client.prompts)
    assert "test_protocol_error" in client.prompts[1]


def test_existing_sense_id_must_be_known_and_non_sense_fields_are_rejected() -> None:
    block = _block("Checkpoint")
    occurrence = _occurrence("checkpoint", start=0, block=block)
    group = _candidate_group("checkpoint", block=block, occurrences=(occurrence,))
    existing = {
        "checkpoint": (
            {
                "concept_sense_id": "sense_agent_checkpoint",
                "summary": "Agent 状态保存点",
                "summary_basis": "context_inferred",
                "support_block_ids": ["block_old"],
            },
        )
    }
    accepted = {
        "i": 0,
        "term": "accepted",
        "sense": "resolved",
        "existing_sense_id": "sense_agent_checkpoint",
        "reason": "复用已有义项",
    }
    client = _QueuedJsonClient(
        (
            JsonChatResult(
                payload={
                    "decisions": [accepted],
                },
                actual_model_id="fake-json-model",
                input_tokens=10,
                output_tokens=5,
            ),
        )
    )
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=10_000,
        max_output_tokens=1_000,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.judge_candidate_groups(
            blocks_by_id={block.block_id: block},
            candidate_groups=(group,),
            occurrence_by_id=_occurrence_map(occurrence),
            existing_senses_by_key=existing,
            concurrency=1,
        )
    )

    assert result.judgements[0].existing_sense_id == "sense_agent_checkpoint"

    invalid_client = _QueuedJsonClient(
        (
            JsonChatResult(
                payload={
                    "decisions": [{**accepted, "existing_sense_id": "sense_hallucinated"}],
                },
                actual_model_id="fake-json-model",
                input_tokens=10,
                output_tokens=5,
            ),
        )
    )
    invalid_inducer = LLMTermSenseInducer(
        invalid_client,
        max_input_chars=10_000,
        max_output_tokens=1_000,
        max_retries=0,
    )
    with pytest.raises(InductionFailure, match="existing_sense_id is not valid"):
        asyncio.run(
            invalid_inducer.judge_candidate_groups(
                blocks_by_id={block.block_id: block},
                candidate_groups=(group,),
                occurrence_by_id=_occurrence_map(occurrence),
                existing_senses_by_key=existing,
                concurrency=1,
            )
        )


def test_incomplete_large_batch_is_split_until_every_candidate_is_covered() -> None:
    block = _block("alpha beta gamma delta")
    occurrences = tuple(
        _occurrence(key, start=index * 6, block=block)
        for index, key in enumerate(("alpha", "beta", "gamma", "delta"))
    )
    groups = tuple(
        _candidate_group(key, block=block, occurrences=(occurrence,))
        for key, occurrence in zip(("alpha", "beta", "gamma", "delta"), occurrences, strict=True)
    )
    client = _AdaptiveCoverageClient()
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=2_000,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.judge_candidate_groups(
            blocks_by_id={block.block_id: block},
            candidate_groups=groups,
            occurrence_by_id=_occurrence_map(*occurrences),
            existing_senses_by_key={},
            concurrency=1,
        )
    )

    assert client.request_sizes == [4, 2, 2]
    assert [item.candidate_group_id for item in result.judgements] == [
        group.candidate_group_id for group in groups
    ]
    assert all(item.term_decision is TermDecision.ACCEPTED for item in result.judgements)
    assert result.usage.attempts == 3


def test_candidate_group_count_limit_splits_requests_instead_of_skipping_block() -> None:
    keys = tuple(f"term_{index}" for index in range(49))
    block = _block(" ".join(keys))
    occurrences = tuple(
        _occurrence(key, start=index * 8, block=block) for index, key in enumerate(keys)
    )
    groups = tuple(
        _candidate_group(key, block=block, occurrences=(occurrence,))
        for key, occurrence in zip(keys, occurrences, strict=True)
    )

    def accepted_response(count: int) -> JsonChatResult:
        return JsonChatResult(
            payload={
                "decisions": [
                    {
                        "i": index,
                        "term": "accepted",
                        "sense": "mention_only",
                        "reason": "测试保留术语入口",
                    }
                    for index in range(count)
                ],
            },
            actual_model_id="fake-json-model",
            input_tokens=10,
            output_tokens=5,
        )

    client = _QueuedJsonClient((accepted_response(48), accepted_response(1)))
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=100_000,
        max_output_tokens=1_000,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.judge_candidate_groups(
            blocks_by_id={block.block_id: block},
            candidate_groups=groups,
            occurrence_by_id=_occurrence_map(*occurrences),
            existing_senses_by_key={},
            concurrency=1,
        )
    )

    assert [len(json.loads(prompt)["block"]["candidates"]) for prompt in client.prompts] == [48, 1]
    assert result.skipped_block_ids == ()
    assert [item.candidate_group_id for item in result.judgements] == [
        group.candidate_group_id for group in groups
    ]
    assert all(item.term_decision is TermDecision.ACCEPTED for item in result.judgements)


def test_consolidation_batches_independent_components_in_one_request() -> None:
    client = _ConcurrentConsolidationClient()
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=2_000,
        max_retries=0,
    )
    drafts = tuple(
        TermSenseDraft(
            draft_id=f"draft_{key}_{index}",
            normalized_keys=(key,),
            occurrence_ids=(f"occ_{key}_{index}",),
            support_block_ids=(f"block_{key}_{index}",),
            summary=f"{key} 摘要 {index}",
            summary_basis=SummaryBasis.CONTEXT_INFERRED,
            source="model",
        )
        for key in ("alpha", "beta")
        for index in range(2)
    )

    result = asyncio.run(
        inducer.consolidate_drafts(
            drafts=drafts,
            evidence_by_draft_id={draft.draft_id: (draft.summary or "",) for draft in drafts},
            concurrency=2,
        )
    )

    assert client.max_active_requests == 1
    assert client.system_prompts == [CONSOLIDATION_SYSTEM_PROMPT]
    assert len(result.drafts) == 2
    assert result.usage.requests == 1
    assert result.usage.attempts == 1


def test_stage_system_prompts_keep_stage_specific_output_contracts() -> None:
    assert "根对象只能有 decisions" in TERM_SENSE_SYSTEM_PROMPT
    assert "根对象只能有 components" in CONSOLIDATION_SYSTEM_PROMPT
    assert "根对象只能有 summaries" in SUMMARY_SYSTEM_PROMPT
    assert "不生成概念摘要" not in CONSOLIDATION_SYSTEM_PROMPT
    assert "不得合并" in SUMMARY_SYSTEM_PROMPT


def test_consolidation_output_budget_splits_independent_components() -> None:
    client = _ConcurrentConsolidationClient()
    drafts = tuple(
        TermSenseDraft(
            draft_id=f"draft_{key}",
            normalized_keys=(key,),
            occurrence_ids=(f"occ_{key}",),
            support_block_ids=(f"block_{key}",),
            source="model",
        )
        for key in ("alpha", "beta", "gamma")
    )
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=800,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.consolidate_drafts(
            drafts=drafts,
            evidence_by_draft_id={
                draft.draft_id: (f"{draft.normalized_keys[0]} 原文证据",) for draft in drafts
            },
            concurrency=1,
        )
    )

    assert len(result.drafts) == 3
    assert result.usage.requests == 2
    assert result.usage.attempts == 2
    assert result.usage.failed_attempts == 0
    assert result.skipped_draft_ids == ()
    assert client.system_prompts == [SUMMARY_SYSTEM_PROMPT] * 2
    assert all("components" not in json.loads(prompt) for prompt in client.user_prompts)


def test_consolidation_salvages_valid_components_and_retries_only_invalid_component() -> None:
    drafts = (
        *(
            TermSenseDraft(
                draft_id=f"draft_alpha_{index}",
                normalized_keys=("alpha",),
                occurrence_ids=(f"occ_alpha_{index}",),
                support_block_ids=(f"block_alpha_{index}",),
                source="model",
            )
            for index in range(2)
        ),
        *(
            TermSenseDraft(
                draft_id=f"draft_beta_{index}",
                normalized_keys=("beta",),
                occurrence_ids=(f"occ_beta_{index}",),
                support_block_ids=(f"block_beta_{index}",),
                source="model",
            )
            for index in range(2)
        ),
    )
    client = _QueuedJsonClient(
        (
            JsonChatResult(
                payload={
                    "components": [
                        {
                            "component": 0,
                            "groups": [
                                {
                                    "indices": [0, 1],
                                    "summary": "Alpha 概念",
                                    "basis": "context_inferred",
                                    "reason": "合法分量",
                                }
                            ],
                        },
                        {
                            "component": 1,
                            "groups": [
                                {
                                    "indices": [2],
                                    "summary": "Beta 分量使用了越界局部序号",
                                    "basis": "context_inferred",
                                    "reason": "测试局部失败",
                                }
                            ],
                        },
                    ]
                },
                actual_model_id="fake-json-model",
                input_tokens=10,
                output_tokens=5,
            ),
            JsonChatResult(
                payload={
                    "components": [
                        {
                            "component": 0,
                            "groups": [
                                {
                                    "indices": [0, 1],
                                    "summary": "Beta 概念",
                                    "basis": "context_inferred",
                                    "reason": "逐分量重试成功",
                                }
                            ],
                        }
                    ]
                },
                actual_model_id="fake-json-model",
                input_tokens=10,
                output_tokens=5,
            ),
        )
    )
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=1_000,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.consolidate_drafts(
            drafts=drafts,
            evidence_by_draft_id={draft.draft_id: ("原文证据",) for draft in drafts},
            concurrency=1,
        )
    )

    assert {draft.normalized_keys for draft in result.drafts} == {("alpha",), ("beta",)}
    assert len(client.prompts) == 2
    assert result.usage.requests == 2


def test_consolidation_root_failure_falls_back_to_single_components() -> None:
    drafts = tuple(
        TermSenseDraft(
            draft_id=f"draft_{key}_{index}",
            normalized_keys=(key,),
            occurrence_ids=(f"occ_{key}_{index}",),
            support_block_ids=(f"block_{key}_{index}",),
            source="model",
        )
        for key in ("alpha", "beta")
        for index in range(2)
    )

    def summary_response(summary: str) -> JsonChatResult:
        return JsonChatResult(
            payload={
                "components": [
                    {
                        "component": 0,
                        "groups": [
                            {
                                "indices": [0, 1],
                                "summary": summary,
                                "basis": "context_inferred",
                                "reason": "逐分量回退成功",
                            }
                        ],
                    }
                ]
            },
            actual_model_id="fake-json-model",
            input_tokens=10,
            output_tokens=5,
        )

    client = _QueuedJsonClient(
        (
            JsonChatResult(
                payload={"groups": []},
                actual_model_id="fake-json-model",
                input_tokens=10,
                output_tokens=5,
            ),
            summary_response("Alpha 概念"),
            summary_response("Beta 概念"),
        )
    )
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=2_000,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.consolidate_drafts(
            drafts=drafts,
            evidence_by_draft_id={draft.draft_id: ("原文证据",) for draft in drafts},
            concurrency=1,
        )
    )

    assert {draft.normalized_keys[0]: draft.summary for draft in result.drafts} == {
        "alpha": "Alpha 概念",
        "beta": "Beta 概念",
    }
    assert len(client.prompts) == 3
    assert result.usage.failed_attempts == 1


def test_consolidation_provider_failure_is_not_split_into_component_requests() -> None:
    drafts = tuple(
        TermSenseDraft(
            draft_id=f"draft_{key}_{index}",
            normalized_keys=(key,),
            occurrence_ids=(f"occ_{key}_{index}",),
            support_block_ids=(f"block_{key}_{index}",),
            source="model",
        )
        for key in ("alpha", "beta")
        for index in range(2)
    )
    client = _FailingJsonClient("chat_provider_failed:APITimeoutError")
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=2_000,
        max_retries=0,
    )

    with pytest.raises(InductionFailure, match="chat_provider_failed"):
        asyncio.run(
            inducer.consolidate_drafts(
                drafts=drafts,
                evidence_by_draft_id={draft.draft_id: ("原文证据",) for draft in drafts},
                concurrency=1,
            )
        )

    assert len(client.prompts) == 1


def test_summary_provider_failure_is_not_split_into_single_items() -> None:
    drafts = tuple(
        TermSenseDraft(
            draft_id=f"draft_{key}",
            normalized_keys=(key,),
            occurrence_ids=(f"occ_{key}",),
            support_block_ids=(f"block_{key}",),
            source="model",
        )
        for key in ("alpha", "beta")
    )
    client = _FailingJsonClient("chat_provider_failed:APITimeoutError")
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=2_000,
        max_retries=0,
    )

    with pytest.raises(InductionFailure, match="chat_provider_failed"):
        asyncio.run(
            inducer.consolidate_drafts(
                drafts=drafts,
                evidence_by_draft_id={draft.draft_id: ("原文证据",) for draft in drafts},
                concurrency=1,
            )
        )

    assert len(client.prompts) == 1


def test_oversized_consolidation_output_does_not_split_a_connected_component() -> None:
    drafts = tuple(
        TermSenseDraft(
            draft_id=f"draft_alpha_{index}",
            normalized_keys=("alpha",),
            occurrence_ids=(f"occ_alpha_{index}",),
            support_block_ids=(f"block_{index}",),
            source="model",
        )
        for index in range(2)
    )
    client = _QueuedJsonClient(())
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=20_000,
        max_output_tokens=767,
        max_retries=0,
    )

    result = asyncio.run(
        inducer.consolidate_drafts(
            drafts=drafts,
            evidence_by_draft_id={draft.draft_id: ("Alpha 原文证据",) for draft in drafts},
            concurrency=1,
        )
    )

    assert client.prompts == []
    assert result.drafts == drafts
    assert result.skipped_draft_ids == (
        "draft_alpha_0",
        "draft_alpha_1",
    )


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    (
        (
            "summary",
            "摘" * 481,
            "consolidation_summary_too_long:length=481:prompt_max=240",
        ),
        ("reason", "因" * 81, "consolidation_reason_too_long"),
    ),
)
def test_consolidation_rejects_text_beyond_output_contract_limits(
    field: str,
    value: str,
    error_code: str,
) -> None:
    draft = TermSenseDraft(
        draft_id="draft_alpha_0",
        normalized_keys=("alpha",),
        occurrence_ids=("occ_alpha_0",),
        support_block_ids=("block_0",),
        source="model",
    )
    group = {
        "indices": [0],
        "summary": "Alpha 概念",
        "basis": "context_inferred",
        "reason": "同一分量",
    }
    group[field] = value

    with pytest.raises(InductionFailure, match=error_code):
        LLMTermSenseInducer._validate_consolidation_component_groups(
            ((draft,),),
            [group],
        )


def test_consolidation_accepts_summary_at_hard_character_limit() -> None:
    draft = TermSenseDraft(
        draft_id="draft_alpha_0",
        normalized_keys=("alpha",),
        occurrence_ids=("occ_alpha_0",),
        support_block_ids=("block_0",),
        source="model",
    )
    LLMTermSenseInducer._validate_consolidation_component_groups(
        ((draft,),),
        [
            {
                "indices": [0],
                "summary": "摘" * 480,
                "basis": "context_inferred",
                "reason": "达到硬上限但未超过",
            }
        ],
    )


def test_oversized_consolidation_component_is_not_partially_merged() -> None:
    drafts = tuple(
        TermSenseDraft(
            draft_id=f"draft_alpha_{index}",
            normalized_keys=("alpha",),
            occurrence_ids=(f"occ_alpha_{index}",),
            support_block_ids=(f"block_{index}",),
            summary=f"Alpha 在第 {index} 个上下文中的概念",
            summary_basis=SummaryBasis.CONTEXT_INFERRED,
            source="model",
        )
        for index in range(2)
    )
    evidence = {draft.draft_id: (f"{draft.summary}；" + ("上下文证据" * 20),) for draft in drafts}
    client = _QueuedJsonClient(())
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=1,
        max_output_tokens=1_000,
        max_retries=0,
    )
    single_size = inducer._consolidation_request_size(((drafts[0],),), evidence)
    assert inducer._consolidation_request_size((drafts,), evidence) > single_size
    inducer._max_input_chars = single_size

    result = asyncio.run(
        inducer.consolidate_drafts(
            drafts=drafts,
            evidence_by_draft_id=evidence,
            concurrency=1,
        )
    )

    assert client.prompts == []
    assert result.drafts == drafts
    assert result.skipped_draft_ids == (
        "draft_alpha_0",
        "draft_alpha_1",
    )


def test_unrefreshed_affected_sense_is_not_published_with_stale_summary() -> None:
    scanner = CandidateScanner()
    first = _snapshot(_block("Worktree keeps Agent state isolated.", block_id="block_1"))
    base, _ = asyncio.run(
        _compiler({"worktree"}).compile(
            snapshot=first,
            scanner=scanner,
            base_artifact=None,
            concurrency=1,
        )
    )
    target = _snapshot(
        _block("Worktree keeps Agent state isolated.", block_id="block_1"),
        _block(
            "Worktree stores a recoverable checkpoint.",
            block_id="block_2",
            ordinal=1,
        ),
    )
    inducer = _SkippingConsolidationInducer({"worktree"})
    compiler = Stage51Compiler(
        inducer,  # type: ignore[arg-type]
        max_input_chars=20_000,
        max_output_tokens=2_000,
    )

    updated, _ = asyncio.run(
        compiler.compile(
            snapshot=target,
            scanner=scanner,
            base_artifact=base,
            concurrency=1,
        )
    )

    assert updated.terms[0].support_block_ids == ("block_1", "block_2")
    assert updated.concept_senses == ()
    assert updated.term_sense_links == ()
    assert updated.stats["stale_summary_suppressed_drafts"] == 1
    assert any(
        diagnostic.code == "candidate_closure_incomplete" for diagnostic in updated.diagnostics
    )
