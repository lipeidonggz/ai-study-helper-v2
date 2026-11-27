"""Incremental Knowledge Map Ledger projection and read-only delta planning."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from app.profile_extraction.util import (
    json_loads_string_list,
    stable_hash,
)
from app.providers.embeddings import EmbeddingProvider
from app.storage.profile_extraction_models import PreviewCandidateRow
from app.wiki.profile_knowledge_map_models import (
    ProfileKnowledgeMapManifest,
    ProfileMapGroup,
    ProfileMapTopic,
)

INCREMENTAL_GROUP_CANDIDATE_LIMIT = 16


class CandidateSourceRecord(BaseModel):
    """一个 candidate 的跨 Run 稳定来源身份。"""

    candidate_id: str
    source_key: str = Field(pattern=r"^src_[0-9a-f]{64}$")
    doc_id: str
    doc_version: int = Field(ge=1)
    normalized_name: str
    display_name: str
    summaries: list[str] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    source_block_ids: list[str] = Field(min_length=1)
    channel_ids: list[str] = Field(default_factory=list)


class TopicLedgerEntry(BaseModel):
    """legacy map 中可参与增量计划的一个 Topic Ledger 条目。"""

    topic_lineage_id: str
    topic_revision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    topic: ProfileMapTopic
    source_keys: list[str] = Field(min_length=1)


class GroupLedgerEntry(BaseModel):
    """legacy map 中可参与增量计划的一个 Group Ledger 条目。"""

    group_lineage_id: str
    group_revision_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    group: ProfileMapGroup


class GroupMembershipEntry(BaseModel):
    """legacy Ledger 投影中的 Group membership。"""

    group_lineage_id: str
    topic_lineage_id: str
    role: Literal["primary", "secondary"]
    retrieval_duty: str
    provenance: Literal["legacy_v4_projection"] = "legacy_v4_projection"


class KnowledgeMapLedgerSnapshot(BaseModel):
    """从不可变 map 投影出的只读 Ledger snapshot。"""

    schema_version: Literal["incremental-map-ledger-snapshot-v1"] = (
        "incremental-map-ledger-snapshot-v1"
    )
    map_id: str
    source_key_by_candidate_id: dict[str, str]
    topics: list[TopicLedgerEntry]
    groups: list[GroupLedgerEntry]
    memberships: list[GroupMembershipEntry]


class SourceUpdatePlan(BaseModel):
    """同一文档中同一 normalized name 的旧 candidate → 新 candidate。"""

    doc_id: str
    normalized_name: str
    old_source_key: str
    new_source_key: str


class CandidateDeltaPlan(BaseModel):
    """candidate source 集合的精确 delta。"""

    unchanged_source_keys: list[str] = Field(default_factory=list)
    added_source_keys: list[str] = Field(default_factory=list)
    removed_source_keys: list[str] = Field(default_factory=list)
    updated_sources: list[SourceUpdatePlan] = Field(default_factory=list)


class DocumentDeltaPlan(BaseModel):
    """文档范围及 candidate 内容变化的摘要。"""

    added_document_ids: list[str] = Field(default_factory=list)
    removed_document_ids: list[str] = Field(default_factory=list)
    changed_document_ids: list[str] = Field(default_factory=list)
    unchanged_document_ids: list[str] = Field(default_factory=list)


class TopicDeltaPlan(BaseModel):
    """Topic resolver 需要处理的来源与 lineage。"""

    unchanged_topic_lineage_ids: list[str] = Field(default_factory=list)
    revised_topic_lineage_ids: list[str] = Field(default_factory=list)
    retired_topic_lineage_ids: list[str] = Field(default_factory=list)
    new_source_keys: list[str] = Field(default_factory=list)
    removed_source_keys_by_topic: dict[str, list[str]] = Field(default_factory=dict)


class CandidateGroupScore(BaseModel):
    """pending source 到现有 Group 的候选相似度。"""

    group_lineage_id: str
    score: float


class PendingSourceGroupCandidates(BaseModel):
    """一个 pending source 的有界 Group 候选。"""

    source_key: str
    candidate_groups: list[CandidateGroupScore] = Field(default_factory=list)


class IncrementalMapPlan(BaseModel):
    """只读增量计划；不包含任何已执行 Ledger mutation。"""

    schema_version: Literal["incremental-knowledge-map-plan-v1"] = (
        "incremental-knowledge-map-plan-v1"
    )
    status: Literal["noop", "incremental", "full", "full_rebuild_required"]
    base_map_id: str | None
    base_snapshot_id: str | None
    target_snapshot_ids: list[str]
    base_manifest_hash: str | None
    full_rebuild_reason: str | None = None
    base_candidate_count: int = Field(ge=0)
    target_candidate_count: int = Field(ge=0)
    documents: DocumentDeltaPlan
    candidates: CandidateDeltaPlan
    topics: TopicDeltaPlan
    affected_current_group_lineage_ids: list[str] = Field(default_factory=list)
    pending_source_group_candidates: list[PendingSourceGroupCandidates] = Field(
        default_factory=list
    )
    blockers: list[str] = Field(default_factory=list)


def candidate_source_record(row: PreviewCandidateRow) -> CandidateSourceRecord:
    """构造不依赖 candidate ID、Run ID 或数据库 rowid 的来源身份。"""

    aliases = [alias for alias in json_loads_string_list(row.aliases_json) if alias.strip()]
    summaries = json_loads_string_list(row.summaries_json)
    source_block_ids = json_loads_string_list(row.source_block_ids_json)
    channel_ids = json_loads_string_list(row.channel_ids_json)
    selection_reasons = _json_value(row.selection_reasons_json, [])
    missing_anchor_types = _json_value(row.missing_anchor_types_json, [])
    name_anchor_source_block_ids = _json_value(
        row.name_anchor_source_block_ids_json,
        [],
    )
    fingerprint = stable_hash(
        {
            "doc_id": row.doc_id,
            "doc_version": row.doc_version,
            "name": row.name,
            "normalized_name": row.normalized_name,
            "provisional_name": row.provisional_name,
            "title_kind": row.title_kind,
            "title_standalone_clear": row.title_standalone_clear,
            "missing_anchor_types": missing_anchor_types,
            "name_changed": row.name_changed,
            "aliases": aliases,
            "summaries": summaries,
            "selection_reasons": selection_reasons,
            "name_anchor_source_block_ids": name_anchor_source_block_ids,
            "name_anchor_reason": row.name_anchor_reason,
            "source_block_ids": source_block_ids,
            "channel_ids": channel_ids,
            "content_hash": row.content_hash,
        }
    )
    return CandidateSourceRecord(
        candidate_id=row.candidate_id,
        source_key=f"src_{fingerprint}",
        doc_id=row.doc_id,
        doc_version=row.doc_version,
        normalized_name=row.normalized_name,
        display_name=row.name,
        summaries=summaries,
        aliases=aliases,
        source_block_ids=source_block_ids,
        channel_ids=channel_ids,
    )


def project_legacy_ledger(
    *,
    manifest: ProfileKnowledgeMapManifest,
    candidate_records: list[CandidateSourceRecord],
) -> KnowledgeMapLedgerSnapshot:
    """把当前 v4 map 投影成只读 legacy Ledger，供 delta planner 使用。"""

    candidate_by_id = {record.candidate_id: record for record in candidate_records}
    source_key_by_candidate_id: dict[str, str] = {}
    topic_entries: list[TopicLedgerEntry] = []
    for topic in manifest.topics:
        source_keys: list[str] = []
        for source in topic.sources:
            record = candidate_by_id.get(source.candidate_id)
            if record is None:
                raise ValueError(
                    f"base CandidateRow is missing for source identity: {source.candidate_id}"
                )
            source_key_by_candidate_id[source.candidate_id] = record.source_key
            source_keys.append(record.source_key)
        ordered_source_keys = sorted(set(source_keys))
        revision_id = stable_hash(
            {
                "topic": topic.model_dump(mode="json"),
                "source_keys": ordered_source_keys,
            }
        )
        topic_entries.append(
            TopicLedgerEntry(
                topic_lineage_id=topic.topic_id,
                topic_revision_id=revision_id,
                topic=topic,
                source_keys=ordered_source_keys,
            )
        )

    group_entries: list[GroupLedgerEntry] = []
    memberships: list[GroupMembershipEntry] = []
    topic_ids_to_groups: dict[str, list[str]] = defaultdict(list)
    for group in manifest.groups:
        revision_id = stable_hash(group.model_dump(mode="json"))
        group_entries.append(
            GroupLedgerEntry(
                group_lineage_id=group.group_id,
                group_revision_id=revision_id,
                group=group,
            )
        )
        for topic_id in group.child_topic_ids:
            topic_ids_to_groups[topic_id].append(group.group_id)

    for topic_id, group_ids in sorted(topic_ids_to_groups.items()):
        for index, group_id in enumerate(sorted(set(group_ids))):
            memberships.append(
                GroupMembershipEntry(
                    group_lineage_id=group_id,
                    topic_lineage_id=topic_id,
                    role="primary" if index == 0 else "secondary",
                    retrieval_duty=(
                        "legacy primary membership"
                        if index == 0
                        else "legacy additional group membership"
                    ),
                )
            )

    return KnowledgeMapLedgerSnapshot(
        map_id=manifest.map_id,
        source_key_by_candidate_id=source_key_by_candidate_id,
        topics=topic_entries,
        groups=group_entries,
        memberships=memberships,
    )


async def plan_incremental_update(
    *,
    base_manifest: ProfileKnowledgeMapManifest | None,
    base_ledger: KnowledgeMapLedgerSnapshot | None,
    group_vectors: dict[str, list[float]],
    base_records: list[CandidateSourceRecord],
    target_records: list[CandidateSourceRecord],
    target_snapshot_ids: list[str],
    embedding_provider: EmbeddingProvider,
    contract_compatibility_reason: str | None = None,
) -> IncrementalMapPlan:
    """比较 base 与 target candidates，产出只读 Topic／Group delta 计划。"""

    target_source_keys = {record.source_key for record in target_records}
    target_by_key = {record.source_key: record for record in target_records}
    base_source_keys = {record.source_key for record in base_records}
    base_by_key = {record.source_key: record for record in base_records}

    removed_keys = sorted(base_source_keys - target_source_keys)
    added_keys = sorted(target_source_keys - base_source_keys)
    unchanged_keys = sorted(base_source_keys & target_source_keys)
    updated_sources, matched_removed, matched_added = _match_updated_sources(
        base_by_key=base_by_key,
        target_by_key=target_by_key,
        removed_keys=removed_keys,
        added_keys=added_keys,
    )
    remaining_removed = set(removed_keys) - matched_removed
    remaining_added = set(added_keys) - matched_added

    documents = _document_delta(
        base_records=base_records,
        target_records=target_records,
    )
    if base_manifest is None or base_ledger is None:
        return IncrementalMapPlan(
            status="full",
            base_map_id=None,
            base_snapshot_id=None,
            target_snapshot_ids=sorted(set(target_snapshot_ids)),
            base_manifest_hash=None,
            full_rebuild_reason=None,
            base_candidate_count=len(base_records),
            target_candidate_count=len(target_records),
            documents=documents,
            candidates=CandidateDeltaPlan(
                unchanged_source_keys=unchanged_keys,
                added_source_keys=sorted(target_source_keys),
                removed_source_keys=sorted(base_source_keys),
                updated_sources=updated_sources,
            ),
            topics=TopicDeltaPlan(new_source_keys=sorted(target_source_keys)),
        )
    if contract_compatibility_reason is not None:
        return IncrementalMapPlan(
            status="full_rebuild_required",
            base_map_id=base_manifest.map_id,
            base_snapshot_id=base_manifest.snapshot_id,
            target_snapshot_ids=sorted(set(target_snapshot_ids)),
            base_manifest_hash=base_manifest.manifest_hash,
            full_rebuild_reason=contract_compatibility_reason,
            base_candidate_count=len(base_records),
            target_candidate_count=len(target_records),
            documents=documents,
            candidates=CandidateDeltaPlan(
                unchanged_source_keys=unchanged_keys,
                added_source_keys=sorted(target_source_keys),
                removed_source_keys=sorted(base_source_keys),
                updated_sources=updated_sources,
            ),
            topics=TopicDeltaPlan(new_source_keys=sorted(target_source_keys)),
        )

    topic_delta, affected_group_ids = _topic_delta(
        base_ledger=base_ledger,
        remaining_removed=remaining_removed,
        remaining_added=remaining_added,
        updated_sources=updated_sources,
    )
    pending_source_keys = sorted(set(topic_delta.new_source_keys))
    group_candidates = await _pending_group_candidates(
        pending_source_keys=pending_source_keys,
        target_by_key=target_by_key,
        base_manifest=base_manifest,
        group_vectors=group_vectors,
        embedding_provider=embedding_provider,
    )
    has_delta = bool(added_keys or removed_keys or updated_sources)
    return IncrementalMapPlan(
        status="incremental" if has_delta else "noop",
        base_map_id=base_manifest.map_id,
        base_snapshot_id=base_manifest.snapshot_id,
        target_snapshot_ids=sorted(set(target_snapshot_ids)),
        base_manifest_hash=base_manifest.manifest_hash,
        full_rebuild_reason=contract_compatibility_reason,
        base_candidate_count=len(base_records),
        target_candidate_count=len(target_records),
        documents=documents,
        candidates=CandidateDeltaPlan(
            unchanged_source_keys=unchanged_keys,
            added_source_keys=added_keys,
            removed_source_keys=removed_keys,
            updated_sources=updated_sources,
        ),
        topics=topic_delta,
        affected_current_group_lineage_ids=sorted(affected_group_ids),
        pending_source_group_candidates=group_candidates,
    )


def _topic_delta(
    *,
    base_ledger: KnowledgeMapLedgerSnapshot,
    remaining_removed: set[str],
    remaining_added: set[str],
    updated_sources: list[SourceUpdatePlan],
) -> tuple[TopicDeltaPlan, set[str]]:
    updated_old_keys = {update.old_source_key for update in updated_sources}
    topic_by_lineage = {entry.topic_lineage_id: entry for entry in base_ledger.topics}
    memberships_by_topic: dict[str, list[str]] = defaultdict(list)
    for membership in base_ledger.memberships:
        memberships_by_topic[membership.topic_lineage_id].append(membership.group_lineage_id)

    removed_by_topic: dict[str, list[str]] = {}
    updated_new_keys = {update.new_source_key for update in updated_sources}
    revised: set[str] = set()
    retired: set[str] = set()
    affected_groups: set[str] = set()
    for topic_lineage_id, entry in topic_by_lineage.items():
        removed_for_topic = sorted(set(entry.source_keys) & (remaining_removed | updated_old_keys))
        if not removed_for_topic:
            continue
        removed_by_topic[topic_lineage_id] = removed_for_topic
        has_unremoved_source = bool(set(entry.source_keys) - set(removed_for_topic))
        has_update = bool(set(removed_for_topic) & updated_old_keys)
        if has_unremoved_source or has_update:
            revised.add(topic_lineage_id)
        else:
            retired.add(topic_lineage_id)
        affected_groups.update(memberships_by_topic.get(topic_lineage_id, []))

    unchanged = sorted(set(topic_by_lineage) - revised - retired)
    return (
        TopicDeltaPlan(
            unchanged_topic_lineage_ids=unchanged,
            revised_topic_lineage_ids=sorted(revised),
            retired_topic_lineage_ids=sorted(retired),
            new_source_keys=sorted(remaining_added | updated_new_keys),
            removed_source_keys_by_topic=removed_by_topic,
        ),
        affected_groups,
    )


async def _pending_group_candidates(
    *,
    pending_source_keys: list[str],
    target_by_key: dict[str, CandidateSourceRecord],
    base_manifest: ProfileKnowledgeMapManifest,
    group_vectors: dict[str, list[float]],
    embedding_provider: EmbeddingProvider,
) -> list[PendingSourceGroupCandidates]:
    if not pending_source_keys or not group_vectors:
        return []
    texts = [
        _candidate_group_query_text(target_by_key[source_key], base_manifest)
        for source_key in pending_source_keys
    ]
    vectors: list[list[float]] = []
    for start in range(0, len(texts), embedding_provider.batch_size):
        batch = texts[start : start + embedding_provider.batch_size]
        batch_result = await embedding_provider.embed_documents(batch)
        vectors.extend(batch_result.vectors)
    group_ids = sorted(group_vectors)
    normalized_groups = [_normalize_vector(group_vectors[group_id]) for group_id in group_ids]
    result: list[PendingSourceGroupCandidates] = []
    for source_key, vector in zip(pending_source_keys, vectors, strict=True):
        normalized_query = _normalize_vector(vector)
        scores = [
            CandidateGroupScore(
                group_lineage_id=group_id,
                score=round(_dot(normalized_query, group_vector), 6),
            )
            for group_id, group_vector in zip(group_ids, normalized_groups, strict=True)
        ]
        scores.sort(key=lambda item: (-item.score, item.group_lineage_id))
        result.append(
            PendingSourceGroupCandidates(
                source_key=source_key,
                candidate_groups=scores[:INCREMENTAL_GROUP_CANDIDATE_LIMIT],
            )
        )
    return result


def _candidate_group_query_text(
    record: CandidateSourceRecord,
    manifest: ProfileKnowledgeMapManifest,
) -> str:
    return "\n".join(
        [
            record.display_name,
            *record.aliases,
            *record.summaries,
            manifest.profile_context,
            *manifest.representative_questions,
        ]
    )


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return [0.0 for _ in vector]
    return [value / norm for value in vector]


def _dot(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding dimensions do not match")
    return sum(a * b for a, b in zip(left, right, strict=True))


def _match_updated_sources(
    *,
    base_by_key: dict[str, CandidateSourceRecord],
    target_by_key: dict[str, CandidateSourceRecord],
    removed_keys: list[str],
    added_keys: list[str],
) -> tuple[list[SourceUpdatePlan], set[str], set[str]]:
    removed_by_identity = {
        (base_by_key[source_key].doc_id, base_by_key[source_key].normalized_name): source_key
        for source_key in removed_keys
    }
    added_by_identity = {
        (target_by_key[source_key].doc_id, target_by_key[source_key].normalized_name): source_key
        for source_key in added_keys
    }
    updated: list[SourceUpdatePlan] = []
    matched_removed: set[str] = set()
    matched_added: set[str] = set()
    for identity in sorted(set(removed_by_identity) & set(added_by_identity)):
        old_source_key = removed_by_identity[identity]
        new_source_key = added_by_identity[identity]
        updated.append(
            SourceUpdatePlan(
                doc_id=identity[0],
                normalized_name=identity[1],
                old_source_key=old_source_key,
                new_source_key=new_source_key,
            )
        )
        matched_removed.add(old_source_key)
        matched_added.add(new_source_key)
    return updated, matched_removed, matched_added


def _document_delta(
    *,
    base_records: list[CandidateSourceRecord],
    target_records: list[CandidateSourceRecord],
) -> DocumentDeltaPlan:
    base_doc_keys: dict[str, set[str]] = defaultdict(set)
    target_doc_keys: dict[str, set[str]] = defaultdict(set)
    for record in base_records:
        base_doc_keys[record.doc_id].add(record.source_key)
    for record in target_records:
        target_doc_keys[record.doc_id].add(record.source_key)
    base_docs = set(base_doc_keys)
    target_docs = set(target_doc_keys)
    common_docs = base_docs & target_docs
    changed_docs = sorted(
        doc_id for doc_id in common_docs if base_doc_keys[doc_id] != target_doc_keys[doc_id]
    )
    unchanged_docs = sorted(common_docs - set(changed_docs))
    return DocumentDeltaPlan(
        added_document_ids=sorted(target_docs - base_docs),
        removed_document_ids=sorted(base_docs - target_docs),
        changed_document_ids=changed_docs,
        unchanged_document_ids=unchanged_docs,
    )


def _json_value(value: str, default: object) -> object:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
