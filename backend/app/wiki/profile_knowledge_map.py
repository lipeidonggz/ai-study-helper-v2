from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import numpy as np
from pydantic import AliasChoices, BaseModel, Field
from sqlalchemy import select

from app.profile_extraction.models import ExtractionProfileConfig, ProfileLanguagePolicy
from app.profile_extraction.preview_service import CANDIDATE_PIPELINE_VERSION
from app.profile_extraction.util import (
    DEFAULT_PROFILE_ID,
    estimate_tokens,
    json_dumps,
    json_loads_string_list,
    new_identifier,
    normalize_name,
    ordered_unique,
    ordered_unique_normalized_text,
    stable_hash,
)
from app.providers.chat import ChatMessage, ChatProvider, ChatProviderError
from app.providers.embeddings import EmbeddingProvider, EmbeddingProviderError
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import RetrievalBlock
from app.retrieval.service import RetrievalReadService, RetrievalSnapshotReader
from app.storage.database import StorageDatabase
from app.storage.profile_extraction_models import (
    ExtractionProfileRow,
    KnowledgeBaseProfileSelectionRow,
    PreviewCandidateRow,
    ProfilePreviewRunRow,
)
from app.wiki.incremental_map_ledger import (
    CandidateSourceRecord,
    IncrementalMapPlan,
    PendingSourceGroupCandidates,
    candidate_source_record,
    project_legacy_ledger,
)
from app.wiki.incremental_map_ledger import (
    plan_incremental_update as build_incremental_map_plan,
)
from app.wiki.profile_knowledge_map_models import (
    BlockAspectExclusion,
    BlockAspectSupport,
    MapConvergenceReport,
    MapEmbeddingContract,
    PersistedIdentityVector,
    ProfileAspectDefinition,
    ProfileAspectSchema,
    ProfileKnowledgeMapActivePointer,
    ProfileKnowledgeMapManifest,
    ProfileKnowledgeMapResult,
    ProfileMapCompileContract,
    ProfileMapGroup,
    ProfileMapTopic,
    ProfileMapTopicSource,
    SearchWikiTopicV2AspectCoverage,
    SearchWikiTopicV2AspectMatch,
    SearchWikiTopicV2EvidenceSelection,
    SearchWikiTopicV2Execution,
    SearchWikiTopicV2GroupMatch,
    SearchWikiTopicV2GroupSelection,
    SearchWikiTopicV2Result,
    SearchWikiTopicV2TopicMatch,
    TopicAspect,
    TopicCandidateMembership,
    TopicIdentityCompileParameters,
    TopicIdentityJudgementAudit,
    TopicLedgerOperation,
    TopicSummaryCandidateRef,
    TopicSummarySource,
    TopicWorkItemDecision,
)

TOPIC_IDENTITY_PROMPT_VERSION = "profile-knowledge-map-topic-identity-v2"
TOPIC_IDENTITY_POLICY_VERSION = "profile-knowledge-map-work-set-v4"
GROUPING_PROMPT_VERSION = "profile-knowledge-map-grouping-v3"
GROUPING_POLICY_VERSION = "profile-knowledge-map-merge-only-groups-v8"
GROUP_EMBEDDING_INPUT_VERSION = "profile-knowledge-map-group-text-v1"
TOPIC_EMBEDDING_INPUT_VERSION = "profile-knowledge-map-topic-text-v1"
TOPIC_IDENTITY_EMBEDDING_INPUT_VERSION = "profile-knowledge-map-topic-summary-identity-v1"
ASPECT_EMBEDDING_INPUT_VERSION = "profile-knowledge-map-aspect-text-v1"
ASPECT_PROMPT_VERSION = "profile-knowledge-map-aspect-compiler-v2"
ASPECT_POLICY_VERSION = "profile-knowledge-map-aspect-catalog-v2"
TOPIC_IDENTITY_SIMILARITY_THRESHOLD = 0.78
TOPIC_IDENTITY_MAX_TARGET_PAIRS = 200
TOPIC_IDENTITY_MAX_TOTAL_TOKENS = 200_000
TOPIC_IDENTITY_MAX_PAIRS_PER_REQUEST = 20
TOPIC_IDENTITY_MAX_INPUT_TOKENS_PER_REQUEST = 32_000
TOPIC_IDENTITY_MAX_OUTPUT_TOKENS_PER_REQUEST = 8_192
TOPIC_IDENTITY_OUTPUT_TOKENS_PER_PAIR_RESERVE = 256
GROUP_INPUT_MAX_ITEMS = 32
MAP_TOPIC_GUARD = 50_000
MAP_GROUP_GUARD = 20_000
GROUP_MERGE_BATCH_SIZE = 32
GROUP_MERGE_MAX_PASSES = 8
MAP_BUILD_MAX_PARALLEL_CHAT_CALLS = 10
MAP_BUILD_MAX_PARALLEL_EMBEDDING_CALLS = 3
GROUP_CANDIDATE_SAFETY_LIMIT = 256
GROUP_SELECTION_SAFETY_LIMIT = 64
TOPIC_CANDIDATE_SAFETY_LIMIT = 256
ASPECT_CANDIDATE_SAFETY_LIMIT = 256
ASPECT_CATALOG_TOPICS_PER_BATCH = 16
ASPECT_MAPPING_TOPICS_PER_BATCH = 16
ASPECT_MAPPING_MAX_BLOCKS = 16
GLOBAL_NEIGHBOR_TOP_K = 8
GLOBAL_NEIGHBOR_SCORE_FLOOR = 0.78
GLOBAL_NEIGHBOR_CHUNK_SIZE = 128
GLOBAL_LEXICAL_EDGE_FLOOR = 0.34
GLOBAL_SOURCE_OVERLAP_FLOOR = 0.50
GLOBAL_BLOCKING_NEIGHBOR_TOP_K = 8
GROUP_TOPIC_OVERLAP_FLOOR = 0.34
GROUP_AUTO_MERGE_COSINE_FLOOR = 0.95
GROUP_AUTO_MERGE_TOPIC_JACCARD_FLOOR = 0.50
GROUP_MARGINAL_RELEVANCE_WEIGHT = 0.70
GROUP_MARGINAL_TOPIC_WEIGHT = 0.30
GROUP_HIGH_RELEVANCE_FLOOR = 0.61
GROUP_RESCUE_RELEVANCE_FLOOR = 0.52
GROUP_TOPIC_COVERAGE_FLOOR = 0.50
ASPECT_QUERY_RELEVANCE_FLOOR = 0.58
ASPECT_IMPORTANCE_RESCUE_FLOOR = 0.62
INCREMENTAL_GROUP_ATTACH_SCORE_FLOOR = 0.62
EVIDENCE_CANDIDATE_LIMIT = 256
EVIDENCE_OUTPUT_LIMIT = 96
EVIDENCE_OUTPUT_TOKEN_LIMIT = 50_000
STRUCTURED_OUTPUT_MAX_ATTEMPTS = 2
GROUPING_MAX_ATTEMPTS = 3
GROUPING_MAX_OUTPUT_TOKENS = 12_000
GROUPING_RETRY_MAX_OUTPUT_TOKENS = 24_000
BLOCK_SELECTION_MAX_OUTPUT_TOKENS = 8_192
BLOCK_SELECTION_RETRY_MAX_OUTPUT_TOKENS = 16_384
ASPECT_CATALOG_MAX_OUTPUT_TOKENS = 16_384
ASPECT_MAPPING_MAX_OUTPUT_TOKENS = 16_384

PROFILE_ASPECT_DEFINITIONS = [
    ProfileAspectDefinition(
        kind="definition",
        label="定义",
        description="说明对象是什么、中心含义和适用对象。",
        importance=0.90,
    ),
    ProfileAspectDefinition(
        kind="purpose",
        label="目的与作用",
        description="说明对象解决什么问题、产生什么价值。",
        importance=0.75,
    ),
    ProfileAspectDefinition(
        kind="mechanism",
        label="工作机制",
        description="说明对象如何工作、由哪些步骤或机制实现。",
        importance=0.90,
    ),
    ProfileAspectDefinition(
        kind="structure",
        label="结构与组成",
        description="说明对象的组成部分、层级或结构关系。",
        importance=0.70,
    ),
    ProfileAspectDefinition(
        kind="prerequisite",
        label="前提条件",
        description="说明适用前必须满足的条件或依赖。",
        importance=0.65,
    ),
    ProfileAspectDefinition(
        kind="boundary",
        label="边界与范围",
        description="说明对象能做什么、不能做什么以及可达范围。",
        importance=0.85,
    ),
    ProfileAspectDefinition(
        kind="permission",
        label="权限与授权",
        description="说明访问权限、授权方式和权限限制。",
        importance=0.80,
    ),
    ProfileAspectDefinition(
        kind="lifecycle",
        label="生命周期与状态",
        description="说明对象的创建、运行、持久化、恢复或终止状态。",
        importance=0.75,
    ),
    ProfileAspectDefinition(
        kind="failure_mode",
        label="失败模式",
        description="说明典型失败、错误累积或恢复问题。",
        importance=0.85,
    ),
    ProfileAspectDefinition(
        kind="risk",
        label="风险",
        description="说明安全、质量、成本或其他负面后果。",
        importance=0.85,
    ),
    ProfileAspectDefinition(
        kind="mitigation",
        label="缓解措施",
        description="说明如何降低风险、限制损害或恢复失败。",
        importance=0.80,
    ),
    ProfileAspectDefinition(
        kind="tradeoff",
        label="取舍",
        description="说明不同目标、成本、能力或安全属性之间的权衡。",
        importance=0.65,
    ),
    ProfileAspectDefinition(
        kind="limitation",
        label="限制",
        description="说明能力上限、已知不足和残余问题。",
        importance=0.80,
    ),
    ProfileAspectDefinition(
        kind="example",
        label="示例",
        description="提供有助于解释对象的具体示例。",
        importance=0.40,
    ),
    ProfileAspectDefinition(
        kind="applicability",
        label="适用范围",
        description="说明适用场景、不适用场景和环境约束。",
        importance=0.65,
    ),
]


class ProfileKnowledgeMapError(RuntimeError):
    """知识地图构建或检索的可分类失败。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.diagnostics = diagnostics or {}


class _GroupDraft(BaseModel):
    label: str = Field(
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("label", "group_label"),
    )
    aliases: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=1, max_length=1_000)
    retrieval_questions: list[str] = Field(default_factory=list, max_length=12)
    item_ids: list[str] = Field(min_length=1, max_length=512)


class _SelectedGroupItem(BaseModel):
    group_id: str
    relevance: Literal["direct", "partial"]
    supported_topic_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=600)


class _GroupSelectionPayload(BaseModel):
    groups: list[_SelectedGroupItem]


class _SupplementalGroupItem(BaseModel):
    group_id: str
    new_dimension: str = Field(min_length=1, max_length=300)
    supported_topic_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=600)


class _GroupCoverageAuditPayload(BaseModel):
    supplemental_groups: list[_SupplementalGroupItem]


class _UncertainIdentityPair(BaseModel):
    left_id: str
    right_id: str


class _TopicIdentityJudgement(BaseModel):
    left_node_ref: str = Field(min_length=1)
    right_node_ref: str = Field(min_length=1)
    relation: Literal["same", "different", "uncertain"]
    reason: str = Field(min_length=1, max_length=1_000)


class _TopicIdentityJudgementsPayload(BaseModel):
    judgements: list[_TopicIdentityJudgement] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _TopicPairDecisionCacheEntry:
    pair_key: str
    left_node_ref: str
    right_node_ref: str
    relation: Literal["same", "different", "uncertain"]
    judge_contract_hash: str
    judgement_id: str


@dataclass(frozen=True, slots=True)
class _IdentityTargetProposal:
    topic: ProfileMapTopic
    target_node_ref: str
    embedding_similarity: float
    has_exact_name_match: bool


@dataclass(frozen=True, slots=True)
class _WorkItem:
    node_type: Literal["topic", "candidate"]
    node_ref: str
    enqueued_reason: str


@dataclass(frozen=True, slots=True)
class _CandidateNode:
    node_ref: str
    extraction_run_id: str
    candidate_id: str
    title: str
    aliases: list[str]
    summaries: list[str]
    source_block_ids: list[str]
    row: PreviewCandidateRow


@dataclass(frozen=True, slots=True)
class _ResolvedTopicLedger:
    topics: list[ProfileMapTopic]
    topic_vectors: dict[str, list[float]]
    candidate_identity_vectors: dict[str, PersistedIdentityVector]
    topic_identity_vectors: dict[str, PersistedIdentityVector]
    usage: list[dict[str, object]]
    convergence: MapConvergenceReport
    judgements: list[TopicIdentityJudgementAudit]
    decisions: list[TopicWorkItemDecision]
    operations: list[TopicLedgerOperation]


class _ActiveTopicVectorIndex:
    """compile attempt 内只索引当前 active Topic revision 的稠密向量。"""

    def __init__(self, capacity: int) -> None:
        self._matrix = np.empty((max(1, capacity), 1024), dtype=np.float32)
        self._size = 0
        self._row_by_lineage: dict[str, int] = {}
        self._node_ref_by_row: list[str | None] = [None] * max(1, capacity)
        self._free_rows: list[int] = []

    def upsert(self, *, lineage_id: str, node_ref: str, vector: list[float]) -> None:
        """新增或覆盖一个 Topic lineage 的当前 revision 向量。"""

        normalized = np.asarray(vector, dtype=np.float32)
        if normalized.ndim != 1 or normalized.shape[0] != 1024:
            raise ValueError("Topic identity vectors must have dimension 1024")
        row = self._row_by_lineage.get(lineage_id)
        if row is None:
            if self._free_rows:
                row = self._free_rows.pop()
            elif self._size >= self._matrix.shape[0]:
                grown = np.empty(
                    (max(1, self._matrix.shape[0] * 2), 1024),
                    dtype=np.float32,
                )
                grown[: self._size] = self._matrix[: self._size]
                self._matrix = grown
                self._node_ref_by_row.extend([None] * (grown.shape[0] - len(self._node_ref_by_row)))
            else:
                row = self._size
                self._size += 1
        self._matrix[row] = normalized
        self._node_ref_by_row[row] = node_ref
        self._row_by_lineage[lineage_id] = row

    def remove(self, lineage_id: str) -> None:
        """移除一个退役或已被合并的 Topic lineage。"""

        row = self._row_by_lineage.pop(lineage_id, None)
        if row is not None:
            self._node_ref_by_row[row] = None
            self._free_rows.append(row)

    def search_at_or_above(
        self,
        *,
        vector: list[float],
        threshold: float,
    ) -> list[tuple[str, float]]:
        """返回全部达到阈值的 active Topic，不应用 top-k 截断。"""

        if self._size == 0:
            return []
        query = np.asarray(vector, dtype=np.float32)
        scores = self._matrix[: self._size] @ query
        result = [
            (cast(str, self._node_ref_by_row[row]), float(score))
            for row, score in enumerate(scores.tolist())
            if self._node_ref_by_row[row] is not None and score >= threshold
        ]
        return sorted(result, key=lambda item: (-item[1], item[0]))


class _GroupingPayload(BaseModel):
    groups: list[_GroupDraft] = Field(min_length=1, max_length=80)
    uncertain_pairs: list[_UncertainIdentityPair] = Field(default_factory=list, max_length=256)


class _GroupMergeAction(BaseModel):
    group_ids: list[str] = Field(min_length=1, max_length=GROUP_MERGE_BATCH_SIZE)
    decision: Literal["merge", "keep"]
    label: str | None = Field(default=None, min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    summary: str | None = Field(default=None, min_length=1, max_length=1_000)
    retrieval_questions: list[str] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=1, max_length=600)


class _GroupMergePayload(BaseModel):
    actions: list[_GroupMergeAction] = Field(min_length=1, max_length=80)
    uncertain_pairs: list[_UncertainIdentityPair] = Field(default_factory=list, max_length=256)


class _AspectDraft(BaseModel):
    kind: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    importance: float = Field(ge=0, le=1)
    source_candidate_ids: list[str] = Field(min_length=1)


class _TopicAspectCatalogDraft(BaseModel):
    topic_id: str
    aspects: list[_AspectDraft] = Field(min_length=1, max_length=32)


class _AspectCatalogPayload(BaseModel):
    topics: list[_TopicAspectCatalogDraft] = Field(min_length=1)


class _AspectExtensionTopicDraft(BaseModel):
    topic_id: str
    aspects: list[_AspectDraft] = Field(default_factory=list, max_length=16)


class _AspectExtensionPayload(BaseModel):
    topics: list[_AspectExtensionTopicDraft] = Field(default_factory=list)


class _BlockAspectDraft(BaseModel):
    block_id: str
    topic_id: str
    aspect_id: str = Field(pattern=r"^ta_[0-9a-f]{16}$")
    support_kind: Literal["direct", "partial"]
    support_strength: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=600)


class _BlockAspectExclusionDraft(BaseModel):
    block_id: str
    topic_id: str
    classification: Literal[
        "weak_background",
        "duplicate",
        "outside_topic",
        "non_evidence",
        "missing_aspect",
    ]
    reason: str = Field(min_length=1, max_length=600)


class _MappedBlockExclusion(BaseModel):
    block_id: str
    topic_id: str
    topic_revision_id: str
    classification: Literal[
        "weak_background",
        "duplicate",
        "outside_topic",
        "non_evidence",
        "missing_aspect",
    ]
    reason: str = Field(min_length=1, max_length=600)


class _AspectMappingPayload(BaseModel):
    supports: list[_BlockAspectDraft] = Field(default_factory=list)
    exclusions: list[_BlockAspectExclusionDraft] = Field(default_factory=list)


class _AspectEvidenceAuditItem(BaseModel):
    block_id: str
    relevance: Literal["direct", "partial"]
    supported_aspect_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=600)
    duplicate_of: str | None = None
    conflicts_with: list[str] = Field(default_factory=list)


class _AspectCoverageItem(BaseModel):
    aspect_id: str = Field(pattern=r"^ta_[0-9a-f]{16}$")
    status: Literal["covered", "partial", "missing"]


class _AspectEvidenceAuditPayload(BaseModel):
    selected_blocks: list[_AspectEvidenceAuditItem] = Field(default_factory=list)
    coverage: list[_AspectCoverageItem] = Field(default_factory=list)


async def _complete_structured_json[StructuredResponse: BaseModel](
    *,
    provider: ChatProvider,
    messages: list[ChatMessage],
    response_type: type[StructuredResponse],
    stage: str,
    invalid_code: str,
    first_max_output_tokens: int,
    retry_max_output_tokens: int,
    validator: Callable[[StructuredResponse], None] | None = None,
    max_attempts: int = STRUCTURED_OUTPUT_MAX_ATTEMPTS,
) -> tuple[StructuredResponse, dict[str, object], list[dict[str, object]]]:
    """执行一次结构化输出；schema 不合法时只做一次显式修复重试。"""

    request_messages = messages
    attempts: list[dict[str, object]] = []
    for attempt in range(1, max_attempts + 1):
        max_output_tokens = first_max_output_tokens if attempt == 1 else retry_max_output_tokens
        try:
            response = await provider.complete(
                request_messages,
                max_output_tokens=max_output_tokens,
            )
        except ChatProviderError as error:
            failed_attempt: dict[str, object] = {
                "request_id": "",
                "input_tokens": 0,
                "output_tokens": 0,
                "finish_reason": "provider_error",
                "returned_chars": 0,
                "max_output_tokens": max_output_tokens,
                "provider_error_code": error.code,
            }
            attempts.append(failed_attempt)
            if error.retryable and attempt < max_attempts:
                request_messages = messages
                continue
            raise ProfileKnowledgeMapError(
                error.code,
                error.message,
                diagnostics={"stage": stage, "attempt": attempt, "attempts": attempts},
            ) from error
        call_details: dict[str, object] = {
            "request_id": response.request_id,
            "input_tokens": response.input_tokens,
            "output_tokens": response.output_tokens,
            "finish_reason": response.finish_reason,
            "returned_chars": len(response.content),
            "max_output_tokens": max_output_tokens,
        }
        try:
            payload = response_type.model_validate_json(response.content)
            if validator is not None:
                validator(payload)
        except Exception as error:
            validation_error = f"{type(error).__name__}: {str(error)[:1_000]}".replace("\n", " ")
            failed_attempt = {**call_details, "validation_error": validation_error}
            next_attempts = [*attempts, failed_attempt]
            if attempt == max_attempts:
                raise ProfileKnowledgeMapError(
                    invalid_code,
                    f"{stage} returned invalid structured output after {attempt} attempts",
                    diagnostics={"stage": stage, "attempts": next_attempts},
                ) from error
            attempts = next_attempts
            request_messages = [
                *messages,
                ChatMessage(role="assistant", content=response.content),
                ChatMessage(
                    role="user",
                    content=(
                        f"上一次输出未通过 schema 校验：{validation_error}。"
                        "不要解释，也不要改变原任务；重新只输出完整 JSON，"
                        "逐字遵守原输出 schema。"
                    ),
                ),
            ]
            continue

        attempts.append(call_details)
        usage = {
            "stage": stage,
            "attempts": attempt,
            "request_ids": [str(item["request_id"]) for item in attempts],
            "input_tokens": sum(cast(int, item["input_tokens"]) for item in attempts),
            "output_tokens": sum(cast(int, item["output_tokens"]) for item in attempts),
            "finish_reasons": [item["finish_reason"] for item in attempts],
            "returned_chars": sum(cast(int, item["returned_chars"]) for item in attempts),
        }
        diagnostics = [
            {
                "code": f"{stage}_retry",
                "message": "structured output failed validation and was retried once",
                **attempt_detail,
            }
            for attempt_detail in attempts[:-1]
        ]
        return payload, usage, diagnostics
    raise AssertionError("structured output attempt loop must return or raise")


def _validate_group_coverage(
    payload: _GroupingPayload,
    *,
    allowed_ids: set[str],
    stage: str,
) -> None:
    """要求分组只引用输入 ID，且不遗漏任何输入项。"""

    assigned: set[str] = set()
    for group in payload.groups:
        unknown = [item_id for item_id in group.item_ids if item_id not in allowed_ids]
        if unknown:
            raise ValueError(f"{stage} grouping referenced unknown IDs: {', '.join(unknown)}")
        assigned.update(group.item_ids)
    missing = sorted(allowed_ids - assigned)
    if missing:
        raise ValueError(f"{stage} grouping omitted IDs: {', '.join(missing)}")
    for pair in payload.uncertain_pairs:
        if pair.left_id not in allowed_ids or pair.right_id not in allowed_ids:
            raise ValueError(f"{stage} grouping returned unknown uncertain pair")
        if pair.left_id == pair.right_id:
            raise ValueError(f"{stage} grouping returned self-referential uncertain pair")


def _validate_group_merge(
    payload: _GroupMergePayload,
    *,
    allowed_ids: set[str],
) -> None:
    """要求 merge batch 对输入 Group 做一次完整且不重复的消费。"""

    consumed: list[str] = []
    for action in payload.actions:
        unknown = [group_id for group_id in action.group_ids if group_id not in allowed_ids]
        if unknown:
            raise ValueError(f"group merge referenced unknown IDs: {', '.join(unknown)}")
        if len(action.group_ids) != len(set(action.group_ids)):
            raise ValueError("group merge action contains duplicate IDs")
        if action.decision == "keep" and len(action.group_ids) != 1:
            raise ValueError("group merge keep action must contain exactly one Group")
        if action.decision == "merge" and len(action.group_ids) < 2:
            raise ValueError("group merge action must contain at least two Groups")
        if action.decision == "merge" and (not action.label or not action.summary):
            raise ValueError("group merge action requires canonical label and summary")
        consumed.extend(action.group_ids)
    if len(consumed) != len(set(consumed)):
        raise ValueError("a Group may be consumed by only one merge action per batch")
    missing = sorted(allowed_ids - set(consumed))
    extra = sorted(set(consumed) - allowed_ids)
    if missing or extra:
        raise ValueError(
            f"group merge must consume every input exactly once; missing={missing}, extra={extra}"
        )
    for pair in payload.uncertain_pairs:
        if pair.left_id not in allowed_ids or pair.right_id not in allowed_ids:
            raise ValueError("group merge returned unknown uncertain pair")
        if pair.left_id == pair.right_id:
            raise ValueError("group merge returned self-referential uncertain pair")


def _validate_group_selection(
    payload: _GroupSelectionPayload,
    *,
    allowed_ids: set[str],
    allowed_topic_ids: dict[str, set[str]],
) -> None:
    """要求 group 路由只返回候选内且不重复的 group ID。"""

    group_ids = [item.group_id for item in payload.groups]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("group selector returned duplicate group IDs")
    unknown = [group_id for group_id in group_ids if group_id not in allowed_ids]
    if unknown:
        raise ValueError(f"group selector returned unknown groups: {', '.join(unknown)}")
    for item in payload.groups:
        invalid_topics = [
            topic_id
            for topic_id in item.supported_topic_ids
            if topic_id not in allowed_topic_ids[item.group_id]
        ]
        if invalid_topics:
            raise ValueError(
                "group selector returned Topics outside its Group: " + ", ".join(invalid_topics)
            )
        if not item.supported_topic_ids:
            raise ValueError("selected Group must identify at least one supporting Topic")
    if len(group_ids) > GROUP_SELECTION_SAFETY_LIMIT:
        raise ValueError(
            f"group selector returned {len(group_ids)} groups; "
            f"safety limit is {GROUP_SELECTION_SAFETY_LIMIT}"
        )


def _validate_group_coverage_audit(
    payload: _GroupCoverageAuditPayload,
    *,
    selected_group_ids: set[str],
    allowed_topic_ids: dict[str, set[str]],
) -> None:
    """要求覆盖补选只引用未选候选 Group，并给出组内支撑 Topic。"""

    group_ids = [item.group_id for item in payload.supplemental_groups]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("group coverage audit returned duplicate group IDs")
    unknown = [group_id for group_id in group_ids if group_id not in allowed_topic_ids]
    if unknown:
        raise ValueError(
            "group coverage audit returned groups outside unselected candidates: "
            + ", ".join(unknown)
        )
    already_selected = sorted(set(group_ids).intersection(selected_group_ids))
    if already_selected:
        raise ValueError(
            "group coverage audit returned already selected groups: " + ", ".join(already_selected)
        )
    for item in payload.supplemental_groups:
        invalid_topics = [
            topic_id
            for topic_id in item.supported_topic_ids
            if topic_id not in allowed_topic_ids[item.group_id]
        ]
        if invalid_topics:
            raise ValueError(
                "group coverage audit returned Topics outside its Group: "
                + ", ".join(invalid_topics)
            )


def _validate_topic_identity_judgements(
    payload: _TopicIdentityJudgementsPayload,
    *,
    expected_pairs: set[tuple[str, str]],
) -> None:
    """要求身份判官对每个输入节点对恰好返回一次判断。"""

    returned_pairs = [
        (judgement.left_node_ref, judgement.right_node_ref) for judgement in payload.judgements
    ]
    if len(returned_pairs) != len(set(returned_pairs)):
        raise ValueError("topic identity returned duplicate node pairs")
    unknown = sorted(set(returned_pairs) - expected_pairs)
    if unknown:
        raise ValueError(f"topic identity returned unknown node pairs: {unknown}")
    missing = sorted(expected_pairs - set(returned_pairs))
    if missing:
        raise ValueError(f"topic identity omitted node pairs: {missing}")


def _validate_aspect_catalog(
    payload: _AspectCatalogPayload,
    *,
    allowed_topics: dict[str, set[str]],
    allowed_kinds: set[str],
) -> None:
    """要求每个输入 Topic 返回唯一且受控的 Aspect 清单。"""

    topic_ids = [topic.topic_id for topic in payload.topics]
    if len(topic_ids) != len(set(topic_ids)):
        raise ValueError("aspect catalog returned duplicate topics")
    if set(topic_ids) != set(allowed_topics):
        raise ValueError("aspect catalog did not cover exactly all input topics")
    for topic in payload.topics:
        seen: set[tuple[str, str]] = set()
        for aspect in topic.aspects:
            if aspect.kind not in allowed_kinds:
                raise ValueError(f"aspect catalog returned unsupported kind: {aspect.kind}")
            unknown = [
                candidate_id
                for candidate_id in aspect.source_candidate_ids
                if candidate_id not in allowed_topics[topic.topic_id]
            ]
            if unknown:
                raise ValueError(f"aspect catalog returned unknown candidates: {unknown}")
            key = (aspect.kind, normalize_name(aspect.label))
            if key in seen:
                raise ValueError("aspect catalog returned duplicate kind/label")
            seen.add(key)


def _validate_aspect_extension(
    payload: _AspectExtensionPayload,
    *,
    allowed_topics: dict[str, set[str]],
    allowed_kinds: set[str],
) -> None:
    """要求扩展结果不引用未知 Topic，并保持 Aspect 与来源合法。"""

    topic_ids = [topic.topic_id for topic in payload.topics]
    if len(topic_ids) != len(set(topic_ids)):
        raise ValueError("aspect extension returned duplicate topics")
    unknown_topics = sorted(set(topic_ids) - set(allowed_topics))
    if unknown_topics:
        raise ValueError(
            "aspect extension returned topics outside the review set: " + ", ".join(unknown_topics)
        )
    for topic in payload.topics:
        seen: set[tuple[str, str]] = set()
        for aspect in topic.aspects:
            if aspect.kind not in allowed_kinds:
                raise ValueError(f"aspect extension returned unsupported kind: {aspect.kind}")
            unknown = [
                candidate_id
                for candidate_id in aspect.source_candidate_ids
                if candidate_id not in allowed_topics[topic.topic_id]
            ]
            if unknown:
                raise ValueError(f"aspect extension returned unknown candidates: {unknown}")
            key = (aspect.kind, normalize_name(aspect.label))
            if key in seen:
                raise ValueError("aspect extension returned duplicate kind/label")
            seen.add(key)


def _validate_aspect_mapping(
    payload: _AspectMappingPayload,
    *,
    allowed_block_ids: set[tuple[str, str]],
    allowed_aspect_ids: dict[tuple[str, str], set[str]],
) -> set[tuple[str, str]]:
    """校验已返回引用并返回仍需定向映射的 Block。"""

    support_keys: list[tuple[str, str, str]] = []
    for support in payload.supports:
        block_key = (support.topic_id, support.block_id)
        if block_key not in allowed_block_ids:
            raise ValueError("aspect mapping returned an unknown block")
        if support.aspect_id not in allowed_aspect_ids.get(block_key, set()):
            raise ValueError("aspect mapping returned an aspect outside the block topic")
        support_keys.append((support.topic_id, support.block_id, support.aspect_id))
    if len(support_keys) != len(set(support_keys)):
        raise ValueError("aspect mapping returned duplicate supports")

    excluded: set[tuple[str, str]] = set()
    for exclusion in payload.exclusions:
        block_key = (exclusion.topic_id, exclusion.block_id)
        if block_key not in allowed_block_ids:
            raise ValueError("aspect mapping excluded an unknown block")
        if block_key in excluded:
            raise ValueError("aspect mapping returned duplicate exclusions")
        excluded.add(block_key)

    covered_blocks = {(topic_id, block_id) for topic_id, block_id, _ in support_keys}
    missing_blocks = allowed_block_ids - covered_blocks - excluded
    return missing_blocks


def _drop_redundant_aspect_mapping_exclusions(
    payload: _AspectMappingPayload,
) -> list[tuple[str, str]]:
    """移除已获得具体 Aspect support 的 block-level exclusion。"""

    supported = {(support.topic_id, support.block_id) for support in payload.supports}
    retained: list[_BlockAspectExclusionDraft] = []
    resolved: list[tuple[str, str]] = []
    for exclusion in payload.exclusions:
        key = (exclusion.topic_id, exclusion.block_id)
        if key in supported:
            resolved.append(key)
        else:
            retained.append(exclusion)
    payload.exclusions = retained
    return resolved


def _drop_out_of_scope_aspect_mapping_references(
    payload: _AspectMappingPayload,
    *,
    allowed_block_ids: set[tuple[str, str]],
    allowed_aspect_ids: dict[tuple[str, str], set[str]],
) -> list[str]:
    """删除不属于当前批次或 Topic 的模型引用，并返回诊断标识。"""

    dropped: list[str] = []
    retained_supports: list[_BlockAspectDraft] = []
    for support in payload.supports:
        block_key = (support.topic_id, support.block_id)
        if block_key not in allowed_block_ids or support.aspect_id not in allowed_aspect_ids.get(
            block_key, set()
        ):
            dropped.append(f"support:{support.topic_id}/{support.block_id}/{support.aspect_id}")
        else:
            retained_supports.append(support)
    payload.supports = retained_supports

    retained_exclusions: list[_BlockAspectExclusionDraft] = []
    for exclusion in payload.exclusions:
        block_key = (exclusion.topic_id, exclusion.block_id)
        if block_key not in allowed_block_ids:
            dropped.append(f"exclusion:{exclusion.topic_id}/{exclusion.block_id}")
        else:
            retained_exclusions.append(exclusion)
    payload.exclusions = retained_exclusions
    return dropped


def _topic_identity_prompt(profile_context: str) -> str:
    """编译 Topic 与 Topic 或新增候选之间的成对身份判断 Prompt。"""

    return f"""<role>
你是知识库 Topic 身份判定器。你不回答问题，也不新增事实。
</role>

<task>
逐对判断两个节点是否应在当前 Profile 粒度下复用同一个 Topic 身份。只能使用输入中的标题、
别名和 summaries；不读取外部知识，也不把上下游、成员或相关概念视为同一对象。
</task>

<profile_context>
{profile_context}
</profile_context>

<rules>
1. 必须逐字复制输入的 left_node_ref 和 right_node_ref，不得发明或改写 ID。
2. 每个输入节点对必须且只能得到一条 judgement。
3. relation 只能是 same、different 或 uncertain。
4. 只有两个节点明确指向同一个可复用知识入口时才返回 same。
5. 名称相近、领域相关、相邻演化、成员、组成项、类型实例、子类、分类项、父机制、
   具体实现和上下游关系都不足以返回 same。
6. 粒度不同，或一个对象是另一个对象的属性、方面时返回 different。
7. summaries 不足以确认对象、作用范围或粒度时返回 uncertain，并在 reason 中说明缺少什么。
8. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{{"judgements":[{{"left_node_ref":"...","right_node_ref":"...",
"relation":"same|different|uncertain","reason":"一句简短理由"}}]}}
</output>"""


def _topic_identity_judge_contract_hash(
    *,
    provider: ChatProvider,
    profile_context: str,
    profile_id: str = "direct-test",
    profile_hash: str = "0" * 64,
) -> str:
    """计算一次 compile attempt 内冻结的 Topic 判官契约哈希。"""

    return stable_hash(
        {
            "input_schema_version": "topic-identity-input-v1",
            "output_schema_version": "topic-identity-output-v1",
            "prompt_version": TOPIC_IDENTITY_PROMPT_VERSION,
            "prompt_template": _topic_identity_prompt(profile_context),
            "relation_enum_version": "same-different-uncertain-v1",
            "provider": provider.profile.provider,
            "model_id": provider.profile.model_id,
            "temperature": provider.profile.temperature,
            "thinking_enabled": provider.profile.thinking_enabled,
            "profile_id": profile_id,
            "profile_hash": profile_hash,
            "profile_context": profile_context,
        }
    )


def _map_embedding_contract(
    *,
    provider: EmbeddingProvider,
    input_version: str,
) -> MapEmbeddingContract:
    """把 embedding provider profile 冻结为地图清单中的输入契约。"""

    return MapEmbeddingContract(
        provider=provider.profile.provider,
        model_id=provider.profile.model_id,
        model_version=provider.profile.model_version,
        dimension=provider.profile.dimension,
        output_type=provider.profile.output_type,
        input_version=input_version,
    )


def _build_map_compile_contract(
    *,
    first_run: ProfilePreviewRunRow,
    provider: ChatProvider,
    embedding_provider: EmbeddingProvider,
    profile_context: str,
    profile_aspect_schema_hash: str,
) -> ProfileMapCompileContract:
    """构造并哈希一次地图编译开始时冻结的完整契约。"""

    judge_contract_hash = _topic_identity_judge_contract_hash(
        provider=provider,
        profile_context=profile_context,
        profile_id=first_run.profile_id,
        profile_hash=first_run.profile_hash,
    )
    payload: dict[str, object] = {
        "schema_version": "profile-map-compile-contract-v1",
        "profile_id": first_run.profile_id,
        "profile_hash": first_run.profile_hash,
        "profile_aspect_schema_hash": profile_aspect_schema_hash,
        "candidate_pipeline_version": first_run.candidate_pipeline_version,
        "name_normalization_version": first_run.name_normalization_version,
        "compiler_version": first_run.compiler_version,
        "candidate_prompt_version": first_run.prompt_version,
        "candidate_prompt_hash": first_run.prompt_hash,
        "topic_identity_policy_version": TOPIC_IDENTITY_POLICY_VERSION,
        "grouping_prompt_version": GROUPING_PROMPT_VERSION,
        "grouping_policy_version": GROUPING_POLICY_VERSION,
        "aspect_prompt_version": ASPECT_PROMPT_VERSION,
        "aspect_policy_version": ASPECT_POLICY_VERSION,
        "group_embedding": _map_embedding_contract(
            provider=embedding_provider,
            input_version=GROUP_EMBEDDING_INPUT_VERSION,
        ).model_dump(mode="json"),
        "topic_embedding": _map_embedding_contract(
            provider=embedding_provider,
            input_version=TOPIC_IDENTITY_EMBEDDING_INPUT_VERSION,
        ).model_dump(mode="json"),
        "aspect_embedding": _map_embedding_contract(
            provider=embedding_provider,
            input_version=ASPECT_EMBEDDING_INPUT_VERSION,
        ).model_dump(mode="json"),
        "topic_identity_judge": {
            "contract_id": f"tjc_{judge_contract_hash[:16]}",
            "provider": provider.profile.provider,
            "model_id": provider.profile.model_id,
            "base_url": provider.profile.base_url,
            "temperature": provider.profile.temperature,
            "thinking_enabled": provider.profile.thinking_enabled,
            "prompt_version": TOPIC_IDENTITY_PROMPT_VERSION,
            "input_schema_version": "topic-identity-input-v1",
            "output_schema_version": "topic-identity-output-v1",
            "relation_enum_version": "same-different-uncertain-v1",
            "contract_hash": judge_contract_hash,
        },
        "topic_identity_parameters": TopicIdentityCompileParameters(
            similarity_threshold=TOPIC_IDENTITY_SIMILARITY_THRESHOLD,
            max_target_pairs_per_work_item=TOPIC_IDENTITY_MAX_TARGET_PAIRS,
            max_judge_total_tokens_per_work_item=TOPIC_IDENTITY_MAX_TOTAL_TOKENS,
            max_pairs_per_request=TOPIC_IDENTITY_MAX_PAIRS_PER_REQUEST,
            max_input_tokens_per_request=TOPIC_IDENTITY_MAX_INPUT_TOKENS_PER_REQUEST,
            max_output_tokens_per_request=TOPIC_IDENTITY_MAX_OUTPUT_TOKENS_PER_REQUEST,
            output_tokens_per_pair_reserve=TOPIC_IDENTITY_OUTPUT_TOKENS_PER_PAIR_RESERVE,
        ).model_dump(mode="json"),
    }
    contract_hash = stable_hash(payload)
    return ProfileMapCompileContract(
        contract_id=f"mcc_{contract_hash[:16]}",
        contract_hash=contract_hash,
        **cast(dict[str, object], payload),
    )


def _compile_input_key(
    *,
    base_map_id: str | None,
    preview_run_ids: list[str],
    compile_contract_id: str,
) -> str:
    """计算一次地图编译输入身份；显式重跑仍会生成新的 attempt ID。"""

    return stable_hash(
        {
            "schema_version": "profile-map-compile-input-v1",
            "base_map_id": base_map_id,
            "preview_run_ids": sorted(set(preview_run_ids)),
            "compile_contract_id": compile_contract_id,
        }
    )


def _candidate_ref(row: PreviewCandidateRow) -> tuple[str, str]:
    """返回候选在 Extraction Run 内的不可变身份。"""

    return (row.run_id, row.candidate_id)


def _candidate_node_ref(candidate_ref: tuple[str, str]) -> str:
    """把 CandidateRef 编码成可持久化审计的 NodeRef。"""

    return f"candidate:{candidate_ref[0]}:{candidate_ref[1]}"


def _topic_node_ref(topic: ProfileMapTopic) -> str:
    """把当前 Topic revision 编码成可持久化审计的 NodeRef。"""

    return f"topic:{topic.topic_id}:{topic.revision_id}"


def _candidate_node(row: PreviewCandidateRow) -> _CandidateNode:
    """构造只存在于当前 compile attempt 的单个候选节点。"""

    aliases = ordered_unique_normalized_text(json_loads_string_list(row.aliases_json))
    summaries = ordered_unique_normalized_text(json_loads_string_list(row.summaries_json))
    if not summaries:
        raise ProfileKnowledgeMapError(
            "knowledge_map_candidate_summary_missing",
            "candidate must contain at least one non-empty summary",
            diagnostics={"candidate_id": row.candidate_id, "extraction_run_id": row.run_id},
        )
    candidate_ref = _candidate_ref(row)
    return _CandidateNode(
        node_ref=_candidate_node_ref(candidate_ref),
        extraction_run_id=candidate_ref[0],
        candidate_id=candidate_ref[1],
        title=row.name,
        aliases=aliases,
        summaries=summaries,
        source_block_ids=json_loads_string_list(row.source_block_ids_json),
        row=row,
    )


def _materialize_topic(
    *,
    lineage_id: str,
    rows: list[PreviewCandidateRow],
) -> ProfileMapTopic:
    """从当前候选成员关系确定性重建 Topic 内容与 revision。"""

    ordered_rows = sorted(
        {(row.run_id, row.candidate_id): row for row in rows}.values(),
        key=lambda row: (
            row.doc_id,
            row.run_id,
            row.candidate_id,
        ),
    )
    if not ordered_rows:
        raise ValueError("active Topic must contain at least one candidate")
    canonical_title = ordered_rows[0].name
    aliases = ordered_unique_normalized_text(
        alias
        for row in ordered_rows
        for alias in [row.name, *json_loads_string_list(row.aliases_json)]
        if normalize_name(alias) != normalize_name(canonical_title)
    )
    summary_text_by_key: dict[str, str] = {}
    summary_refs_by_key: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in ordered_rows:
        for summary in json_loads_string_list(row.summaries_json):
            text = " ".join(summary.split())
            key = normalize_name(text)
            if not text or not key:
                continue
            summary_text_by_key.setdefault(key, text)
            summary_refs_by_key[key].add((row.run_id, row.candidate_id))
    summaries = list(summary_text_by_key.values())
    summary_sources = [
        TopicSummarySource(
            text=summary_text_by_key[key],
            source_candidate_refs=[
                TopicSummaryCandidateRef(
                    extraction_run_id=extraction_run_id,
                    candidate_id=candidate_id,
                )
                for extraction_run_id, candidate_id in sorted(summary_refs_by_key[key])
            ],
        )
        for key in summary_text_by_key
    ]
    if not summaries:
        raise ProfileKnowledgeMapError(
            "knowledge_map_topic_summary_missing",
            "Topic must contain at least one non-empty candidate summary",
        )
    sources = [
        ProfileMapTopicSource(
            extraction_run_id=row.run_id,
            candidate_id=row.candidate_id,
            doc_id=row.doc_id,
            doc_version=row.doc_version,
            source_block_ids=json_loads_string_list(row.source_block_ids_json),
            channel_ids=json_loads_string_list(row.channel_ids_json),
        )
        for row in ordered_rows
    ]
    candidate_refs = [(source.extraction_run_id, source.candidate_id) for source in sources]
    revision_id = stable_hash(
        {
            "lineage": lineage_id,
            "candidate_refs": candidate_refs,
            "title": canonical_title,
            "aliases": aliases,
            "summaries": summaries,
        }
    )
    return ProfileMapTopic(
        topic_id=lineage_id,
        revision_id=revision_id,
        title=canonical_title,
        aliases=aliases,
        summaries=summaries,
        summary_sources=summary_sources,
        sources=sources,
        source_doc_ids=sorted({source.doc_id for source in sources}),
        source_block_ids=ordered_unique(
            block_id for source in sources for block_id in source.source_block_ids
        ),
        channel_ids=ordered_unique(
            channel_id for source in sources for channel_id in source.channel_ids
        ),
    )


def _topic_candidate_memberships(
    topics: list[ProfileMapTopic],
) -> list[TopicCandidateMembership]:
    """从已物化 Topic sources 派生权威 CandidateRef membership 分区。"""

    return sorted(
        [
            TopicCandidateMembership(
                topic_lineage_id=topic.topic_id,
                extraction_run_id=source.extraction_run_id,
                candidate_id=source.candidate_id,
            )
            for topic in topics
            for source in topic.sources
        ],
        key=lambda membership: (
            membership.topic_lineage_id,
            membership.extraction_run_id,
            membership.candidate_id,
        ),
    )


def _new_topic_lineage_id(initial_candidate_ref: tuple[str, str]) -> str:
    """为 create_topic 生成与初始 CandidateRef 绑定的稳定谱系 ID。"""

    digest = stable_hash(
        {
            "kind": "topic-lineage-v1",
            "initial_candidate_ref": initial_candidate_ref,
        }
    )
    return f"pt_{digest[:16]}"


def _merged_topic_lineage_id(topics: list[ProfileMapTopic]) -> str:
    """为一次 Topic merge 生成新的稳定谱系 ID。"""

    digest = stable_hash(
        {
            "kind": "topic-merge-lineage-v1",
            "parents": sorted(f"topic:{topic.topic_id}:{topic.revision_id}" for topic in topics),
        }
    )
    return f"pt_{digest[:16]}"


def _summary_identity_text(summaries: list[str]) -> str:
    """构造只包含同级摘要集合的 Topic 身份向量输入。"""

    normalized = ordered_unique_normalized_text(summaries)
    if not normalized:
        raise ValueError("identity embedding requires at least one summary")
    return "\n".join(normalized)


def _identity_embedding_contract_hash(
    compile_contract: ProfileMapCompileContract,
) -> str:
    """返回 Topic／Candidate 身份向量使用的 embedding 契约哈希。"""

    return stable_hash(compile_contract.topic_embedding.model_dump(mode="json"))


def _identity_summary_input_hash(
    *,
    text: str,
    compile_contract: ProfileMapCompileContract,
) -> str:
    """绑定规范化摘要输入与固定输入模板版本。"""

    return stable_hash(
        {
            "input_version": compile_contract.topic_embedding.input_version,
            "text": text,
        }
    )


def _identity_vector_hash(vector: list[float]) -> str:
    """返回完整向量内容的稳定哈希。"""

    return stable_hash({"vector": vector})


def _persisted_identity_vector(
    *,
    node_ref: str,
    text: str,
    vector: list[float],
    compile_contract: ProfileMapCompileContract,
) -> PersistedIdentityVector:
    """构造一条可发布、可复用、可验证的身份向量缓存。"""

    normalized = _normalize_vector(vector)
    expected_dimension = compile_contract.topic_embedding.dimension
    normalized_array = np.asarray(normalized, dtype=np.float32)
    if normalized_array.shape != (expected_dimension,) or not np.isfinite(normalized_array).all():
        raise ProfileKnowledgeMapError(
            "knowledge_map_identity_vector_invalid",
            "identity embedding must be finite and match the contract dimension",
            diagnostics={
                "node_ref": node_ref,
                "expected_dimension": expected_dimension,
            },
        )
    return PersistedIdentityVector(
        node_ref=node_ref,
        embedding_contract_hash=_identity_embedding_contract_hash(compile_contract),
        vector_dimension=expected_dimension,
        summary_input_hash=_identity_summary_input_hash(
            text=text,
            compile_contract=compile_contract,
        ),
        vector_hash=_identity_vector_hash(normalized),
        vector=normalized,
    )


def _validated_cached_identity_vector(
    cached: PersistedIdentityVector | None,
    *,
    node_ref: str,
    text: str,
    compile_contract: ProfileMapCompileContract,
) -> list[float] | None:
    """校验持久身份向量；任一身份条件不一致时返回 None 并触发重算。"""

    if cached is None:
        return None
    normalized = _normalize_vector(cached.vector)
    array = np.asarray(normalized, dtype=np.float32)
    expected_dimension = compile_contract.topic_embedding.dimension
    if (
        cached.node_ref != node_ref
        or cached.embedding_contract_hash != _identity_embedding_contract_hash(compile_contract)
        or cached.vector_dimension != expected_dimension
        or cached.summary_input_hash
        != _identity_summary_input_hash(
            text=text,
            compile_contract=compile_contract,
        )
        or cached.vector_hash != _identity_vector_hash(normalized)
        or array.shape != (expected_dimension,)
        or not np.isfinite(array).all()
    ):
        return None
    return normalized


def _topic_card(topic: ProfileMapTopic) -> dict[str, object]:
    """生成身份判官使用的当前 Topic revision 卡片。"""

    return {
        "node_type": "topic",
        "title": topic.title,
        "aliases": topic.aliases,
        "summaries": topic.summaries,
    }


def _candidate_card(candidate: _CandidateNode) -> dict[str, object]:
    """生成身份判官使用的候选节点卡片。"""

    return {
        "node_type": "candidate",
        "title": candidate.title,
        "aliases": candidate.aliases,
        "summaries": candidate.summaries,
    }


def _topic_pair_key(
    left_node_ref: str,
    right_node_ref: str,
    judge_contract_hash: str,
) -> str:
    """为 Topic↔Topic 判断生成与方向无关的 attempt 内缓存键。"""

    return stable_hash(
        {
            "kind": "topic-topic-identity-pair-v1",
            "left": min(left_node_ref, right_node_ref),
            "right": max(left_node_ref, right_node_ref),
            "judge_contract_hash": judge_contract_hash,
        }
    )


def _grouping_prompt(mode: Literal["topic", "global"], profile_context: str) -> str:
    scope = (
        "把跨文档合并后的 topic cards 归成可检索的主题分组。"
        if mode == "topic"
        else "把多篇文档的 local group cards 归并为全局主题分组。"
    )
    item_name = "topic_id" if mode == "topic" else "group_id"
    return f"""<role>
你是 Wiki 知识地图主题分组器。你不回答问题，也不生成证据。
</role>

<task>
{scope}
分组用于后续主题检索，不是严格本体分类。一个 item 可以同时属于多个分组，
但不得为了增加分组制造同义分组。Group 表示一个有明确检索职责的区域，不是标签集合。
</task>

<profile_context>
{profile_context}
</profile_context>

<rules>
1. item_ids 只能逐字引用输入中的 {item_name}，不得发明、合并或改写 ID。
2. 输入中的每个 item 必须至少进入一个分组；不得静默遗漏。
3. label 必须说明该组覆盖的对象、机制或边界范围，不能只写“基础知识”“相关内容”等空泛名称。
4. aliases 只能补充同一分组的常见名称，不得塞入成员、子类或不同粒度对象。
5. summary 只描述该分组的检索范围，不写输入之外的事实。
6. retrieval_questions 写 1–5 个该分组应帮助核对的问题。
7. 同一对象的不同方面可以进入同一分组；父机制、具体实现和不同粒度对象保持可辨认。
8. 默认让每个 item 进入一个主 Group；只有该 item 在另一个 Group 中承担不同检索职责时，
   才允许额外归属，并在 summary 中明确这一职责。
9. 如果两个候选 Group 回答同一个核心 retrieval question，必须合并，不能仅因措辞或举例不同
   而拆成两个 Group。
10. label 和 summary 必须覆盖该 Group 的全部主要对象与边界，不能用其中一个子面的名称
    代表整个 Group。
11. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{{"groups":[{{"label":"...","aliases":[],"summary":"...","retrieval_questions":["..."],"item_ids":["..."]}}]}}
</output>"""


GROUP_MERGE_PROMPT = """<role>
你是 Wiki Group 的全局合并判定器。你不回答问题，也不创建新的检索区域。
</role>

<task>
只对输入中的现有 Groups 做 merge 或 keep 决策。merge 表示这些 Groups 实际描述同一检索区域，
应形成一个更少的 Group；keep 表示当前 Group 必须原样保留。所有输入必须恰好被消费一次。
</task>

<rules>
1. group_ids 只能逐字引用输入中的 group_id，不能发明 ID。
2. 每个输入 group_id 必须且只能出现在一个 action 中。
3. decision 只能是 merge 或 keep；keep 的 group_ids 必须只有一个。
4. merge 必须给出能覆盖全部 child Topics 的 canonical label 和 summary，并给出简短 reason。
5. 只有检索范围基本相同才能 merge；部分重叠、不同对象、不同生命周期或不同视角保持 keep。
6. 不能把输入 Groups 拆开，不能让一个 Group 同时进入多个输出。
7. 无法确认时 keep，并在 uncertain_pairs 中记录；不得猜测 merge。
8. 不要为了减少数量而合并语义不同的 Groups。
9. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{"actions":[{"group_ids":["wg_..."],"decision":"keep|merge",
"label":"merge 时必填","aliases":[],"summary":"merge 时必填",
"retrieval_questions":[],"reason":"..."}],
"uncertain_pairs":[{"left_id":"wg_...","right_id":"wg_..."}]}
</output>"""


GROUP_SELECTION_PROMPT = """<role>
你是 Wiki 主题 group 路由器。你不回答问题，也不生成或改写证据。
</role>

<task>
从有限候选 groups 中选出与 topic 明确相关、需要继续展开证据的 group。
选择所有与 topic 明确相关、需要继续展开证据的 groups，不要求凑满固定数量。
只保留回答主题所必需或能构成明显补足的分组；泛背景、普通示例和只有弱关联的分组不要选择。
开放主题的间接但明确边界仍应保留。输出数量受系统安全上限约束，但不得把安全上限当作选择目标。
profile_context 与 representative_questions 定义当前知识库的领域范围，
只用于理解 topic，不得替代 candidate_groups。
</task>

<rules>
1. group_id 必须逐字来自输入，不得重复或发明。
2. group 的 summary 和 retrieval_questions 用于判断主题关系；不能据此直接陈述事实。
3. 优先覆盖不同对象、机制、作用域和边界；不要只选词面最相似的 group，也不要收集泛背景。
4. relevance 只能是 direct 或 partial。
5. 每个 selected Group 必须填写 supported_topic_ids，精确列出支撑该判断的候选 child Topic；
   partial 还要说明它新增了哪一个尚未覆盖的 Topic。
6. reason 必须说明该 Group 补足了什么检索问题，不得只写“相关”。
7. GroupScore、candidate_rank 和 child Topic query score 是结构化参考，不得替代语义判断。
8. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{"groups":[{"group_id":"...","relevance":"direct|partial",
"supported_topic_ids":["pt_..."],"reason":"..."}]}
</output>"""


GROUP_COVERAGE_AUDIT_PROMPT = """<role>
你是 Wiki Group 路由的覆盖缺口审查器。你不回答问题、不改写 topic，也不创建 Group。
</role>

<task>
基础 Group Router 已经给出 selected_groups。现在只审查未选候选 Groups，
找出会让回答系统性缺少一个独立检索维度的 Groups。
没有缺口时返回空数组，不要求凑数量。
</task>

<rules>
1. 只返回未选 candidate_groups 中的 group_id，不能重复、发明或返回已选 Group。
2. 补选理由必须是一个现有 selected_groups 未覆盖的独立对象、机制、作用域、风险或边界，
   不能只是同义改写、另一个普通示例或泛背景。
3. 不能因为 group_score 高、candidate_rank 靠前或 Topic 多就补选。
4. supported_topic_ids 必须逐字来自该 Group，并实际支撑 new_dimension。
5. 如果缺少该 Group 只会少一个例子，或答案仍能完整表达现有维度，不要补选。
6. 开放主题中的独立间接边界可以补选，但必须说明它补足的具体检索问题。
7. 不要求固定补选数量，也不得把安全上限当目标。
8. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{"supplemental_groups":[{"group_id":"...","new_dimension":"...",
"supported_topic_ids":["pt_..."],"reason":"..."}]}
</output>"""


ASPECT_CATALOG_PROMPT = """<role>
你是 Wiki Topic 的证据方面编译器。你不回答问题，也不创造 Topic 事实。
</role>

<task>
针对输入中的每个 Topic，在 Profile Aspect Schema 允许的 kind 内生成一个小型、
去重、Topic-specific 的 TopicAspect 清单。
</task>

<rules>
1. 每个输入 topic_id 必须且只能返回一次。
2. source_candidate_ids 只能引用该 Topic 输入中的 candidate_id。
3. kind 必须来自 profile_aspect_schema.definitions。
4. label 必须描述该 Topic 上的具体方面，不能只写“风险”“机制”等空泛名称。
5. description 只说明检索和解释范围，不能添加输入之外的事实。
6. 同一事实、同一机制的不同措辞合并；不同对象、不同机制、不同粒度或不同具体风险不得合并。
7. 不能因为 Block 或来源文档很多就增加大量近义 Aspect；Aspect 数量应由 Topic 语义复杂度决定。
8. importance 在 0 到 1 之间，表示该 Aspect 对完整解释该 Topic 的基础重要程度。
9. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{"topics":[{"topic_id":"pt_...","aspects":[{"kind":"definition","label":"...","description":"...","importance":0.8,"source_candidate_ids":["pc_..."]}]}]}
</output>"""


ASPECT_MAPPING_PROMPT = """<role>
你是 Wiki Topic Block 的 Aspect 映射器。你不回答问题，也不创建新的 Aspect。
</role>

<task>
把输入 Topic 的每个原始 Block 映射到一个或多个已有 TopicAspect，或明确列为 exclusion。
</task>

<rules>
1. block_id、topic_id 和 aspect_id 必须逐字来自输入。
2. 一个 Block 可以支持多个 Aspects；同一 Block/Aspect 不得重复。
3. 每个 Block 必须至少出现在 supports 或 exclusions 中。
4. support_kind 只能是 direct 或 partial。
5. support_strength 在 0 到 1 之间，表示 Block 对该 Aspect 的直接和完整支持程度。
6. reason 只说明原文如何支持该 Aspect，不输出隐藏思维链。
7. 不能创建新 Aspect；没有合适 Aspect 时写入 exclusions。classification 必须区分：
   weak_background（弱背景）、duplicate（重复）、outside_topic（越出当前 Topic）、
   non_evidence（非知识证据）或 missing_aspect（现有 catalog 缺失方面）。
8. 同一个 Block 不得同时出现在 supports 和 exclusions；若已支持某个 Aspect，就不再排除该 Block。
9. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{"supports":[{"block_id":"...","topic_id":"pt_...","aspect_id":"ta_...","support_kind":"direct","support_strength":0.9,"reason":"..."}],"exclusions":[{"block_id":"...","topic_id":"pt_...","classification":"weak_background|duplicate|outside_topic|non_evidence|missing_aspect","reason":"..."}]}
</output>"""


ASPECT_EXTENSION_PROMPT = """<role>
你是 Wiki TopicAspect catalog 的受控扩展器。你不回答问题，也不创造 Topic 事实。
</role>

<task>
只审查输入中带有 missing_aspect 的未分类 Blocks。若 Block 确实提供现有 catalog 未覆盖的
独立证据方面，才在 Profile Aspect Schema 允许的 kind 中新增 TopicAspect；弱背景或重复
内容不要扩建 catalog。
</task>

<rules>
1. topic_id、block_id 和 source_candidate_ids 必须逐字来自输入。
2. 新 Aspect 的 label 必须说明当前 Topic 上的具体方面，不能只写 kind 名称。
3. 同一 kind 下的不同对象、机制、风险或粒度不得合并为同一个粗 Aspect。
4. 不得为单个措辞差异创建近义 Aspect；已有 catalog 足够时不要返回新 Aspect。
5. description 只用于检索和解释，不是事实来源。
6. importance 在 0 到 1 之间。
7. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{"topics":[{"topic_id":"pt_...","aspects":[{"kind":"...","label":"...","description":"...","importance":0.8,"source_candidate_ids":["pc_..."]}]}]}
</output>"""


ASPECT_EVIDENCE_AUDIT_PROMPT = """<role>
你是 Wiki Aspect evidence 集合审核器。你不回答问题，也不创造 Aspect 或事实。
</role>

<task>
审核已经按 Aspect 选出的代表 Blocks，删除弱相关或重复证据，标记冲突，
并报告预编译 Aspect 的覆盖状态。
</task>

<rules>
1. block_id 和 aspect_id 必须逐字来自输入，不得发明或重复。
2. selected_blocks 只能选择输入 candidate_blocks 中的 Block。
3. 同一事实的重复 Block 只保留信息更完整的一条。
4. 不同来源的冲突、版本差异或新增机制可以同时保留，并写入 conflicts_with。
5. coverage 必须覆盖输入中的全部 aspects，值为 covered、partial 或 missing。
6. 不能现场创建新 Aspect；missing 只表示已编译 Aspect 没有得到足够证据。
7. 只返回合法 JSON，不输出 Markdown。
</rules>

<output>
{"selected_blocks":[{"block_id":"...","relevance":"direct|partial","supported_aspect_ids":["ta_..."],"reason":"...","duplicate_of":null,"conflicts_with":[]}],"coverage":[{"aspect_id":"ta_...","status":"covered|partial|missing"}]}
</output>"""


def _profile_aspect_schema(
    profile_config: dict[str, object],
) -> tuple[ProfileAspectSchema, str]:
    """从冻结 Profile 派生首版受控 Aspect Schema。"""

    schema = ProfileAspectSchema(definitions=PROFILE_ASPECT_DEFINITIONS)
    schema_hash = stable_hash(
        {
            "schema": schema.model_dump(mode="json"),
            "profile": profile_config,
            "policy_version": ASPECT_POLICY_VERSION,
        }
    )
    return schema, schema_hash


def _validated_profile_config(profile_config: object) -> dict[str, object]:
    """校验并保留冻结 Profile 快照的原始表示，避免默认值展开改变身份。"""

    if not isinstance(profile_config, dict):
        raise ProfileKnowledgeMapError(
            "knowledge_map_profile_config_invalid",
            "Profile configuration snapshot must be an object",
        )
    try:
        ExtractionProfileConfig.model_validate(profile_config)
        return profile_config
    except Exception as error:
        raise ProfileKnowledgeMapError(
            "knowledge_map_profile_config_invalid",
            "Profile configuration snapshot is invalid",
        ) from error


def _profile_config_from_row(row: ExtractionProfileRow) -> dict[str, object]:
    """从冻结 Profile row 重建与 Preview snapshot 相同的规范化配置。"""

    try:
        return _validated_profile_config(
            {
                "name": row.name,
                "description": row.description,
                "domain_context": row.domain_context,
                "primary_uses": json.loads(row.primary_uses_json),
                "representative_questions": json.loads(row.representative_questions_json),
                "extraction_channels": json.loads(row.extraction_channels_json),
                "exclude_criteria": json.loads(row.exclude_criteria_json),
                "granularity": row.granularity,
                "additional_guidance": row.additional_guidance,
                "language_policy": ProfileLanguagePolicy().model_dump(mode="json"),
            }
        )
    except ProfileKnowledgeMapError:
        raise
    except Exception as error:
        raise ProfileKnowledgeMapError(
            "knowledge_map_profile_config_invalid",
            "Selected Profile configuration is invalid",
        ) from error


def _manifest_hash_payload(manifest: ProfileKnowledgeMapManifest) -> dict[str, object]:
    """按 manifest schema 版本排除不参与该版本 hash 的扩展字段。"""

    excluded = {"map_id", "manifest_hash", "created_at"}
    if manifest.schema_version == "profile-knowledge-map-v4":
        excluded.update({"base_map_id", "compilation_mode", "delta_summary"})
    return manifest.model_dump(mode="json", exclude=excluded)


def _derive_group_aspect_fields(
    group: ProfileMapGroup,
    *,
    topic_aspects_by_topic: dict[str, list[TopicAspect]],
) -> ProfileMapGroup:
    """把 child Topic 的紧凑 Aspect 信息投影到 Group 检索文本。"""

    aspects = [
        aspect
        for topic_id in group.child_topic_ids
        for aspect in topic_aspects_by_topic.get(topic_id, [])
    ]
    ordered = sorted(aspects, key=lambda aspect: (-aspect.importance, aspect.aspect_id))
    return group.model_copy(
        update={
            "aspect_kinds": ordered_unique(aspect.kind for aspect in ordered),
            "aspect_labels": ordered_unique(aspect.label for aspect in ordered)[:20],
        }
    )


def _vector_bundle_hash(path: Path) -> str:
    """返回 vector bundle 文件内容的稳定 SHA-256。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity_vectors_from_bundle(
    bundle: Any,
    *,
    prefix: str,
) -> dict[str, PersistedIdentityVector]:
    """读取并验证 bundle 中一个身份向量分区。"""

    refs_key = f"{prefix}_refs"
    if refs_key not in bundle:
        return {}
    refs = [str(value) for value in bundle[refs_key].tolist()]
    vectors = bundle[f"{prefix}_vectors"]
    embedding_hashes = [
        str(value) for value in bundle[f"{prefix}_embedding_contract_hashes"].tolist()
    ]
    input_hashes = [str(value) for value in bundle[f"{prefix}_summary_input_hashes"].tolist()]
    vector_hashes = [str(value) for value in bundle[f"{prefix}_vector_hashes"].tolist()]
    if len(refs) != len(embedding_hashes) or len(refs) != len(input_hashes):
        raise ValueError("identity vector bundle metadata lengths do not match")
    if len(refs) != len(vector_hashes) or len(refs) != len(vectors):
        raise ValueError("identity vector bundle entries do not match")
    entries: dict[str, PersistedIdentityVector] = {}
    for ref, vector, embedding_hash, input_hash, vector_hash in zip(
        refs,
        vectors,
        embedding_hashes,
        input_hashes,
        vector_hashes,
        strict=True,
    ):
        raw_vector = [float(value) for value in vector]
        array = np.asarray(raw_vector, dtype=np.float32)
        if (
            array.ndim != 1
            or not np.isfinite(array).all()
            or not math.isclose(
                float(np.linalg.norm(array)),
                1.0,
                rel_tol=1e-4,
                abs_tol=1e-4,
            )
            or _identity_vector_hash(raw_vector) != vector_hash
        ):
            raise ValueError(f"identity vector bundle contains an invalid entry: {ref}")
        entries[ref] = PersistedIdentityVector(
            node_ref=ref,
            embedding_contract_hash=embedding_hash,
            vector_dimension=len(raw_vector),
            summary_input_hash=input_hash,
            vector_hash=vector_hash,
            vector=raw_vector,
        )
    return entries


class ProfileKnowledgeMapService:
    """从 Profile Preview Run 构建和读取可检索的知识地图。"""

    def __init__(
        self,
        data_root: Path,
        *,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self._data_root = data_root.resolve()
        self._database = StorageDatabase(self._data_root)
        self._retrieval = RetrievalReadService(self._data_root)
        self._root = self._data_root / "wiki-knowledge-maps"
        self._progress_callback = progress_callback

    def _emit_progress(
        self,
        stage: str,
        message: str,
        **details: object,
    ) -> None:
        """向可选的脚本调用方发送结构化阶段进度。"""

        if self._progress_callback is not None:
            self._progress_callback(
                {
                    "stage": stage,
                    "message": message,
                    **details,
                }
            )

    async def build(
        self,
        *,
        preview_run_ids: list[str],
        provider: ChatProvider,
        embedding_provider: EmbeddingProvider,
        force_new: bool = False,
        activate: bool = False,
        confirm_ready_partial: bool = False,
    ) -> ProfileKnowledgeMapResult:
        """从一组 completed Preview Runs 构建单篇或多篇知识地图。"""

        if not preview_run_ids:
            raise ProfileKnowledgeMapError(
                "knowledge_map_preview_runs_missing",
                "at least one Preview Run is required",
            )
        unique_run_ids = list(dict.fromkeys(preview_run_ids))
        with self._database.session() as session:
            runs = [session.get(ProfilePreviewRunRow, run_id) for run_id in unique_run_ids]
            if any(run is None for run in runs):
                missing = [
                    run_id for run_id, run in zip(unique_run_ids, runs, strict=True) if run is None
                ]
                raise ProfileKnowledgeMapError(
                    "knowledge_map_preview_run_missing",
                    f"Preview Runs do not exist: {', '.join(missing)}",
                )
            concrete_runs = [run for run in runs if run is not None]
            if any(run.status != "completed" for run in concrete_runs):
                raise ProfileKnowledgeMapError(
                    "knowledge_map_source_run_incomplete",
                    "all Preview Runs must be completed",
                )
            incompatible_runs = [
                run.run_id
                for run in concrete_runs
                if run.candidate_pipeline_version != CANDIDATE_PIPELINE_VERSION
            ]
            if incompatible_runs:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_source_run_schema_incompatible",
                    "Preview Runs must use the flat-summary candidate pipeline",
                    diagnostics={"run_ids": incompatible_runs},
                )
            first = concrete_runs[0]
            if any(
                run.snapshot_id != first.snapshot_id
                or run.snapshot_manifest_hash != first.snapshot_manifest_hash
                or run.profile_hash != first.profile_hash
                or run.candidate_pipeline_version != first.candidate_pipeline_version
                or run.name_normalization_version != first.name_normalization_version
                or run.compiler_version != first.compiler_version
                or run.prompt_hash != first.prompt_hash
                for run in concrete_runs
            ):
                raise ProfileKnowledgeMapError(
                    "knowledge_map_scope_mismatch",
                    "Preview Runs must share snapshot, Profile, and candidate pipeline versions",
                )
            document_scopes = [
                json_loads_string_list(run.document_scope_json) for run in concrete_runs
            ]
            flattened_scope = [doc_id for scope in document_scopes for doc_id in scope]
            if len(flattened_scope) != len(set(flattened_scope)):
                raise ProfileKnowledgeMapError(
                    "knowledge_map_scope_mismatch",
                    "Preview Runs must not contain overlapping document scopes",
                )
            candidate_rows = list(
                session.execute(
                    select(PreviewCandidateRow)
                    .where(PreviewCandidateRow.run_id.in_(unique_run_ids))
                    .order_by(
                        PreviewCandidateRow.doc_id,
                        PreviewCandidateRow.ordinal,
                    )
                ).scalars()
            )

        if not candidate_rows:
            raise ProfileKnowledgeMapError(
                "knowledge_map_candidates_missing",
                "Preview Runs do not contain candidates",
            )
        self._emit_progress(
            "load",
            "已加载抽取运行和候选",
            preview_runs=len(unique_run_ids),
            documents=len({row.doc_id for row in candidate_rows}),
            candidates=len(candidate_rows),
        )
        profile_config_payload = _validated_profile_config(
            json.loads(first.profile_config_snapshot_json)
        )
        profile_context = str(profile_config_payload.get("domain_context", ""))
        raw_representative_questions = profile_config_payload.get(
            "representative_questions",
            [],
        )
        if not isinstance(raw_representative_questions, list):
            raise ProfileKnowledgeMapError(
                "knowledge_map_profile_config_invalid",
                "Profile representative questions must be a list",
            )
        representative_questions = [
            str(question)
            for question in raw_representative_questions
            if isinstance(question, str) and question
        ]
        aspect_schema, aspect_schema_hash = _profile_aspect_schema(profile_config_payload)
        compile_contract = _build_map_compile_contract(
            first_run=first,
            provider=provider,
            embedding_provider=embedding_provider,
            profile_context=profile_context,
            profile_aspect_schema_hash=aspect_schema_hash,
        )
        compile_input_key = _compile_input_key(
            base_map_id=None,
            preview_run_ids=unique_run_ids,
            compile_contract_id=compile_contract.contract_id,
        )
        compile_attempt_id = new_identifier("mca")
        reused_candidate_identity_vectors = self._latest_candidate_identity_vectors(
            snapshot_id=first.snapshot_id,
            compile_contract=compile_contract,
        )
        candidate_by_id = {row.candidate_id: row for row in candidate_rows}
        if len(candidate_rows) > MAP_TOPIC_GUARD:
            raise ProfileKnowledgeMapError(
                "knowledge_map_topic_limit_exceeded",
                "knowledge map topics exceed the high-water guard",
                diagnostics={"candidate_count": len(candidate_rows)},
            )
        self._emit_progress(
            "topic_ledger",
            "开始解析 Topic Ledger",
            candidates=len(candidate_rows),
        )
        resolved_topics = await self._resolve_topic_ledger(
            candidate_rows=candidate_rows,
            base_topics=[],
            base_topic_vectors={},
            base_candidate_identity_vectors=reused_candidate_identity_vectors,
            provider=provider,
            embedding_provider=embedding_provider,
            profile_context=profile_context,
            compile_contract=compile_contract,
            compile_attempt_id=compile_attempt_id,
        )
        topics = resolved_topics.topics
        usage = resolved_topics.usage
        topic_convergence = resolved_topics.convergence
        self._emit_progress(
            "topic_ledger",
            "Topic Ledger 已完成",
            topics=len(topics),
            decisions=len(resolved_topics.decisions),
            convergence=topic_convergence.status,
        )
        if len(topics) > MAP_TOPIC_GUARD:
            raise ProfileKnowledgeMapError(
                "knowledge_map_topic_limit_exceeded",
                "merged knowledge map topics exceed the high-water guard",
                diagnostics={"topic_count": len(topics)},
            )
        reader = self._retrieval.bind_snapshot(first.snapshot_id)
        if reader.manifest.manifest_hash != first.snapshot_manifest_hash:
            raise ProfileKnowledgeMapError(
                "knowledge_map_snapshot_mismatch",
                "Preview Run snapshot hash does not match the current snapshot",
            )
        self._emit_progress(
            "aspects",
            "开始编译 TopicAspect 与证据支持",
            topics=len(topics),
            chat_parallel_calls=MAP_BUILD_MAX_PARALLEL_CHAT_CALLS,
        )
        (
            topic_aspects,
            block_aspect_supports,
            block_aspect_exclusions,
            topic_aspects_by_topic,
            aspect_usage,
        ) = await self._compile_topic_aspects(
            topics=topics,
            candidate_by_id=candidate_by_id,
            reader=reader,
            provider=provider,
            aspect_schema=aspect_schema,
        )
        self._emit_progress(
            "aspects",
            "TopicAspect 编译完成",
            aspects=len(topic_aspects),
            supports=len(block_aspect_supports),
            exclusions=len(block_aspect_exclusions),
        )
        usage.extend(aspect_usage)
        topic_vectors_raw = await _embed_all(
            embedding_provider,
            [_topic_grouping_text(topic) for topic in topics],
        )
        topic_vectors = _normalized_vector_matrix(topic_vectors_raw)
        self._emit_progress(
            "groups",
            "开始构建 Group Ledger",
            topics=len(topics),
            chat_parallel_calls=MAP_BUILD_MAX_PARALLEL_CHAT_CALLS,
        )
        groups, group_usage, group_vectors_raw, group_convergence = await self._build_groups(
            topics=topics,
            topic_vectors=topic_vectors.tolist(),
            topic_aspects_by_topic=topic_aspects_by_topic,
            provider=provider,
            embedding_provider=embedding_provider,
            profile_context=profile_context,
        )
        self._emit_progress(
            "groups",
            "Group Ledger 已完成",
            groups=len(groups),
            convergence=group_convergence.status,
        )
        usage.extend(group_usage)
        if not groups:
            raise ProfileKnowledgeMapError(
                "knowledge_map_groups_missing",
                "knowledge map did not produce groups",
            )
        if len(groups) > MAP_GROUP_GUARD:
            raise ProfileKnowledgeMapError(
                "knowledge_map_group_limit_exceeded",
                "knowledge map groups exceed the high-water guard",
                diagnostics={"group_count": len(groups)},
            )

        group_vectors = _normalized_vector_matrix(group_vectors_raw)
        group_vector_by_id = {
            group.group_id: group_vectors[index].tolist() for index, group in enumerate(groups)
        }
        topic_vector_by_id = {
            topic.topic_id: topic_vectors[index].tolist() for index, topic in enumerate(topics)
        }
        aspect_vectors_raw = await _embed_all(
            embedding_provider,
            [_aspect_search_text(aspect) for aspect in topic_aspects],
        )
        aspect_vectors = _normalized_vector_matrix(aspect_vectors_raw)
        aspect_vector_by_id = {
            aspect.aspect_id: aspect_vectors[index].tolist()
            for index, aspect in enumerate(topic_aspects)
        }
        snapshot_root = self._root / first.snapshot_id
        snapshot_root.mkdir(parents=True, exist_ok=True)
        self._emit_progress(
            "publish",
            "开始写入并校验地图产物",
            topics=len(topics),
            groups=len(groups),
            aspects=len(topic_aspects),
        )
        with tempfile.NamedTemporaryFile(
            dir=snapshot_root,
            prefix="vector-bundle-",
            suffix=".npz",
            delete=False,
        ) as temporary_bundle:
            temporary_bundle_path = Path(temporary_bundle.name)
        temporary_bundle_path.unlink(missing_ok=True)
        vector_bundle_hash = self._write_vector_bundle(
            temporary_bundle_path,
            group_vectors=group_vector_by_id,
            topic_vectors=topic_vector_by_id,
            aspect_vectors=aspect_vector_by_id,
            topic_identity_vectors=resolved_topics.topic_identity_vectors,
        )
        vector_bundle_ref = "vectors.npz"
        with tempfile.NamedTemporaryFile(
            dir=snapshot_root,
            prefix="candidate-vector-bundle-",
            suffix=".npz",
            delete=False,
        ) as temporary_candidate_bundle:
            temporary_candidate_bundle_path = Path(temporary_candidate_bundle.name)
        temporary_candidate_bundle_path.unlink(missing_ok=True)
        candidate_bundle_ref = "candidate-vectors.npz"
        candidate_bundle_hash = self._write_candidate_identity_vector_bundle(
            temporary_candidate_bundle_path,
            candidate_identity_vectors=resolved_topics.candidate_identity_vectors,
        )
        created_at = datetime.now(UTC).isoformat()
        draft_manifest = ProfileKnowledgeMapManifest(
            map_id="km_0000000000000000",
            snapshot_id=first.snapshot_id,
            snapshot_manifest_hash=first.snapshot_manifest_hash,
            profile_id=first.profile_id,
            profile_hash=first.profile_hash,
            compile_contract=compile_contract,
            compile_input_key=compile_input_key,
            compile_attempt_id=compile_attempt_id,
            profile_context=profile_context,
            representative_questions=representative_questions,
            preview_run_ids=unique_run_ids,
            document_ids=sorted({doc_id for topic in topics for doc_id in topic.source_doc_ids}),
            grouping_prompt_version=GROUPING_PROMPT_VERSION,
            grouping_policy_version=GROUPING_POLICY_VERSION,
            aspect_prompt_version=ASPECT_PROMPT_VERSION,
            aspect_policy_version=ASPECT_POLICY_VERSION,
            profile_aspect_schema=aspect_schema,
            profile_aspect_schema_hash=aspect_schema_hash,
            usage=usage,
            topics=sorted(topics, key=lambda topic: topic.topic_id),
            groups=sorted(groups, key=lambda group: group.group_id),
            topic_aspects=sorted(topic_aspects, key=lambda aspect: aspect.aspect_id),
            block_aspect_supports=sorted(
                block_aspect_supports,
                key=lambda support: support.support_id,
            ),
            block_aspect_exclusions=sorted(
                block_aspect_exclusions,
                key=lambda exclusion: (exclusion.topic_id, exclusion.block_id),
            ),
            topic_identity_judgements=sorted(
                resolved_topics.judgements,
                key=lambda judgement: judgement.judgement_id,
            ),
            topic_work_item_decisions=sorted(
                resolved_topics.decisions,
                key=lambda decision: decision.decision_id,
            ),
            topic_operations=sorted(
                resolved_topics.operations,
                key=lambda operation: operation.operation_id,
            ),
            topic_candidate_memberships=_topic_candidate_memberships(topics),
            topic_convergence=topic_convergence,
            group_convergence=group_convergence,
            group_embedding_model_id=embedding_provider.profile.model_id,
            group_embedding_input_version=GROUP_EMBEDDING_INPUT_VERSION,
            topic_embedding_model_id=embedding_provider.profile.model_id,
            topic_embedding_input_version=TOPIC_EMBEDDING_INPUT_VERSION,
            aspect_embedding_model_id=embedding_provider.profile.model_id,
            aspect_embedding_input_version=ASPECT_EMBEDDING_INPUT_VERSION,
            vector_bundle_ref=vector_bundle_ref,
            vector_bundle_hash=vector_bundle_hash,
            candidate_identity_vector_bundle_ref=candidate_bundle_ref,
            candidate_identity_vector_bundle_hash=candidate_bundle_hash,
            manifest_hash="0" * 64,
            created_at=created_at,
        )
        manifest_hash = stable_hash(_manifest_hash_payload(draft_manifest))
        map_id = f"km_{manifest_hash[:16]}"
        manifest = draft_manifest.model_copy(
            update={"map_id": map_id, "manifest_hash": manifest_hash}
        )
        path = self._map_path(first.snapshot_id, map_id)
        if path.exists() and not force_new:
            temporary_bundle_path.unlink(missing_ok=True)
            temporary_candidate_bundle_path.unlink(missing_ok=True)
            loaded = self.load(first.snapshot_id, map_id)
            if activate:
                self._write_active_pointer(
                    first.snapshot_id,
                    loaded.manifest,
                    confirm_ready_partial=confirm_ready_partial,
                )
            return loaded.model_copy(update={"reused": True})
        final_bundle_path = self._vector_bundle_path(
            first.snapshot_id,
            map_id,
            bundle_ref=vector_bundle_ref,
        )
        final_bundle_path.parent.mkdir(parents=True, exist_ok=True)
        final_candidate_bundle_path = self._vector_bundle_path(
            first.snapshot_id,
            map_id,
            bundle_ref=candidate_bundle_ref,
        )
        os.replace(temporary_candidate_bundle_path, final_candidate_bundle_path)
        os.replace(temporary_bundle_path, final_bundle_path)
        self._write_manifest(path, manifest)
        if activate:
            self._write_active_pointer(
                first.snapshot_id,
                manifest,
                confirm_ready_partial=confirm_ready_partial,
            )
        self._emit_progress(
            "complete",
            "知识地图构建完成",
            map_id=manifest.map_id,
            topics=len(manifest.topics),
            groups=len(manifest.groups),
        )
        return ProfileKnowledgeMapResult(
            manifest=manifest,
            group_vectors=group_vector_by_id,
            topic_vectors=topic_vector_by_id,
            aspect_vectors=aspect_vector_by_id,
            candidate_identity_vectors=resolved_topics.candidate_identity_vectors,
            topic_identity_vectors=resolved_topics.topic_identity_vectors,
        )

    async def plan_incremental_map_update(
        self,
        *,
        preview_run_ids: list[str],
        embedding_provider: EmbeddingProvider,
        base_map_id: str | None = None,
    ) -> IncrementalMapPlan:
        """只读比较 base map 与完整目标 Preview Runs，产出 Topic／Group 增量计划。

        `preview_run_ids` 必须覆盖目标知识库的完整文档范围；调用方不能只传本批变化的
        Runs，否则未出现在输入中的旧文档会被计划为删除。
        """

        if not preview_run_ids:
            raise ProfileKnowledgeMapError(
                "knowledge_map_preview_runs_missing",
                "at least one target Preview Run is required",
            )
        unique_target_run_ids = list(dict.fromkeys(preview_run_ids))
        with self._database.session() as session:
            target_runs = [
                session.get(ProfilePreviewRunRow, run_id) for run_id in unique_target_run_ids
            ]
            if any(run is None for run in target_runs):
                missing = [
                    run_id
                    for run_id, run in zip(unique_target_run_ids, target_runs, strict=True)
                    if run is None
                ]
                raise ProfileKnowledgeMapError(
                    "knowledge_map_preview_run_missing",
                    f"Preview Runs do not exist: {', '.join(missing)}",
                )
            concrete_target_runs = [run for run in target_runs if run is not None]
            if any(run.status != "completed" for run in concrete_target_runs):
                raise ProfileKnowledgeMapError(
                    "knowledge_map_source_run_incomplete",
                    "all target Preview Runs must be completed",
                )
            incompatible_runs = [
                run.run_id
                for run in concrete_target_runs
                if run.candidate_pipeline_version != CANDIDATE_PIPELINE_VERSION
            ]
            if incompatible_runs:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_source_run_schema_incompatible",
                    "target Preview Runs must use the flat-summary candidate pipeline",
                    diagnostics={"run_ids": incompatible_runs},
                )
            first_target = concrete_target_runs[0]
            if any(
                run.snapshot_id != first_target.snapshot_id
                or run.snapshot_manifest_hash != first_target.snapshot_manifest_hash
                or run.profile_hash != first_target.profile_hash
                or run.candidate_pipeline_version != first_target.candidate_pipeline_version
                or run.name_normalization_version != first_target.name_normalization_version
                or run.compiler_version != first_target.compiler_version
                or run.prompt_hash != first_target.prompt_hash
                for run in concrete_target_runs
            ):
                raise ProfileKnowledgeMapError(
                    "knowledge_map_scope_mismatch",
                    "target Preview Runs must share snapshot, Profile, and pipeline versions",
                )
            target_rows = list(
                session.execute(
                    select(PreviewCandidateRow)
                    .where(PreviewCandidateRow.run_id.in_(unique_target_run_ids))
                    .order_by(PreviewCandidateRow.doc_id, PreviewCandidateRow.ordinal)
                ).scalars()
            )
            if not target_rows:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_candidates_missing",
                    "target Preview Runs do not contain candidates",
                )

            base_result: ProfileKnowledgeMapResult | None = None
            base_first_run: ProfilePreviewRunRow | None = None
            base_rows: list[PreviewCandidateRow] = []
            if base_map_id is not None:
                base_result = self._load_map_by_id(base_map_id)
                base_manifest = base_result.manifest
                base_runs = [
                    session.get(ProfilePreviewRunRow, run_id)
                    for run_id in base_manifest.preview_run_ids
                ]
                if any(run is None for run in base_runs):
                    raise ProfileKnowledgeMapError(
                        "incremental_base_run_missing",
                        "base map Preview Run metadata is missing",
                    )
                base_first_run = next(run for run in base_runs if run is not None)
                base_candidate_ids = sorted(
                    {
                        source.candidate_id
                        for topic in base_manifest.topics
                        for source in topic.sources
                    }
                )
                base_rows = list(
                    session.execute(
                        select(PreviewCandidateRow).where(
                            PreviewCandidateRow.candidate_id.in_(base_candidate_ids)
                        )
                    ).scalars()
                )

        target_snapshot_ids = sorted({run.snapshot_id for run in concrete_target_runs})
        target_records = [candidate_source_record(row) for row in target_rows]
        base_records = [candidate_source_record(row) for row in base_rows]
        base_ledger = None
        group_vectors: dict[str, list[float]] = {}
        contract_reason: str | None = None
        if base_result is not None and base_first_run is not None:
            base_manifest = base_result.manifest
            group_vectors = base_result.group_vectors
            contract_reasons: list[str] = []
            if (
                base_manifest.schema_version != "profile-knowledge-map-v6"
                or base_manifest.compile_contract is None
            ):
                contract_reasons.append("base map does not contain the v6 compile contract")
            if first_target.profile_id != base_manifest.profile_id:
                contract_reasons.append("Profile ID changed")
            if first_target.profile_hash != base_manifest.profile_hash:
                contract_reasons.append("Profile hash changed")
            if first_target.candidate_pipeline_version != base_first_run.candidate_pipeline_version:
                contract_reasons.append("candidate pipeline version changed")
            if first_target.name_normalization_version != base_first_run.name_normalization_version:
                contract_reasons.append("name normalization version changed")
            if embedding_provider.profile.model_id != base_manifest.group_embedding_model_id:
                contract_reasons.append("group embedding model changed")
            if embedding_provider.profile.model_id != base_manifest.topic_embedding_model_id:
                contract_reasons.append("topic embedding model changed")
            if base_manifest.grouping_prompt_version != GROUPING_PROMPT_VERSION:
                contract_reasons.append("grouping prompt version changed")
            if base_manifest.grouping_policy_version != GROUPING_POLICY_VERSION:
                contract_reasons.append("grouping policy version changed")
            target_profile_config = _validated_profile_config(
                json.loads(first_target.profile_config_snapshot_json)
            )
            _, target_aspect_schema_hash = _profile_aspect_schema(target_profile_config)
            if target_aspect_schema_hash != base_manifest.profile_aspect_schema_hash:
                contract_reasons.append("Profile Aspect Schema contract changed")
            if base_manifest.aspect_policy_version != ASPECT_POLICY_VERSION:
                contract_reasons.append("Aspect policy version changed")
            if contract_reasons:
                contract_reason = "; ".join(contract_reasons)
            try:
                base_ledger = project_legacy_ledger(
                    manifest=base_manifest,
                    candidate_records=base_records,
                )
            except ValueError as error:
                raise ProfileKnowledgeMapError(
                    "incremental_source_identity_missing",
                    str(error),
                ) from error

        return await build_incremental_map_plan(
            base_manifest=None if base_result is None else base_result.manifest,
            base_ledger=base_ledger,
            group_vectors=group_vectors,
            base_records=base_records,
            target_records=target_records,
            target_snapshot_ids=target_snapshot_ids,
            embedding_provider=embedding_provider,
            contract_compatibility_reason=contract_reason,
        )

    async def build_incremental_map_update(
        self,
        *,
        preview_run_ids: list[str],
        provider: ChatProvider,
        embedding_provider: EmbeddingProvider,
        base_map_id: str | None = None,
        force_new: bool = False,
        activate: bool = False,
        confirm_ready_partial: bool = False,
    ) -> ProfileKnowledgeMapResult:
        """执行 no-op／full／增量 map update；当前增量执行覆盖新增、更新和删除 source。"""

        plan = await self.plan_incremental_map_update(
            preview_run_ids=preview_run_ids,
            embedding_provider=embedding_provider,
            base_map_id=base_map_id,
        )
        self._emit_progress(
            "incremental",
            "增量计划已生成",
            status=plan.status,
            base_map_id=plan.base_map_id,
            added_sources=len(plan.candidates.added_source_keys),
            removed_sources=len(plan.candidates.removed_source_keys),
            updated_sources=len(plan.candidates.updated_sources),
        )
        if plan.status == "noop" and plan.base_map_id is not None:
            base_result = self._load_map_by_id(plan.base_map_id)
            base_refs = {
                (source.extraction_run_id, source.candidate_id)
                for topic in base_result.manifest.topics
                for source in topic.sources
            }
            with self._database.session() as session:
                target_run_ids = list(dict.fromkeys(preview_run_ids))
                target_runs = [
                    session.get(ProfilePreviewRunRow, run_id) for run_id in target_run_ids
                ]
                target_refs = {
                    (row.run_id, row.candidate_id)
                    for row in session.execute(
                        select(PreviewCandidateRow).where(
                            PreviewCandidateRow.run_id.in_(target_run_ids)
                        )
                    ).scalars()
                }
            first_target = next(run for run in target_runs if run is not None)
            current_contract = _build_map_compile_contract(
                first_run=first_target,
                provider=provider,
                embedding_provider=embedding_provider,
                profile_context=base_result.manifest.profile_context,
                profile_aspect_schema_hash=base_result.manifest.profile_aspect_schema_hash,
            )
            if (
                target_refs == base_refs
                and current_contract == base_result.manifest.compile_contract
            ):
                return base_result.model_copy(update={"reused": True})
            if current_contract != base_result.manifest.compile_contract:
                return await self.build(
                    preview_run_ids=preview_run_ids,
                    provider=provider,
                    embedding_provider=embedding_provider,
                    force_new=force_new,
                    activate=activate,
                    confirm_ready_partial=confirm_ready_partial,
                )
        if plan.status in {"full", "full_rebuild_required"}:
            return await self.build(
                preview_run_ids=preview_run_ids,
                provider=provider,
                embedding_provider=embedding_provider,
                force_new=force_new,
                activate=activate,
                confirm_ready_partial=confirm_ready_partial,
            )
        if plan.base_map_id is None:
            raise ProfileKnowledgeMapError(
                "incremental_base_map_missing",
                "incremental map update requires a base map",
            )

        base_result = self._load_map_by_id(plan.base_map_id)
        base_manifest = base_result.manifest
        unique_target_run_ids = list(dict.fromkeys(preview_run_ids))
        with self._database.session() as session:
            target_runs = [
                session.get(ProfilePreviewRunRow, run_id) for run_id in unique_target_run_ids
            ]
            if any(run is None for run in target_runs):
                raise ProfileKnowledgeMapError(
                    "incremental_target_run_missing",
                    "target Preview Run metadata is missing",
                )
            concrete_runs = [run for run in target_runs if run is not None]
            first_target = concrete_runs[0]
            if first_target.snapshot_id != base_manifest.snapshot_id:
                raise ProfileKnowledgeMapError(
                    "incremental_snapshot_rebind_required",
                    "the current executor requires incremental sources in the base snapshot",
                )
            target_rows = list(
                session.execute(
                    select(PreviewCandidateRow)
                    .where(PreviewCandidateRow.run_id.in_(unique_target_run_ids))
                    .order_by(PreviewCandidateRow.doc_id, PreviewCandidateRow.ordinal)
                ).scalars()
            )
            base_candidate_ids = sorted(
                {source.candidate_id for topic in base_manifest.topics for source in topic.sources}
            )
            base_rows = list(
                session.execute(
                    select(PreviewCandidateRow).where(
                        PreviewCandidateRow.candidate_id.in_(base_candidate_ids)
                    )
                ).scalars()
            )

        target_records = [candidate_source_record(row) for row in target_rows]
        if len(first_target.snapshot_id) == 0:
            raise AssertionError("target snapshot must be present")
        reader = self._retrieval.bind_snapshot(first_target.snapshot_id)
        if reader.manifest.manifest_hash != first_target.snapshot_manifest_hash:
            raise ProfileKnowledgeMapError(
                "knowledge_map_snapshot_mismatch",
                "target Preview Run snapshot hash does not match the current snapshot",
            )

        base_row_by_id = {row.candidate_id: row for row in base_rows}
        target_row_by_id = {row.candidate_id: row for row in target_rows}
        all_row_by_id = {**base_row_by_id, **target_row_by_id}
        compile_contract = _build_map_compile_contract(
            first_run=first_target,
            provider=provider,
            embedding_provider=embedding_provider,
            profile_context=base_manifest.profile_context,
            profile_aspect_schema_hash=base_manifest.profile_aspect_schema_hash,
        )
        if compile_contract != base_manifest.compile_contract:
            return await self.build(
                preview_run_ids=preview_run_ids,
                provider=provider,
                embedding_provider=embedding_provider,
                force_new=force_new,
                activate=activate,
                confirm_ready_partial=confirm_ready_partial,
            )
        compile_input_key = _compile_input_key(
            base_map_id=base_manifest.map_id,
            preview_run_ids=unique_target_run_ids,
            compile_contract_id=compile_contract.contract_id,
        )
        compile_attempt_id = new_identifier("mca")
        self._emit_progress(
            "incremental",
            "开始执行增量 Ledger 更新",
            target_runs=len(unique_target_run_ids),
        )
        resolved_topics = await self._resolve_topic_ledger(
            candidate_rows=target_rows,
            base_candidate_rows=base_rows,
            base_topics=base_manifest.topics,
            base_topic_vectors=base_result.topic_vectors,
            base_candidate_identity_vectors=base_result.candidate_identity_vectors,
            base_topic_identity_vectors=base_result.topic_identity_vectors,
            provider=provider,
            embedding_provider=embedding_provider,
            profile_context=base_manifest.profile_context,
            compile_contract=compile_contract,
            compile_attempt_id=compile_attempt_id,
        )
        retained_topics = resolved_topics.topics
        topic_by_id = {topic.topic_id: topic for topic in retained_topics}
        base_topic_by_id = {topic.topic_id: topic for topic in base_manifest.topics}
        retired_topic_ids = set(base_topic_by_id) - set(topic_by_id)
        new_topic_ids = set(topic_by_id) - set(base_topic_by_id)
        revised_topic_ids = {
            topic_id
            for topic_id in set(base_topic_by_id) & set(topic_by_id)
            if base_topic_by_id[topic_id].revision_id != topic_by_id[topic_id].revision_id
        }
        dirty_topic_ids = revised_topic_ids | new_topic_ids
        dirty_topics = [topic_by_id[topic_id] for topic_id in sorted(dirty_topic_ids)]
        existing_aspects = [
            aspect
            for aspect in base_manifest.topic_aspects
            if aspect.topic_id not in retired_topic_ids and aspect.topic_id not in dirty_topic_ids
        ]
        existing_supports = [
            support
            for support in base_manifest.block_aspect_supports
            if support.topic_id not in retired_topic_ids and support.topic_id not in dirty_topic_ids
        ]
        existing_exclusions = [
            exclusion
            for exclusion in base_manifest.block_aspect_exclusions
            if exclusion.topic_id not in retired_topic_ids
            and exclusion.topic_id not in dirty_topic_ids
        ]
        compiled_aspects: list[TopicAspect] = []
        compiled_supports: list[BlockAspectSupport] = []
        compiled_exclusions: list[BlockAspectExclusion] = []
        aspect_usage: list[dict[str, object]] = []
        if dirty_topics:
            self._emit_progress(
                "aspects",
                "开始增量重编译 TopicAspect",
                dirty_topics=len(dirty_topics),
            )
            (
                compiled_aspects,
                compiled_supports,
                compiled_exclusions,
                _aspects_by_topic,
                aspect_usage,
            ) = await self._compile_topic_aspects(
                topics=dirty_topics,
                candidate_by_id=all_row_by_id,
                reader=reader,
                provider=provider,
                aspect_schema=base_manifest.profile_aspect_schema,
            )
            self._emit_progress(
                "aspects",
                "增量 TopicAspect 编译完成",
                aspects=len(compiled_aspects),
            )

        topic_aspects = [*existing_aspects, *compiled_aspects]
        block_aspect_supports = [*existing_supports, *compiled_supports]
        block_aspect_exclusions = [*existing_exclusions, *compiled_exclusions]
        aspects_by_topic: dict[str, list[TopicAspect]] = defaultdict(list)
        for aspect in topic_aspects:
            aspects_by_topic[aspect.topic_id].append(aspect)

        groups = self._apply_incremental_group_updates(
            groups=base_manifest.groups,
            topics=retained_topics,
            topic_by_id=topic_by_id,
            retired_topic_ids=retired_topic_ids,
            new_topic_ids=new_topic_ids,
            pending_group_candidates=plan.pending_source_group_candidates,
            target_records=target_records,
            aspects_by_topic=aspects_by_topic,
        )
        self._emit_progress(
            "groups",
            "增量 Group Ledger 已更新",
            groups=len(groups),
        )
        topic_by_id = {topic.topic_id: topic for topic in retained_topics}
        if {topic_id for group in groups for topic_id in group.child_topic_ids} != set(topic_by_id):
            missing = sorted(
                set(topic_by_id)
                - {topic_id for group in groups for topic_id in group.child_topic_ids}
            )
            raise ProfileKnowledgeMapError(
                "incremental_group_coverage_invalid",
                "incremental map left Topics without a Group",
                diagnostics={"missing_topic_ids": missing},
            )

        topic_vectors_by_id = {
            topic_id: vector
            for topic_id, vector in base_result.topic_vectors.items()
            if topic_id in topic_by_id and topic_id not in dirty_topic_ids
        }
        if dirty_topics:
            dirty_texts = [_topic_grouping_text(topic) for topic in dirty_topics]
            dirty_vectors = await _embed_all(embedding_provider, dirty_texts)
            for topic, vector in zip(dirty_topics, dirty_vectors, strict=True):
                topic_vectors_by_id[topic.topic_id] = _normalize_vector(vector)

        group_by_id = {group.group_id: group for group in groups}
        affected_group_ids = self._incremental_affected_group_ids(
            base_groups=base_manifest.groups,
            final_groups=groups,
            retired_topic_ids=retired_topic_ids,
            dirty_topic_ids=dirty_topic_ids,
        )
        group_vectors_by_id = {
            group_id: vector
            for group_id, vector in base_result.group_vectors.items()
            if group_id in group_by_id and group_id not in affected_group_ids
        }
        missing_group_ids = sorted(set(group_by_id) - set(group_vectors_by_id))
        if missing_group_ids:
            missing_group_vectors = await _embed_all(
                embedding_provider,
                [_group_search_text(group_by_id[group_id]) for group_id in missing_group_ids],
            )
            for group_id, vector in zip(
                missing_group_ids,
                missing_group_vectors,
                strict=True,
            ):
                group_vectors_by_id[group_id] = _normalize_vector(vector)

        aspect_ids = {aspect.aspect_id for aspect in topic_aspects}
        aspect_vectors_by_id = {
            aspect_id: vector
            for aspect_id, vector in base_result.aspect_vectors.items()
            if aspect_id in aspect_ids
            and aspect_id not in {aspect.aspect_id for aspect in compiled_aspects}
        }
        missing_aspects = [
            aspect for aspect in topic_aspects if aspect.aspect_id not in aspect_vectors_by_id
        ]
        if missing_aspects:
            missing_aspect_vectors = await _embed_all(
                embedding_provider,
                [_aspect_search_text(aspect) for aspect in missing_aspects],
            )
            for aspect, vector in zip(missing_aspects, missing_aspect_vectors, strict=True):
                aspect_vectors_by_id[aspect.aspect_id] = _normalize_vector(vector)

        snapshot_root = self._root / first_target.snapshot_id
        snapshot_root.mkdir(parents=True, exist_ok=True)
        self._emit_progress(
            "publish",
            "开始写入增量地图产物",
            topics=len(retained_topics),
            groups=len(groups),
        )
        with tempfile.NamedTemporaryFile(
            dir=snapshot_root,
            prefix="vector-bundle-",
            suffix=".npz",
            delete=False,
        ) as temporary_bundle:
            temporary_bundle_path = Path(temporary_bundle.name)
        temporary_bundle_path.unlink(missing_ok=True)
        vector_bundle_hash = self._write_vector_bundle(
            temporary_bundle_path,
            group_vectors=group_vectors_by_id,
            topic_vectors=topic_vectors_by_id,
            aspect_vectors=aspect_vectors_by_id,
            topic_identity_vectors=resolved_topics.topic_identity_vectors,
        )
        with tempfile.NamedTemporaryFile(
            dir=snapshot_root,
            prefix="candidate-vector-bundle-",
            suffix=".npz",
            delete=False,
        ) as temporary_candidate_bundle:
            temporary_candidate_bundle_path = Path(temporary_candidate_bundle.name)
        temporary_candidate_bundle_path.unlink(missing_ok=True)
        candidate_bundle_ref = "candidate-vectors.npz"
        candidate_bundle_hash = self._write_candidate_identity_vector_bundle(
            temporary_candidate_bundle_path,
            candidate_identity_vectors=resolved_topics.candidate_identity_vectors,
        )
        incremental_usage = {
            "stage": "incremental_ledger_update",
            "base_map_id": base_manifest.map_id,
            "base_snapshot_id": base_manifest.snapshot_id,
            "target_snapshot_id": first_target.snapshot_id,
            "added_source_count": len(plan.candidates.added_source_keys),
            "removed_source_count": len(plan.candidates.removed_source_keys),
            "updated_source_count": len(plan.candidates.updated_sources),
            "revised_topic_count": len(revised_topic_ids),
            "retired_topic_count": len(retired_topic_ids),
            "new_topic_count": len(new_topic_ids),
            "reused_topic_count": len(topic_by_id) - len(dirty_topic_ids),
            "final_group_count": len(groups),
        }
        draft_manifest = base_manifest.model_copy(
            update={
                "schema_version": "profile-knowledge-map-v6",
                "map_id": "km_0000000000000000",
                "manifest_hash": "0" * 64,
                "base_map_id": base_manifest.map_id,
                "compilation_mode": "incremental",
                "delta_summary": plan.model_dump(mode="json"),
                "snapshot_id": first_target.snapshot_id,
                "snapshot_manifest_hash": first_target.snapshot_manifest_hash,
                "compile_contract": compile_contract,
                "compile_input_key": compile_input_key,
                "compile_attempt_id": compile_attempt_id,
                "preview_run_ids": unique_target_run_ids,
                "document_ids": sorted(
                    {doc_id for topic in retained_topics for doc_id in topic.source_doc_ids}
                ),
                "usage": [
                    incremental_usage,
                    *resolved_topics.usage,
                    *aspect_usage,
                ],
                "topics": sorted(retained_topics, key=lambda topic: topic.topic_id),
                "groups": sorted(groups, key=lambda group: group.group_id),
                "topic_aspects": sorted(topic_aspects, key=lambda aspect: aspect.aspect_id),
                "block_aspect_supports": sorted(
                    block_aspect_supports,
                    key=lambda support: support.support_id,
                ),
                "block_aspect_exclusions": sorted(
                    block_aspect_exclusions,
                    key=lambda exclusion: (exclusion.topic_id, exclusion.block_id),
                ),
                "topic_identity_judgements": sorted(
                    resolved_topics.judgements,
                    key=lambda judgement: judgement.judgement_id,
                ),
                "topic_work_item_decisions": sorted(
                    resolved_topics.decisions,
                    key=lambda decision: decision.decision_id,
                ),
                "topic_operations": sorted(
                    resolved_topics.operations,
                    key=lambda operation: operation.operation_id,
                ),
                "topic_candidate_memberships": _topic_candidate_memberships(retained_topics),
                "topic_convergence": resolved_topics.convergence,
                "group_embedding_model_id": embedding_provider.profile.model_id,
                "topic_embedding_model_id": embedding_provider.profile.model_id,
                "aspect_embedding_model_id": embedding_provider.profile.model_id,
                "vector_bundle_ref": "vectors.npz",
                "vector_bundle_hash": vector_bundle_hash,
                "candidate_identity_vector_bundle_ref": candidate_bundle_ref,
                "candidate_identity_vector_bundle_hash": candidate_bundle_hash,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        manifest_hash = stable_hash(_manifest_hash_payload(draft_manifest))
        manifest = draft_manifest.model_copy(
            update={
                "map_id": f"km_{manifest_hash[:16]}",
                "manifest_hash": manifest_hash,
            }
        )
        path = self._map_path(first_target.snapshot_id, manifest.map_id)
        if path.exists() and not force_new:
            temporary_bundle_path.unlink(missing_ok=True)
            temporary_candidate_bundle_path.unlink(missing_ok=True)
            loaded = self.load(first_target.snapshot_id, manifest.map_id)
            if activate:
                self._write_active_pointer(
                    first_target.snapshot_id,
                    loaded.manifest,
                    confirm_ready_partial=confirm_ready_partial,
                )
            return loaded.model_copy(update={"reused": True})
        final_bundle_path = self._vector_bundle_path(
            first_target.snapshot_id,
            manifest.map_id,
            bundle_ref=manifest.vector_bundle_ref,
        )
        final_bundle_path.parent.mkdir(parents=True, exist_ok=True)
        assert manifest.candidate_identity_vector_bundle_ref is not None
        final_candidate_bundle_path = self._vector_bundle_path(
            first_target.snapshot_id,
            manifest.map_id,
            bundle_ref=manifest.candidate_identity_vector_bundle_ref,
        )
        os.replace(temporary_candidate_bundle_path, final_candidate_bundle_path)
        os.replace(temporary_bundle_path, final_bundle_path)
        self._write_manifest(path, manifest)
        if activate:
            self._write_active_pointer(
                first_target.snapshot_id,
                manifest,
                confirm_ready_partial=confirm_ready_partial,
            )
        self._emit_progress(
            "complete",
            "增量知识地图构建完成",
            map_id=manifest.map_id,
            topics=len(manifest.topics),
            groups=len(manifest.groups),
        )
        return ProfileKnowledgeMapResult(
            manifest=manifest,
            group_vectors=group_vectors_by_id,
            topic_vectors=topic_vectors_by_id,
            aspect_vectors=aspect_vectors_by_id,
            candidate_identity_vectors=resolved_topics.candidate_identity_vectors,
            topic_identity_vectors=resolved_topics.topic_identity_vectors,
        )

    @staticmethod
    def _apply_incremental_group_updates(
        *,
        groups: list[ProfileMapGroup],
        topics: list[ProfileMapTopic],
        topic_by_id: dict[str, ProfileMapTopic],
        retired_topic_ids: set[str],
        new_topic_ids: set[str],
        pending_group_candidates: list[PendingSourceGroupCandidates],
        target_records: list[CandidateSourceRecord],
        aspects_by_topic: dict[str, list[TopicAspect]],
    ) -> list[ProfileMapGroup]:
        """以确定性的小步操作更新 Group，复杂语义裁决留给后续 executor 扩展。"""

        group_by_id = {group.group_id: group for group in groups}
        for group in list(group_by_id.values()):
            remaining = [
                topic_id for topic_id in group.child_topic_ids if topic_id not in retired_topic_ids
            ]
            if not remaining:
                group_by_id.pop(group.group_id, None)
                continue
            group_by_id[group.group_id] = group.model_copy(update={"child_topic_ids": remaining})

        scores_by_source_key: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for item in pending_group_candidates:
            scores_by_source_key[item.source_key] = [
                (
                    score.group_lineage_id,
                    score.score,
                )
                for score in item.candidate_groups
                if score.group_lineage_id in group_by_id
            ]
        source_key_by_candidate_id = {
            record.candidate_id: record.source_key for record in target_records
        }
        for topic_id in sorted(new_topic_ids):
            topic = topic_by_id[topic_id]
            candidate_scores: dict[str, float] = {}
            for source in topic.sources:
                source_key = source_key_by_candidate_id.get(source.candidate_id)
                if source_key is None:
                    continue
                for group_id, score in scores_by_source_key.get(source_key, []):
                    candidate_scores[group_id] = max(
                        candidate_scores.get(group_id, 0.0),
                        score,
                    )
            best_group_id = (
                min(
                    candidate_scores,
                    key=lambda group_id: (-candidate_scores[group_id], group_id),
                )
                if candidate_scores
                else None
            )
            if (
                best_group_id is not None
                and candidate_scores[best_group_id] >= INCREMENTAL_GROUP_ATTACH_SCORE_FLOOR
            ):
                group = group_by_id[best_group_id]
                group_by_id[best_group_id] = group.model_copy(
                    update={"child_topic_ids": ordered_unique([*group.child_topic_ids, topic_id])}
                )
                continue
            group_hash = stable_hash(
                {
                    "label": normalize_name(topic.title),
                    "topic_ids": [topic_id],
                }
            )
            group_id = f"wg_{group_hash[:16]}"
            group_by_id[group_id] = ProfileMapGroup(
                group_id=group_id,
                label=topic.title,
                aliases=topic.aliases,
                summary="；".join(topic.summaries),
                retrieval_questions=[f"{topic.title} 的核心证据是什么？"],
                child_topic_ids=[topic_id],
                source_doc_ids=topic.source_doc_ids,
                aspect_kinds=ordered_unique(
                    aspect.kind for aspect in aspects_by_topic.get(topic_id, [])
                ),
                aspect_labels=ordered_unique(
                    aspect.label for aspect in aspects_by_topic.get(topic_id, [])
                )[:20],
            )

        result: list[ProfileMapGroup] = []
        for group in group_by_id.values():
            if not group.child_topic_ids:
                continue
            source_doc_ids = sorted(
                {
                    doc_id
                    for topic_id in group.child_topic_ids
                    for doc_id in topic_by_id[topic_id].source_doc_ids
                }
            )
            group = _derive_group_aspect_fields(
                group.model_copy(update={"source_doc_ids": source_doc_ids}),
                topic_aspects_by_topic=aspects_by_topic,
            )
            result.append(group)
        return sorted(result, key=lambda group: group.group_id)

    @staticmethod
    def _incremental_affected_group_ids(
        *,
        base_groups: list[ProfileMapGroup],
        final_groups: list[ProfileMapGroup],
        retired_topic_ids: set[str],
        dirty_topic_ids: set[str],
    ) -> set[str]:
        final_ids = {group.group_id for group in final_groups}
        changed: set[str] = set()
        for group in base_groups:
            if group.group_id not in final_ids:
                changed.add(group.group_id)
                continue
            if retired_topic_ids.intersection(group.child_topic_ids):
                changed.add(group.group_id)
                continue
            if dirty_topic_ids.intersection(group.child_topic_ids):
                changed.add(group.group_id)
        base_child_sets = {group.group_id: set(group.child_topic_ids) for group in base_groups}
        for group in final_groups:
            if base_child_sets.get(group.group_id) != set(group.child_topic_ids):
                changed.add(group.group_id)
        return changed

    def _load_map_by_id(self, map_id: str) -> ProfileKnowledgeMapResult:
        paths = sorted(self._root.glob(f"*/{map_id}/map.json"))
        if not paths:
            raise ProfileKnowledgeMapError(
                "knowledge_map_not_found",
                f"Knowledge map does not exist: {map_id}",
            )
        if len(paths) > 1:
            raise ProfileKnowledgeMapError(
                "knowledge_map_identity_ambiguous",
                f"Knowledge map ID exists under multiple snapshots: {map_id}",
            )
        return self.load(paths[0].parent.parent.name, map_id)

    def _latest_candidate_identity_vectors(
        self,
        *,
        snapshot_id: str,
        compile_contract: ProfileMapCompileContract,
    ) -> dict[str, PersistedIdentityVector]:
        """从同一 snapshot／Profile／契约的最近地图复用候选身份向量。"""

        snapshot_root = self._root / snapshot_id
        if not snapshot_root.exists():
            return {}
        map_paths = sorted(
            snapshot_root.glob("km_*/map.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in map_paths:
            try:
                manifest = ProfileKnowledgeMapManifest.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if (
                    manifest.schema_version != "profile-knowledge-map-v6"
                    or manifest.compile_contract != compile_contract
                    or manifest.candidate_identity_vector_bundle_ref is None
                    or manifest.candidate_identity_vector_bundle_hash is None
                ):
                    continue
                candidate_identity_vectors = self._read_candidate_identity_vector_bundle(
                    self._vector_bundle_path(
                        snapshot_id,
                        manifest.map_id,
                        bundle_ref=manifest.candidate_identity_vector_bundle_ref,
                    ),
                    expected_hash=manifest.candidate_identity_vector_bundle_hash,
                )
                return candidate_identity_vectors
            except (OSError, ValueError, ProfileKnowledgeMapError):
                continue
        return {}

    def load(self, snapshot_id: str, map_id: str) -> ProfileKnowledgeMapResult:
        """读取并校验不可变知识地图 artifact。"""

        path = self._map_path(snapshot_id, map_id)
        try:
            manifest = ProfileKnowledgeMapManifest.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as error:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                f"knowledge map is unreadable: {map_id}",
            ) from error
        expected = stable_hash(_manifest_hash_payload(manifest))
        if (
            manifest.snapshot_id != snapshot_id
            or manifest.map_id != map_id
            or expected != manifest.manifest_hash
        ):
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "knowledge map identity or hash does not match",
            )
        if manifest.schema_version == "profile-knowledge-map-v6" and (
            manifest.candidate_identity_vector_bundle_ref is None
            or manifest.candidate_identity_vector_bundle_hash is None
        ):
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "candidate identity vector bundle identity is missing",
            )
        (
            group_vectors,
            topic_vectors,
            aspect_vectors,
            topic_identity_vectors,
        ) = self._read_vector_bundle(
            self._vector_bundle_path(
                snapshot_id,
                map_id,
                bundle_ref=manifest.vector_bundle_ref,
            ),
            expected_hash=manifest.vector_bundle_hash,
        )
        candidate_identity_vectors = (
            self._read_candidate_identity_vector_bundle(
                self._vector_bundle_path(
                    snapshot_id,
                    map_id,
                    bundle_ref=cast(
                        str,
                        manifest.candidate_identity_vector_bundle_ref,
                    ),
                ),
                expected_hash=cast(
                    str,
                    manifest.candidate_identity_vector_bundle_hash,
                ),
            )
            if manifest.schema_version == "profile-knowledge-map-v6"
            else {}
        )
        if set(group_vectors) != {group.group_id for group in manifest.groups}:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "group vector IDs do not match the map",
            )
        if set(topic_vectors) != {topic.topic_id for topic in manifest.topics}:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "topic vector IDs do not match the map",
            )
        if set(aspect_vectors) != {aspect.aspect_id for aspect in manifest.topic_aspects}:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "aspect vector IDs do not match the map",
            )
        if manifest.schema_version == "profile-knowledge-map-v6":
            if set(topic_identity_vectors) != {
                f"topic:{topic.topic_id}:{topic.revision_id}" for topic in manifest.topics
            }:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_invalid",
                    "Topic identity vectors do not match active Topic revisions",
                )
            expected_candidates = {
                f"candidate:{source.extraction_run_id}:{source.candidate_id}"
                for topic in manifest.topics
                for source in topic.sources
            }
            if set(candidate_identity_vectors) != expected_candidates:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_invalid",
                    "candidate identity vectors do not match map sources",
                )
        return ProfileKnowledgeMapResult(
            manifest=manifest,
            group_vectors=group_vectors,
            topic_vectors=topic_vectors,
            aspect_vectors=aspect_vectors,
            candidate_identity_vectors=candidate_identity_vectors,
            topic_identity_vectors=topic_identity_vectors,
        )

    def latest(self, snapshot_id: str) -> ProfileKnowledgeMapResult | None:
        """返回指定 snapshot 最新创建的知识地图，不存在时返回空。"""

        snapshot_root = self._root / snapshot_id
        if not snapshot_root.exists():
            return None
        candidates: list[ProfileKnowledgeMapResult] = []
        for path in sorted(snapshot_root.glob("km_*/map.json"), reverse=True):
            try:
                candidates.append(self.load(snapshot_id, path.parent.name))
            except ProfileKnowledgeMapError:
                continue
        return max(
            candidates,
            key=lambda item: item.manifest.created_at,
            default=None,
        )

    def activate(
        self,
        snapshot_id: str,
        map_id: str,
        *,
        confirm_ready_partial: bool = False,
    ) -> ProfileKnowledgeMapActivePointer:
        """显式把一张已验证地图激活为 snapshot 的 Profile 地图。

        `ready_partial` 表示全局收敛触达安全上限，默认拒绝激活。调用方只有在人工
        核对收敛诊断后才能传入 `confirm_ready_partial=True`。
        """

        manifest = self.load(snapshot_id, map_id).manifest
        return self._write_active_pointer(
            snapshot_id,
            manifest,
            confirm_ready_partial=confirm_ready_partial,
        )

    def active_for_selected_profile(
        self,
        snapshot_id: str,
    ) -> ProfileKnowledgeMapResult | None:
        """读取与 selected Profile、Aspect 编译契约和 snapshot 同时匹配的 active map。"""

        with self._database.session() as session:
            selection = session.get(KnowledgeBaseProfileSelectionRow, DEFAULT_PROFILE_ID)
            if selection is None or selection.selected_profile_id is None:
                return None
            profile = session.get(ExtractionProfileRow, selection.selected_profile_id)
            if profile is None:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_active_scope_mismatch",
                    "selected Profile does not exist",
                )
            profile_hash = selection.selected_profile_hash
            if profile_hash is None or profile.profile_hash != profile_hash:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_active_scope_mismatch",
                    "selected Profile hash does not match the frozen Profile",
                )
            profile_config = _profile_config_from_row(profile)
        _, aspect_schema_hash = _profile_aspect_schema(profile_config)
        return self._active_map(
            snapshot_id,
            selection.selected_profile_id,
            profile_hash,
            aspect_schema_hash,
        )

    def _active_map(
        self,
        snapshot_id: str,
        profile_id: str,
        profile_hash: str,
        aspect_schema_hash: str,
    ) -> ProfileKnowledgeMapResult | None:
        pointer_path = self._active_pointer_path(snapshot_id)
        if not pointer_path.exists():
            return None
        try:
            pointer = ProfileKnowledgeMapActivePointer.model_validate_json(
                pointer_path.read_text(encoding="utf-8")
            )
        except Exception as error:
            raise ProfileKnowledgeMapError(
                "knowledge_map_active_pointer_invalid",
                "active knowledge map pointer is unreadable",
            ) from error
        if (
            pointer.snapshot_id != snapshot_id
            or pointer.profile_id != profile_id
            or pointer.profile_hash != profile_hash
            or pointer.profile_aspect_schema_hash != aspect_schema_hash
            or pointer.aspect_policy_version != ASPECT_POLICY_VERSION
        ):
            raise ProfileKnowledgeMapError(
                "knowledge_map_active_scope_mismatch",
                "active knowledge map does not match the selected Profile, Aspect contract, "
                "and snapshot",
            )
        map_result = self.load(snapshot_id, pointer.map_id)
        if (
            map_result.manifest.profile_id != profile_id
            or map_result.manifest.profile_hash != profile_hash
            or map_result.manifest.manifest_hash != pointer.manifest_hash
            or map_result.manifest.profile_aspect_schema_hash != aspect_schema_hash
            or map_result.manifest.aspect_policy_version != ASPECT_POLICY_VERSION
        ):
            raise ProfileKnowledgeMapError(
                "knowledge_map_active_scope_mismatch",
                "active map manifest does not match the selected Profile and Aspect contract",
            )
        return map_result

    def _write_active_pointer(
        self,
        snapshot_id: str,
        manifest: ProfileKnowledgeMapManifest,
        *,
        confirm_ready_partial: bool,
    ) -> ProfileKnowledgeMapActivePointer:
        convergence_statuses = {
            manifest.topic_convergence.status,
            manifest.group_convergence.status,
        }
        if "failed" in convergence_statuses:
            raise ProfileKnowledgeMapError(
                "knowledge_map_activation_not_ready",
                "failed knowledge maps cannot be activated",
            )
        if "ready_partial" in convergence_statuses and not confirm_ready_partial:
            raise ProfileKnowledgeMapError(
                "knowledge_map_activation_not_ready",
                "ready_partial knowledge maps require explicit human confirmation",
                diagnostics={
                    "topic_convergence": manifest.topic_convergence.model_dump(mode="json"),
                    "group_convergence": manifest.group_convergence.model_dump(mode="json"),
                },
            )
        pointer = ProfileKnowledgeMapActivePointer(
            snapshot_id=snapshot_id,
            profile_id=manifest.profile_id,
            profile_hash=manifest.profile_hash,
            profile_aspect_schema_hash=manifest.profile_aspect_schema_hash,
            aspect_policy_version=manifest.aspect_policy_version,
            map_id=manifest.map_id,
            manifest_hash=manifest.manifest_hash,
            activated_at=datetime.now(UTC).isoformat(),
        )
        path = self._active_pointer_path(snapshot_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(pointer.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return pointer

    def _active_pointer_path(self, snapshot_id: str) -> Path:
        return self._root / snapshot_id / "active-map.json"

    async def _compile_topic_aspects(
        self,
        *,
        topics: list[ProfileMapTopic],
        candidate_by_id: dict[str, PreviewCandidateRow],
        reader: RetrievalSnapshotReader,
        provider: ChatProvider,
        aspect_schema: ProfileAspectSchema,
    ) -> tuple[
        list[TopicAspect],
        list[BlockAspectSupport],
        list[BlockAspectExclusion],
        dict[str, list[TopicAspect]],
        list[dict[str, object]],
    ]:
        """用 Catalog-first 流程编译 TopicAspects 与 Block supports。"""

        all_block_ids = ordered_unique(
            block_id for topic in topics for block_id in topic.source_block_ids
        )
        blocks = reader.get_blocks(all_block_ids).blocks
        block_by_id = {block.block_id: block for block in blocks}
        if set(block_by_id) != set(all_block_ids):
            raise ProfileKnowledgeMapError(
                "knowledge_map_aspect_invalid",
                "Aspect compilation could not load every source block",
            )
        allowed_kinds = {definition.kind for definition in aspect_schema.definitions}
        usage: list[dict[str, object]] = []
        catalog_by_topic: dict[str, list[TopicAspect]] = {}
        topics_by_id = {topic.topic_id: topic for topic in topics}
        semaphore = asyncio.Semaphore(MAP_BUILD_MAX_PARALLEL_CHAT_CALLS)

        async def catalog_batch(
            batch: list[ProfileMapTopic],
        ) -> tuple[list[TopicAspect], list[dict[str, object]]]:
            cards = []
            for topic in batch:
                candidate_cards = []
                for source in topic.sources:
                    row = candidate_by_id[source.candidate_id]
                    candidate_cards.append(
                        {
                            "candidate_id": source.candidate_id,
                            "doc_id": source.doc_id,
                            "summaries": json_loads_string_list(row.summaries_json),
                            "selection_reasons": _load_json_list(row.selection_reasons_json),
                            "channel_ids": source.channel_ids,
                        }
                    )
                cards.append(
                    {
                        "topic_id": topic.topic_id,
                        "title": topic.title,
                        "aliases": topic.aliases,
                        "summaries": topic.summaries,
                        "source_candidates": candidate_cards,
                    }
                )
            allowed_topics = {
                topic.topic_id: {source.candidate_id for source in topic.sources} for topic in batch
            }
            async with semaphore:
                payload, batch_usage, diagnostics = await _complete_structured_json(
                    provider=provider,
                    messages=[
                        ChatMessage(role="system", content=ASPECT_CATALOG_PROMPT),
                        ChatMessage(
                            role="user",
                            content=json_dumps(
                                {
                                    "profile_aspect_schema": aspect_schema.model_dump(mode="json"),
                                    "topics": cards,
                                }
                            ),
                        ),
                    ],
                    response_type=_AspectCatalogPayload,
                    stage="topic_aspect_catalog",
                    invalid_code="knowledge_map_aspect_catalog_invalid",
                    first_max_output_tokens=ASPECT_CATALOG_MAX_OUTPUT_TOKENS,
                    retry_max_output_tokens=GROUPING_RETRY_MAX_OUTPUT_TOKENS,
                    validator=lambda value: _validate_aspect_catalog(
                        value,
                        allowed_topics=allowed_topics,
                        allowed_kinds=allowed_kinds,
                    ),
                )
            schema_importance = {
                definition.kind: definition.importance for definition in aspect_schema.definitions
            }
            compiled: list[TopicAspect] = []
            for topic_result in payload.topics:
                topic_revision_id = topics_by_id[topic_result.topic_id].revision_id
                for draft in topic_result.aspects:
                    aspect_id = (
                        "ta_"
                        + stable_hash(
                            {
                                "topic_revision_id": topic_revision_id,
                                "kind": normalize_name(draft.kind),
                                "label": normalize_name(draft.label),
                            }
                        )[:16]
                    )
                    compiled.append(
                        TopicAspect(
                            aspect_id=aspect_id,
                            topic_id=topic_result.topic_id,
                            topic_revision_id=topic_revision_id,
                            kind=draft.kind,
                            label=draft.label,
                            description=draft.description,
                            importance=round(
                                0.6 * draft.importance + 0.4 * schema_importance[draft.kind],
                                6,
                            ),
                            source_candidate_ids=ordered_unique(draft.source_candidate_ids),
                        )
                    )
            return compiled, [
                {
                    **batch_usage,
                    "input_topics": len(batch),
                    "output_aspects": len(compiled),
                },
                *diagnostics,
            ]

        catalog_results = await asyncio.gather(
            *(catalog_batch(batch) for batch in _chunks(topics, ASPECT_CATALOG_TOPICS_PER_BATCH))
        )
        for compiled, batch_usage in catalog_results:
            usage.extend(batch_usage)
            for aspect in compiled:
                catalog_by_topic.setdefault(aspect.topic_id, []).append(aspect)

        # 同一 Topic 内若模型返回同义标签，按 kind/label 合并，保留最高 importance。
        merged_catalog: dict[str, list[TopicAspect]] = {}
        for topic_id, aspects in catalog_by_topic.items():
            by_key: dict[tuple[str, str], TopicAspect] = {}
            for aspect in aspects:
                key = (aspect.kind, normalize_name(aspect.label))
                current = by_key.get(key)
                if current is None:
                    by_key[key] = aspect
                else:
                    by_key[key] = current.model_copy(
                        update={
                            "importance": max(current.importance, aspect.importance),
                            "source_candidate_ids": ordered_unique(
                                [
                                    *current.source_candidate_ids,
                                    *aspect.source_candidate_ids,
                                ]
                            ),
                        }
                    )
            merged_catalog[topic_id] = sorted(by_key.values(), key=lambda aspect: aspect.aspect_id)
        if set(merged_catalog) != set(topics_by_id):
            raise ProfileKnowledgeMapError(
                "knowledge_map_aspect_invalid",
                "Aspect catalog did not produce aspects for every Topic",
            )

        mapping_results = await asyncio.gather(
            *(
                self._map_aspect_blocks(
                    topics=batch,
                    aspects_by_topic=merged_catalog,
                    block_by_id=block_by_id,
                    provider=provider,
                    semaphore=semaphore,
                )
                for batch in _topic_batches_by_block_budget(
                    topics,
                    max_topics=ASPECT_MAPPING_TOPICS_PER_BATCH,
                    max_blocks=ASPECT_MAPPING_MAX_BLOCKS,
                )
            )
        )
        supports: list[BlockAspectSupport] = []
        exclusions: list[_MappedBlockExclusion] = []
        for batch_supports, batch_exclusions, batch_usage in mapping_results:
            supports.extend(batch_supports)
            exclusions.extend(batch_exclusions)
            usage.extend(batch_usage)
        review_topic_ids = {
            exclusion.topic_id
            for exclusion in exclusions
            if exclusion.classification == "missing_aspect"
        }
        if review_topic_ids:
            extension_usage, extension_count = await self._extend_aspect_catalog_for_unclassified(
                topics=[topic for topic in topics if topic.topic_id in review_topic_ids],
                aspects_by_topic=merged_catalog,
                exclusions=[
                    exclusion
                    for exclusion in exclusions
                    if exclusion.classification == "missing_aspect"
                ],
                block_by_id=block_by_id,
                profile_aspect_schema=aspect_schema,
                provider=provider,
                semaphore=semaphore,
            )
            usage.extend(extension_usage)
            if extension_count:
                review_topics = [topic for topic in topics if topic.topic_id in review_topic_ids]
                remapping_results = await asyncio.gather(
                    *(
                        self._map_aspect_blocks(
                            topics=batch,
                            aspects_by_topic=merged_catalog,
                            block_by_id=block_by_id,
                            provider=provider,
                            semaphore=semaphore,
                        )
                        for batch in _topic_batches_by_block_budget(
                            review_topics,
                            max_topics=ASPECT_MAPPING_TOPICS_PER_BATCH,
                            max_blocks=ASPECT_MAPPING_MAX_BLOCKS,
                        )
                    )
                )
                supports = [
                    support for support in supports if support.topic_id not in review_topic_ids
                ]
                exclusions = [
                    exclusion
                    for exclusion in exclusions
                    if exclusion.topic_id not in review_topic_ids
                ]
                for batch_supports, batch_exclusions, batch_usage in remapping_results:
                    supports.extend(batch_supports)
                    exclusions.extend(batch_exclusions)
                    usage.extend(batch_usage)
        unresolved_missing_aspects = [
            exclusion for exclusion in exclusions if exclusion.classification == "missing_aspect"
        ]
        if unresolved_missing_aspects:
            raise ProfileKnowledgeMapError(
                "knowledge_map_aspect_unresolved",
                "Aspect catalog extension did not resolve every missing aspect",
                diagnostics={
                    "blocks": [
                        f"{exclusion.topic_id}/{exclusion.block_id}"
                        for exclusion in unresolved_missing_aspects
                    ]
                },
            )
        published_exclusions = [
            BlockAspectExclusion(
                exclusion_id=(
                    "bae_"
                    + stable_hash(
                        {
                            "topic_revision_id": exclusion.topic_revision_id,
                            "block_id": exclusion.block_id,
                        }
                    )[:16]
                ),
                block_id=exclusion.block_id,
                topic_id=exclusion.topic_id,
                topic_revision_id=exclusion.topic_revision_id,
                reason_code=exclusion.classification,
                reason=exclusion.reason,
            )
            for exclusion in exclusions
        ]
        supported_aspect_ids = {support.aspect_id for support in supports}
        dropped_aspect_ids: list[str] = []
        for topic_id, aspects in list(merged_catalog.items()):
            retained = [aspect for aspect in aspects if aspect.aspect_id in supported_aspect_ids]
            dropped_aspect_ids.extend(
                aspect.aspect_id
                for aspect in aspects
                if aspect.aspect_id not in supported_aspect_ids
            )
            if not retained:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_aspect_invalid",
                    f"Topic has no supported Aspect after mapping: {topic_id}",
                )
            merged_catalog[topic_id] = retained
        if dropped_aspect_ids:
            usage.append(
                {
                    "stage": "topic_aspect_support_filter",
                    "dropped_aspect_ids": sorted(dropped_aspect_ids),
                    "dropped_aspect_count": len(dropped_aspect_ids),
                }
            )
        all_aspects = sorted(
            (aspect for aspects in merged_catalog.values() for aspect in aspects),
            key=lambda aspect: aspect.aspect_id,
        )
        return all_aspects, supports, published_exclusions, merged_catalog, usage

    async def _extend_aspect_catalog_for_unclassified(
        self,
        *,
        topics: list[ProfileMapTopic],
        aspects_by_topic: dict[str, list[TopicAspect]],
        exclusions: list[_MappedBlockExclusion],
        block_by_id: dict[str, RetrievalBlock],
        profile_aspect_schema: ProfileAspectSchema,
        provider: ChatProvider,
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[dict[str, object]], int]:
        """只对明确标记 missing_aspect 的 Blocks 做一次受控 catalog 扩展。"""

        topic_by_id = {topic.topic_id: topic for topic in topics}
        excluded_by_topic: dict[str, list[_MappedBlockExclusion]] = defaultdict(list)
        for exclusion in exclusions:
            excluded_by_topic[exclusion.topic_id].append(exclusion)
        candidate_ids_by_block: dict[str, set[str]] = defaultdict(set)
        for topic in topics:
            for source in topic.sources:
                for block_id in source.source_block_ids:
                    candidate_ids_by_block[block_id].add(source.candidate_id)

        allowed_kinds = {definition.kind for definition in profile_aspect_schema.definitions}
        schema_importance = {
            definition.kind: definition.importance
            for definition in profile_aspect_schema.definitions
        }
        usage: list[dict[str, object]] = []

        async def extend_batch(
            batch: list[ProfileMapTopic],
        ) -> tuple[list[TopicAspect], dict[str, object], list[dict[str, object]]]:
            allowed_topics: dict[str, set[str]] = {}
            cards = []
            for topic in batch:
                missing_blocks = excluded_by_topic[topic.topic_id]
                cards.append(
                    {
                        "topic_id": topic.topic_id,
                        "title": topic.title,
                        "aliases": topic.aliases,
                        "summaries": topic.summaries,
                        "existing_aspects": [
                            {
                                "aspect_id": aspect.aspect_id,
                                "kind": aspect.kind,
                                "label": aspect.label,
                                "description": aspect.description,
                            }
                            for aspect in aspects_by_topic[topic.topic_id]
                        ],
                        "unclassified_blocks": [
                            {
                                "block_id": exclusion.block_id,
                                "candidate_ids": sorted(candidate_ids_by_block[exclusion.block_id]),
                                "doc_title": block_by_id[exclusion.block_id].doc_title,
                                "section_path": block_by_id[exclusion.block_id].section_path,
                                "text": block_by_id[exclusion.block_id].text,
                                "reason": exclusion.reason,
                            }
                            for exclusion in missing_blocks
                        ],
                    }
                )
                allowed_topics[topic.topic_id] = set().union(
                    *(candidate_ids_by_block[exclusion.block_id] for exclusion in missing_blocks)
                )
            async with semaphore:
                payload, batch_usage, diagnostics = await _complete_structured_json(
                    provider=provider,
                    messages=[
                        ChatMessage(role="system", content=ASPECT_EXTENSION_PROMPT),
                        ChatMessage(
                            role="user",
                            content=json_dumps(
                                {
                                    "profile_aspect_schema": (
                                        profile_aspect_schema.model_dump(mode="json")
                                    ),
                                    "topics": cards,
                                }
                            ),
                        ),
                    ],
                    response_type=_AspectExtensionPayload,
                    stage="topic_aspect_extension",
                    invalid_code="knowledge_map_aspect_extension_invalid",
                    first_max_output_tokens=ASPECT_CATALOG_MAX_OUTPUT_TOKENS,
                    retry_max_output_tokens=GROUPING_RETRY_MAX_OUTPUT_TOKENS,
                    validator=lambda value: _validate_aspect_extension(
                        value,
                        allowed_topics=allowed_topics,
                        allowed_kinds=allowed_kinds,
                    ),
                )
            returned_topic_ids = {topic.topic_id for topic in payload.topics}
            omitted_topic_ids = sorted(set(allowed_topics) - returned_topic_ids)
            payload.topics.extend(
                _AspectExtensionTopicDraft(topic_id=topic_id, aspects=[])
                for topic_id in omitted_topic_ids
            )
            compiled: list[TopicAspect] = []
            for topic_result in payload.topics:
                topic_revision_id = topic_by_id[topic_result.topic_id].revision_id
                for draft in topic_result.aspects:
                    aspect_id = (
                        "ta_"
                        + stable_hash(
                            {
                                "topic_revision_id": topic_revision_id,
                                "kind": normalize_name(draft.kind),
                                "label": normalize_name(draft.label),
                            }
                        )[:16]
                    )
                    compiled.append(
                        TopicAspect(
                            aspect_id=aspect_id,
                            topic_id=topic_result.topic_id,
                            topic_revision_id=topic_revision_id,
                            kind=draft.kind,
                            label=draft.label,
                            description=draft.description,
                            importance=round(
                                0.6 * draft.importance + 0.4 * schema_importance[draft.kind],
                                6,
                            ),
                            source_candidate_ids=ordered_unique(draft.source_candidate_ids),
                        )
                    )
            return (
                compiled,
                {
                    **batch_usage,
                    "reviewed_topics": len(batch),
                    "reviewed_blocks": sum(
                        len(excluded_by_topic[topic.topic_id]) for topic in batch
                    ),
                    "omitted_review_topics": omitted_topic_ids,
                    "proposed_aspects": len(compiled),
                },
                diagnostics,
            )

        batches = _topic_batches_by_block_budget(
            topics,
            max_topics=ASPECT_MAPPING_TOPICS_PER_BATCH,
            max_blocks=ASPECT_MAPPING_MAX_BLOCKS,
            block_count=lambda topic: len(excluded_by_topic[topic.topic_id]),
        )
        results = await asyncio.gather(*(extend_batch(batch) for batch in batches))
        added_count = 0
        for proposed, batch_usage, diagnostics in results:
            usage.append(batch_usage)
            usage.extend(diagnostics)
            for aspect in proposed:
                current = aspects_by_topic[aspect.topic_id]
                key = (aspect.kind, normalize_name(aspect.label))
                existing = next(
                    (
                        candidate
                        for candidate in current
                        if (candidate.kind, normalize_name(candidate.label)) == key
                    ),
                    None,
                )
                if existing is None:
                    current.append(aspect)
                    added_count += 1
                else:
                    existing_index = current.index(existing)
                    current[existing_index] = existing.model_copy(
                        update={
                            "importance": max(existing.importance, aspect.importance),
                            "source_candidate_ids": ordered_unique(
                                [
                                    *existing.source_candidate_ids,
                                    *aspect.source_candidate_ids,
                                ]
                            ),
                        }
                    )
        if not topic_by_id:
            return usage, 0
        return usage, added_count

    async def _map_aspect_blocks(
        self,
        *,
        topics: list[ProfileMapTopic],
        aspects_by_topic: dict[str, list[TopicAspect]],
        block_by_id: dict[str, RetrievalBlock],
        provider: ChatProvider,
        semaphore: asyncio.Semaphore,
        block_scope: dict[str, set[str]] | None = None,
        repair_attempted: bool = False,
    ) -> tuple[
        list[BlockAspectSupport],
        list[_MappedBlockExclusion],
        list[dict[str, object]],
    ]:
        """把一批 Topic 的全部 Blocks 映射到已有 Aspect catalog。"""

        topics_by_id = {topic.topic_id: topic for topic in topics}
        topic_cards = []
        allowed_blocks: set[tuple[str, str]] = set()
        allowed_aspects: dict[tuple[str, str], set[str]] = {}
        for topic in topics:
            aspects = aspects_by_topic[topic.topic_id]
            scoped_block_ids = (
                [
                    block_id
                    for block_id in topic.source_block_ids
                    if block_id in block_scope[topic.topic_id]
                ]
                if block_scope is not None
                else topic.source_block_ids
            )
            topic_cards.append(
                {
                    "topic_id": topic.topic_id,
                    "title": topic.title,
                    "summaries": topic.summaries,
                    "aspects": [
                        {
                            "aspect_id": aspect.aspect_id,
                            "kind": aspect.kind,
                            "label": aspect.label,
                            "description": aspect.description,
                            "importance": aspect.importance,
                        }
                        for aspect in aspects
                    ],
                    "blocks": [
                        {
                            "block_id": block_id,
                            "doc_title": block_by_id[block_id].doc_title,
                            "section_path": block_by_id[block_id].section_path,
                            "text": block_by_id[block_id].text,
                        }
                        for block_id in scoped_block_ids
                    ],
                }
            )
            for block_id in scoped_block_ids:
                key = (topic.topic_id, block_id)
                allowed_blocks.add(key)
                allowed_aspects[key] = {aspect.aspect_id for aspect in aspects}

        async with semaphore:
            resolved_exclusions: set[tuple[str, str]] = set()
            out_of_scope_references: set[str] = set()
            missing_blocks: set[tuple[str, str]] = set()

            def validate_mapping(value: _AspectMappingPayload) -> None:
                out_of_scope_references.update(
                    _drop_out_of_scope_aspect_mapping_references(
                        value,
                        allowed_block_ids=allowed_blocks,
                        allowed_aspect_ids=allowed_aspects,
                    )
                )
                missing_blocks.clear()
                missing_blocks.update(
                    _validate_aspect_mapping(
                        value,
                        allowed_block_ids=allowed_blocks,
                        allowed_aspect_ids=allowed_aspects,
                    )
                )
                resolved_exclusions.update(_drop_redundant_aspect_mapping_exclusions(value))

            payload, usage, diagnostics = await _complete_structured_json(
                provider=provider,
                messages=[
                    ChatMessage(role="system", content=ASPECT_MAPPING_PROMPT),
                    ChatMessage(role="user", content=json_dumps({"topics": topic_cards})),
                ],
                response_type=_AspectMappingPayload,
                stage="topic_aspect_mapping",
                invalid_code="knowledge_map_aspect_mapping_invalid",
                first_max_output_tokens=ASPECT_MAPPING_MAX_OUTPUT_TOKENS,
                retry_max_output_tokens=GROUPING_RETRY_MAX_OUTPUT_TOKENS,
                validator=validate_mapping,
            )
        if resolved_exclusions:
            diagnostics.append(
                {
                    "code": "topic_aspect_mapping_overlap_resolved",
                    "message": (
                        "Block-level exclusions were dropped because the same Block "
                        "already had concrete Aspect support."
                    ),
                    "blocks": [
                        f"{topic_id}/{block_id}"
                        for topic_id, block_id in sorted(resolved_exclusions)
                    ],
                }
            )
        if out_of_scope_references:
            diagnostics.append(
                {
                    "code": "topic_aspect_mapping_out_of_scope_references",
                    "message": (
                        "Model references outside the current Topic or batch were dropped; "
                        "every valid input Block still requires support or exclusion."
                    ),
                    "references": sorted(out_of_scope_references),
                }
            )
        supports = [
            BlockAspectSupport(
                support_id=(
                    "bas_"
                    + stable_hash(
                        {
                            "topic_revision_id": topics_by_id[draft.topic_id].revision_id,
                            "block_id": draft.block_id,
                            "aspect_id": draft.aspect_id,
                        }
                    )[:16]
                ),
                block_id=draft.block_id,
                topic_id=draft.topic_id,
                topic_revision_id=topics_by_id[draft.topic_id].revision_id,
                aspect_id=draft.aspect_id,
                support_kind=draft.support_kind,
                support_strength=draft.support_strength,
                reason=draft.reason,
            )
            for draft in payload.supports
        ]
        exclusions = [
            _MappedBlockExclusion(
                block_id=draft.block_id,
                topic_id=draft.topic_id,
                topic_revision_id=topics_by_id[draft.topic_id].revision_id,
                classification=draft.classification,
                reason=draft.reason,
            )
            for draft in payload.exclusions
        ]
        if missing_blocks and not repair_attempted:
            repair_scope: dict[str, set[str]] = defaultdict(set)
            for topic_id, block_id in missing_blocks:
                repair_scope[topic_id].add(block_id)
            diagnostics.append(
                {
                    "code": "topic_aspect_mapping_repair",
                    "message": "Unclassified Blocks were sent through one targeted mapping repair.",
                    "blocks": [
                        f"{topic_id}/{block_id}" for topic_id, block_id in sorted(missing_blocks)
                    ],
                }
            )
            repair_topics = [topic for topic in topics if topic.topic_id in repair_scope]
            repair_supports, repair_exclusions, repair_usage = await self._map_aspect_blocks(
                topics=repair_topics,
                aspects_by_topic=aspects_by_topic,
                block_by_id=block_by_id,
                provider=provider,
                semaphore=semaphore,
                block_scope=dict(repair_scope),
                repair_attempted=True,
            )
            return (
                [*supports, *repair_supports],
                [*exclusions, *repair_exclusions],
                [
                    {
                        **usage,
                        "initial_output_supports": len(supports),
                        "initial_output_exclusions": len(exclusions),
                    },
                    *diagnostics,
                    *repair_usage,
                ],
            )
        if missing_blocks:
            rendered = ", ".join(
                f"{topic_id}/{block_id}" for topic_id, block_id in sorted(missing_blocks)
            )
            raise ProfileKnowledgeMapError(
                "knowledge_map_aspect_mapping_incomplete",
                "targeted Aspect mapping repair still omitted Blocks",
                diagnostics={"missing_blocks": rendered.split(", ")},
            )
        return (
            supports,
            exclusions,
            [
                {
                    **usage,
                    "input_topics": len(topics),
                    "input_blocks": len(allowed_blocks),
                    "output_supports": len(supports),
                    "output_exclusions": len(exclusions),
                },
                *diagnostics,
            ],
        )

    async def _resolve_topic_ledger(
        self,
        *,
        candidate_rows: list[PreviewCandidateRow],
        base_candidate_rows: list[PreviewCandidateRow] | None = None,
        base_topics: list[ProfileMapTopic],
        base_topic_vectors: dict[str, list[float]],
        base_candidate_identity_vectors: dict[str, PersistedIdentityVector] | None = None,
        base_topic_identity_vectors: dict[str, PersistedIdentityVector] | None = None,
        provider: ChatProvider,
        embedding_provider: EmbeddingProvider,
        profile_context: str,
        compile_contract: ProfileMapCompileContract | None = None,
        compile_attempt_id: str | None = None,
    ) -> _ResolvedTopicLedger:
        """以空或已有 Topic Ledger 为基线执行 7.6 WorkSet 身份解析。"""

        identity_parameters = (
            compile_contract.topic_identity_parameters
            if compile_contract is not None
            else TopicIdentityCompileParameters(
                similarity_threshold=TOPIC_IDENTITY_SIMILARITY_THRESHOLD,
                max_target_pairs_per_work_item=TOPIC_IDENTITY_MAX_TARGET_PAIRS,
                max_judge_total_tokens_per_work_item=TOPIC_IDENTITY_MAX_TOTAL_TOKENS,
                max_pairs_per_request=TOPIC_IDENTITY_MAX_PAIRS_PER_REQUEST,
                max_input_tokens_per_request=TOPIC_IDENTITY_MAX_INPUT_TOKENS_PER_REQUEST,
                max_output_tokens_per_request=TOPIC_IDENTITY_MAX_OUTPUT_TOKENS_PER_REQUEST,
                output_tokens_per_pair_reserve=TOPIC_IDENTITY_OUTPUT_TOKENS_PER_PAIR_RESERVE,
            )
        )
        target_rows_by_ref = {_candidate_ref(row): row for row in candidate_rows}
        row_by_ref = {
            **{_candidate_ref(row): row for row in (base_candidate_rows or [])},
            **target_rows_by_ref,
        }
        target_refs = set(target_rows_by_ref)
        base_refs = {
            (source.extraction_run_id, source.candidate_id)
            for topic in base_topics
            for source in topic.sources
        }
        added_refs = sorted(target_refs - base_refs)
        removed_refs = base_refs - target_refs
        missing_base_rows = sorted(
            ref
            for ref in base_refs
            for topic in base_topics
            if any(
                (source.extraction_run_id, source.candidate_id) == ref for source in topic.sources
            )
            and ref not in row_by_ref
        )
        if missing_base_rows:
            rendered = ", ".join(
                f"{run_id}/{candidate_id}" for run_id, candidate_id in missing_base_rows
            )
            raise ProfileKnowledgeMapError(
                "incremental_source_identity_missing",
                "base Topic candidates cannot be loaded for the current compile attempt",
                diagnostics={"missing_candidate_refs": rendered.split(", ")},
            )

        topic_by_id = {topic.topic_id: topic for topic in base_topics}
        topic_vector_by_id: dict[str, list[float]] = {}
        topic_identity_vectors: dict[str, PersistedIdentityVector] = {}
        operations: list[TopicLedgerOperation] = []
        active_index = _ActiveTopicVectorIndex(len(base_topics) + len(added_refs))
        name_index: dict[str, set[str]] = defaultdict(set)

        def record_operation(
            *,
            kind: Literal[
                "create_topic",
                "attach_candidate",
                "detach_candidate",
                "merge_topics",
                "retire_topic",
            ],
            input_refs: list[str],
            output_refs: list[str],
            expected_revisions: list[str],
            payload: dict[str, object],
            reason: str,
            provenance: dict[str, object],
            decision_id: str | None = None,
        ) -> str:
            operation_hash = stable_hash(
                {
                    "kind": kind,
                    "input_refs": input_refs,
                    "output_refs": output_refs,
                    "expected_revisions": expected_revisions,
                    "payload": payload,
                    "reason": reason,
                    "provenance": provenance,
                    "decision_id": decision_id,
                }
            )
            operation_id = f"top_{operation_hash[:16]}"
            operations.append(
                TopicLedgerOperation(
                    operation_id=operation_id,
                    kind=kind,
                    input_refs=input_refs,
                    output_refs=output_refs,
                    expected_revisions=expected_revisions,
                    payload=payload,
                    reason=reason,
                    provenance=provenance,
                    decision_id=decision_id,
                )
            )
            return operation_id

        def remove_name_index(topic: ProfileMapTopic) -> None:
            node_ref = _topic_node_ref(topic)
            for name in [topic.title, *topic.aliases]:
                normalized = normalize_name(name)
                if normalized:
                    name_index[normalized].discard(node_ref)
                    if not name_index[normalized]:
                        name_index.pop(normalized, None)

        def add_name_index(topic: ProfileMapTopic) -> None:
            node_ref = _topic_node_ref(topic)
            for name in [topic.title, *topic.aliases]:
                normalized = normalize_name(name)
                if normalized:
                    name_index[normalized].add(node_ref)

        affected_topic_ids: set[str] = set()
        for topic in base_topics:
            existing_refs = {
                (source.extraction_run_id, source.candidate_id) for source in topic.sources
            }
            removed_topic_refs = existing_refs & removed_refs
            remaining_refs = existing_refs - removed_refs
            revised = (
                _materialize_topic(
                    lineage_id=topic.topic_id,
                    rows=[row_by_ref[ref] for ref in sorted(remaining_refs)],
                )
                if remaining_refs
                else None
            )
            for extraction_run_id, candidate_id in sorted(removed_topic_refs):
                record_operation(
                    kind="detach_candidate",
                    input_refs=[
                        _topic_node_ref(topic),
                        _candidate_node_ref((extraction_run_id, candidate_id)),
                    ],
                    output_refs=[] if revised is None else [_topic_node_ref(revised)],
                    expected_revisions=[topic.revision_id],
                    payload={
                        "candidate_ref": {
                            "extraction_run_id": extraction_run_id,
                            "candidate_id": candidate_id,
                        }
                    },
                    reason="candidate removed from the target run scope",
                    provenance={
                        "delta": "removed",
                        "extraction_run_id": extraction_run_id,
                        "candidate_id": candidate_id,
                    },
                )
            if not remaining_refs:
                topic_by_id.pop(topic.topic_id, None)
                record_operation(
                    kind="retire_topic",
                    input_refs=[_topic_node_ref(topic)],
                    output_refs=[],
                    expected_revisions=[topic.revision_id],
                    payload={"reason": "candidate_membership_empty"},
                    reason="Topic lost every candidate membership",
                    provenance={"delta": "retired_source"},
                )
                continue
            if remaining_refs == {
                (source.extraction_run_id, source.candidate_id) for source in topic.sources
            }:
                identity_text = _topic_identity_text(topic)
                cached_identity = (base_topic_identity_vectors or {}).get(_topic_node_ref(topic))
                vector = (
                    _validated_cached_identity_vector(
                        cached_identity,
                        node_ref=_topic_node_ref(topic),
                        text=identity_text,
                        compile_contract=compile_contract,
                    )
                    if compile_contract is not None
                    else None
                )
                if vector is None and compile_contract is None:
                    vector = base_topic_vectors.get(topic.topic_id)
                if vector is None:
                    embedded = await _embed_all(
                        embedding_provider,
                        [identity_text],
                    )
                    vector = embedded[0]
                normalized_vector = _normalize_vector(vector)
                topic_vector_by_id[topic.topic_id] = normalized_vector
                if compile_contract is not None:
                    topic_identity_vectors[_topic_node_ref(topic)] = _persisted_identity_vector(
                        node_ref=_topic_node_ref(topic),
                        text=identity_text,
                        vector=normalized_vector,
                        compile_contract=compile_contract,
                    )
                active_index.upsert(
                    lineage_id=topic.topic_id,
                    node_ref=_topic_node_ref(topic),
                    vector=normalized_vector,
                )
                add_name_index(topic)
                continue
            assert revised is not None
            topic_by_id[topic.topic_id] = revised
            identity_text = _topic_identity_text(revised)
            vector = (
                await _embed_all(
                    embedding_provider,
                    [identity_text],
                )
            )[0]
            normalized_vector = _normalize_vector(vector)
            topic_vector_by_id[revised.topic_id] = normalized_vector
            if compile_contract is not None:
                topic_identity_vectors[_topic_node_ref(revised)] = _persisted_identity_vector(
                    node_ref=_topic_node_ref(revised),
                    text=identity_text,
                    vector=normalized_vector,
                    compile_contract=compile_contract,
                )
            active_index.upsert(
                lineage_id=revised.topic_id,
                node_ref=_topic_node_ref(revised),
                vector=normalized_vector,
            )
            add_name_index(revised)
            affected_topic_ids.add(revised.topic_id)

        candidates = {ref: _candidate_node(row_by_ref[ref]) for ref in added_refs}
        candidate_vectors: dict[tuple[str, str], list[float]] = {}
        candidate_identity_vectors: dict[str, PersistedIdentityVector] = {}
        if added_refs:
            self._emit_progress(
                "candidate_vectors",
                "开始准备候选身份向量",
                candidates=len(added_refs),
            )
        candidate_vector_started = time.perf_counter()
        candidate_cache_hits = 0
        candidate_embedding_api_calls = 0
        for node_ref, entry in (base_candidate_identity_vectors or {}).items():
            parts = node_ref.removeprefix("candidate:").split(":", 1)
            if len(parts) == 2 and (parts[0], parts[1]) in target_refs:
                candidate_identity_vectors[node_ref] = entry
        if added_refs:
            missing_refs: list[tuple[str, str]] = []
            for ref in added_refs:
                node_ref = candidates[ref].node_ref
                identity_text = _summary_identity_text(candidates[ref].summaries)
                cached = (base_candidate_identity_vectors or {}).get(node_ref)
                vector = (
                    _validated_cached_identity_vector(
                        cached,
                        node_ref=node_ref,
                        text=identity_text,
                        compile_contract=compile_contract,
                    )
                    if compile_contract is not None
                    else None
                )
                if vector is None:
                    missing_refs.append(ref)
                else:
                    candidate_vectors[ref] = vector
                    candidate_cache_hits += 1
                    candidate_identity_vectors[node_ref] = cast(
                        PersistedIdentityVector,
                        cached,
                    )
            if missing_refs:
                candidate_embedding_batches = math.ceil(
                    len(missing_refs) / embedding_provider.batch_size
                )
                candidate_embedding_api_calls = candidate_embedding_batches
                try:
                    embedded_candidates = await _embed_all(
                        embedding_provider,
                        [_summary_identity_text(candidates[ref].summaries) for ref in missing_refs],
                    )
                except Exception as error:
                    raise ProfileKnowledgeMapError(
                        "knowledge_map_candidate_embedding_failed",
                        "candidate identity embedding failed",
                        diagnostics={
                            "candidate_count": len(added_refs),
                            "cache_hit_count": candidate_cache_hits,
                            "cache_miss_count": len(missing_refs),
                            "batch_count": candidate_embedding_batches,
                            "embedding_api_call_count": candidate_embedding_api_calls,
                            "failure_count": 1,
                            "elapsed_seconds": round(
                                time.perf_counter() - candidate_vector_started,
                                6,
                            ),
                        },
                    ) from error
                for ref, vector in zip(
                    missing_refs,
                    embedded_candidates,
                    strict=True,
                ):
                    normalized_vector = _normalize_vector(vector)
                    candidate_vectors[ref] = normalized_vector
                    if compile_contract is not None:
                        node_ref = candidates[ref].node_ref
                        candidate_identity_vectors[node_ref] = _persisted_identity_vector(
                            node_ref=node_ref,
                            text=_summary_identity_text(candidates[ref].summaries),
                            vector=normalized_vector,
                            compile_contract=compile_contract,
                        )
            else:
                candidate_embedding_batches = 0
            candidate_vector_diagnostics: dict[str, object] = {
                "stage": "topic_candidate_vector_cache",
                "candidate_count": len(added_refs),
                "cache_hit_count": candidate_cache_hits,
                "cache_miss_count": len(missing_refs),
                "batch_count": candidate_embedding_batches,
                "embedding_api_call_count": candidate_embedding_api_calls,
                "failure_count": 0,
                "elapsed_seconds": round(
                    time.perf_counter() - candidate_vector_started,
                    6,
                ),
            }
        else:
            candidate_vector_diagnostics = {
                "stage": "topic_candidate_vector_cache",
                "candidate_count": 0,
                "cache_hit_count": 0,
                "cache_miss_count": 0,
                "batch_count": 0,
                "embedding_api_call_count": 0,
                "failure_count": 0,
                "elapsed_seconds": 0.0,
            }
        if added_refs:
            self._emit_progress(
                "candidate_vectors",
                "候选身份向量已准备",
                candidates=len(added_refs),
                cache_hits=candidate_vector_diagnostics["cache_hit_count"],
                embedding_api_calls=candidate_vector_diagnostics["embedding_api_call_count"],
            )

        workset: dict[tuple[str, str], _WorkItem] = {}

        def enqueue(item: _WorkItem) -> None:
            workset[(item.node_type, item.node_ref)] = item

        for topic_id in sorted(affected_topic_ids):
            topic = topic_by_id[topic_id]
            enqueue(
                _WorkItem(
                    node_type="topic",
                    node_ref=_topic_node_ref(topic),
                    enqueued_reason="affected_topic_revision",
                )
            )
        for ref in added_refs:
            enqueue(
                _WorkItem(
                    node_type="candidate",
                    node_ref=candidates[ref].node_ref,
                    enqueued_reason="added_candidate",
                )
            )
        self._emit_progress(
            "topic_ledger",
            "Topic WorkSet 已初始化",
            work_items=len(workset),
            added_candidates=len(added_refs),
            affected_topics=len(affected_topic_ids),
        )

        judge_contract_hash = (
            compile_contract.topic_identity_judge.contract_hash
            if compile_contract is not None
            else _topic_identity_judge_contract_hash(
                provider=provider,
                profile_context=profile_context,
            )
        )
        judge_contract_id = (
            compile_contract.topic_identity_judge.contract_id
            if compile_contract is not None
            else f"tjc_{judge_contract_hash[:16]}"
        )
        usage: list[dict[str, object]] = []
        usage.append(candidate_vector_diagnostics)
        judgements: list[TopicIdentityJudgementAudit] = []
        decisions: list[TopicWorkItemDecision] = []
        topic_pair_cache: dict[str, _TopicPairDecisionCacheEntry] = {}
        topic_pair_cache_evictions = 0
        topic_pair_cache_hits = 0
        topic_pair_cache_misses = 0
        processed_candidates: set[tuple[str, str]] = set()
        accepted_merge_count = 0
        rejected_merge_count = 0
        uncertain_count = 0
        processed_count = 0

        def work_order(item: _WorkItem) -> tuple[object, ...]:
            if item.node_type == "topic":
                _, lineage_id, revision_id = item.node_ref.split(":", 2)
                return (0, lineage_id, revision_id)
            candidate_ref = item.node_ref.removeprefix("candidate:").split(":", 1)
            row = row_by_ref[(candidate_ref[0], candidate_ref[1])]
            return (
                1,
                row.doc_id,
                row.doc_version,
                row.ordinal,
                row.run_id,
                row.candidate_id,
            )

        def enqueue_topic(topic: ProfileMapTopic, *, reason: str) -> None:
            for key, pending in list(workset.items()):
                if pending.node_type != "topic":
                    continue
                _, lineage_id, _revision_id = pending.node_ref.split(":", 2)
                if lineage_id == topic.topic_id:
                    del workset[key]
            enqueue(
                _WorkItem(
                    node_type="topic",
                    node_ref=_topic_node_ref(topic),
                    enqueued_reason=reason,
                )
            )

        def remove_topic_work(lineage_id: str) -> None:
            for key, pending in list(workset.items()):
                if pending.node_type != "topic":
                    continue
                _, pending_lineage_id, _revision_id = pending.node_ref.split(":", 2)
                if pending_lineage_id == lineage_id:
                    del workset[key]

        def topic_rows(topic: ProfileMapTopic) -> list[PreviewCandidateRow]:
            return [
                row_by_ref[(source.extraction_run_id, source.candidate_id)]
                for source in topic.sources
            ]

        def invalidate_topic_pair_cache(previous: list[ProfileMapTopic]) -> None:
            nonlocal topic_pair_cache_evictions
            old_refs = {_topic_node_ref(topic) for topic in previous}
            if not old_refs:
                return
            for key, judgement in list(topic_pair_cache.items()):
                if judgement.left_node_ref in old_refs or judgement.right_node_ref in old_refs:
                    del topic_pair_cache[key]
                    topic_pair_cache_evictions += 1

        async def install_topic(
            *,
            topic: ProfileMapTopic,
            previous: list[ProfileMapTopic],
        ) -> list[float]:
            invalidate_topic_pair_cache(previous)
            for old_topic in previous:
                remove_name_index(old_topic)
                active_index.remove(old_topic.topic_id)
                topic_by_id.pop(old_topic.topic_id, None)
                topic_vector_by_id.pop(old_topic.topic_id, None)
                topic_identity_vectors.pop(_topic_node_ref(old_topic), None)
            identity_text = _topic_identity_text(topic)
            vector = (
                await _embed_all(
                    embedding_provider,
                    [identity_text],
                )
            )[0]
            normalized_vector = _normalize_vector(vector)
            topic_by_id[topic.topic_id] = topic
            topic_vector_by_id[topic.topic_id] = normalized_vector
            if compile_contract is not None:
                topic_identity_vectors[_topic_node_ref(topic)] = _persisted_identity_vector(
                    node_ref=_topic_node_ref(topic),
                    text=identity_text,
                    vector=normalized_vector,
                    compile_contract=compile_contract,
                )
            active_index.upsert(
                lineage_id=topic.topic_id,
                node_ref=_topic_node_ref(topic),
                vector=normalized_vector,
            )
            add_name_index(topic)
            return normalized_vector

        while workset:
            item = min(workset.values(), key=work_order)
            del workset[(item.node_type, item.node_ref)]
            decision_hash = stable_hash(
                {
                    "compile_attempt_id": compile_attempt_id or "attemptless",
                    "node_ref": item.node_ref,
                    "judge_contract_hash": judge_contract_hash,
                }
            )
            decision_id = f"twd_{decision_hash[:16]}"

            candidate: _CandidateNode | None = None
            current_topic: ProfileMapTopic | None = None
            current_vector: list[float]
            if item.node_type == "candidate":
                ref_parts = item.node_ref.removeprefix("candidate:").split(":", 1)
                ref = (ref_parts[0], ref_parts[1])
                if ref in processed_candidates or ref not in candidates:
                    continue
                candidate = candidates[ref]
                current_vector = candidate_vectors[ref]
                current_card = _candidate_card(candidate)
            else:
                _, lineage_id, revision_id = item.node_ref.split(":", 2)
                current_topic = topic_by_id.get(lineage_id)
                if (
                    current_topic is None
                    or current_topic.revision_id != revision_id
                    or lineage_id not in topic_vector_by_id
                ):
                    continue
                current_vector = topic_vector_by_id[lineage_id]
                current_card = _topic_card(current_topic)

            processed_count += 1
            if processed_count == 1 or processed_count % 10 == 0:
                self._emit_progress(
                    "topic_ledger",
                    "正在处理 Topic WorkSet",
                    processed_work_items=processed_count,
                    remaining_work_items=len(workset),
                    active_topics=len(topic_by_id),
                    pending_candidates=len(added_refs) - len(processed_candidates),
                )
            proposals_by_ref: dict[str, _IdentityTargetProposal] = {}
            for node_ref, similarity in active_index.search_at_or_above(
                vector=current_vector,
                threshold=identity_parameters.similarity_threshold,
            ):
                _, target_lineage_id, _target_revision = node_ref.split(":", 2)
                if current_topic is not None and target_lineage_id == current_topic.topic_id:
                    continue
                target_topic = topic_by_id[target_lineage_id]
                proposals_by_ref[node_ref] = _IdentityTargetProposal(
                    topic=target_topic,
                    target_node_ref=node_ref,
                    embedding_similarity=similarity,
                    has_exact_name_match=False,
                )

            current_names = [
                str(current_card["title"]),
                *cast(list[str], current_card["aliases"]),
            ]
            for name in current_names:
                normalized = normalize_name(name)
                for node_ref in name_index.get(normalized, set()):
                    _, target_lineage_id, _target_revision = node_ref.split(":", 2)
                    if current_topic is not None and target_lineage_id == current_topic.topic_id:
                        continue
                    target_topic = topic_by_id[target_lineage_id]
                    existing = proposals_by_ref.get(node_ref)
                    if existing is None:
                        similarity = float(
                            np.dot(
                                np.asarray(current_vector, dtype=np.float32),
                                np.asarray(
                                    topic_vector_by_id[target_lineage_id],
                                    dtype=np.float32,
                                ),
                            )
                        )
                        proposals_by_ref[node_ref] = _IdentityTargetProposal(
                            topic=target_topic,
                            target_node_ref=node_ref,
                            embedding_similarity=similarity,
                            has_exact_name_match=True,
                        )
                    else:
                        proposals_by_ref[node_ref] = _IdentityTargetProposal(
                            topic=existing.topic,
                            target_node_ref=existing.target_node_ref,
                            embedding_similarity=existing.embedding_similarity,
                            has_exact_name_match=True,
                        )
            proposals = sorted(
                proposals_by_ref.values(),
                key=lambda proposal: (
                    not proposal.has_exact_name_match,
                    -proposal.embedding_similarity,
                    proposal.target_node_ref,
                ),
            )

            judgements_by_pair: dict[tuple[str, str], _TopicIdentityJudgement] = {}
            judgement_id_by_pair: dict[tuple[str, str], str] = {}
            pending_targets: list[_IdentityTargetProposal] = []
            for proposal in proposals:
                pair = (item.node_ref, proposal.target_node_ref)
                if item.node_type == "topic":
                    pair_key = _topic_pair_key(
                        item.node_ref,
                        proposal.target_node_ref,
                        judge_contract_hash,
                    )
                    cached = topic_pair_cache.get(pair_key)
                    cached_refs_match = cached is not None and (
                        (
                            cached.left_node_ref == item.node_ref
                            and cached.right_node_ref == proposal.target_node_ref
                        )
                        or (
                            cached.left_node_ref == proposal.target_node_ref
                            and cached.right_node_ref == item.node_ref
                        )
                    )
                    if (
                        cached is not None
                        and cached.pair_key == pair_key
                        and cached_refs_match
                        and cached.judge_contract_hash == judge_contract_hash
                    ):
                        judgements_by_pair[pair] = _TopicIdentityJudgement(
                            left_node_ref=pair[0],
                            right_node_ref=pair[1],
                            relation=cached.relation,
                            reason="复用当前 compile attempt 的已校验判断。",
                        )
                        judgement_id_by_pair[pair] = cached.judgement_id
                        topic_pair_cache_hits += 1
                        continue
                    topic_pair_cache_misses += 1
                pending_targets.append(proposal)

            if len(proposals) > identity_parameters.max_target_pairs_per_work_item:
                raise ProfileKnowledgeMapError(
                    "identity_target_budget_exceeded",
                    "Topic identity target count exceeds the per-WorkItem guard",
                    diagnostics={
                        "work_item_node_ref": item.node_ref,
                        "target_count": len(proposals),
                        "max_target_pairs": (identity_parameters.max_target_pairs_per_work_item),
                    },
                )

            prompt = _topic_identity_prompt(profile_context)
            request_pairs: list[dict[str, object]] = []
            pair_estimated_tokens: dict[tuple[str, str], int] = {}
            for proposal in pending_targets:
                pair_payload = {
                    "left_node_ref": item.node_ref,
                    "right_node_ref": proposal.target_node_ref,
                    "left": current_card,
                    "right": _topic_card(proposal.topic),
                }
                estimated = estimate_tokens(json_dumps(pair_payload))
                pair_key_tuple = (item.node_ref, proposal.target_node_ref)
                pair_estimated_tokens[pair_key_tuple] = estimated
                request_pairs.append(pair_payload)

            request_batches: list[list[dict[str, object]]] = []
            request_batch_input_tokens: list[int] = []
            current_batch: list[dict[str, object]] = []
            current_input_tokens = estimate_tokens(
                json_dumps(
                    {
                        "schema_version": "topic-identity-input-v1",
                        "profile_context": profile_context,
                        "pairs": [],
                    }
                )
            ) + estimate_tokens(prompt)
            for pair_payload in request_pairs:
                pair_tokens = pair_estimated_tokens[
                    (
                        cast(str, pair_payload["left_node_ref"]),
                        cast(str, pair_payload["right_node_ref"]),
                    )
                ]
                if (
                    current_input_tokens + pair_tokens
                    > identity_parameters.max_input_tokens_per_request
                ) and not current_batch:
                    raise ProfileKnowledgeMapError(
                        "identity_pair_input_too_large",
                        "a Topic identity pair exceeds the request input budget",
                        diagnostics={"work_item_node_ref": item.node_ref},
                    )
                output_tokens = (
                    len(current_batch) + 1
                ) * identity_parameters.output_tokens_per_pair_reserve
                if current_batch and (
                    len(current_batch) >= identity_parameters.max_pairs_per_request
                    or current_input_tokens + pair_tokens
                    > identity_parameters.max_input_tokens_per_request
                    or output_tokens > identity_parameters.max_output_tokens_per_request
                ):
                    request_batch_input_tokens.append(current_input_tokens)
                    request_batches.append(current_batch)
                    current_batch = []
                    current_input_tokens = estimate_tokens(
                        json_dumps(
                            {
                                "schema_version": "topic-identity-input-v1",
                                "profile_context": profile_context,
                                "pairs": [],
                            }
                        )
                    ) + estimate_tokens(prompt)
                    output_tokens = identity_parameters.output_tokens_per_pair_reserve
                if output_tokens > identity_parameters.max_output_tokens_per_request:
                    raise ProfileKnowledgeMapError(
                        "identity_target_budget_exceeded",
                        "Topic identity output reserve exceeds the request budget",
                    )
                current_batch.append(pair_payload)
                current_input_tokens += pair_tokens
            if current_batch:
                request_batch_input_tokens.append(current_input_tokens)
                request_batches.append(current_batch)
            total_estimated_tokens = sum(
                request_input_tokens
                + len(batch) * identity_parameters.output_tokens_per_pair_reserve
                for batch, request_input_tokens in zip(
                    request_batches,
                    request_batch_input_tokens,
                    strict=True,
                )
            )
            if total_estimated_tokens > identity_parameters.max_judge_total_tokens_per_work_item:
                raise ProfileKnowledgeMapError(
                    "identity_target_budget_exceeded",
                    "Topic identity token budget exceeds the per-WorkItem guard",
                    diagnostics={
                        "work_item_node_ref": item.node_ref,
                        "estimated_tokens": total_estimated_tokens,
                        "max_total_tokens": (
                            identity_parameters.max_judge_total_tokens_per_work_item
                        ),
                    },
                )

            semaphore = asyncio.Semaphore(MAP_BUILD_MAX_PARALLEL_CHAT_CALLS)

            async def judge_batch(
                pairs: list[dict[str, object]],
                *,
                current_prompt: str = prompt,
                current_item_ref: str = item.node_ref,
                build_semaphore: asyncio.Semaphore = semaphore,
            ) -> tuple[
                _TopicIdentityJudgementsPayload,
                dict[str, object],
                list[dict[str, object]],
            ]:
                expected_pairs = {
                    (
                        cast(str, pair["left_node_ref"]),
                        cast(str, pair["right_node_ref"]),
                    )
                    for pair in pairs
                }
                async with build_semaphore:
                    payload, batch_usage, diagnostics = await _complete_structured_json(
                        provider=provider,
                        messages=[
                            ChatMessage(role="system", content=current_prompt),
                            ChatMessage(
                                role="user",
                                content=json_dumps(
                                    {
                                        "schema_version": "topic-identity-input-v1",
                                        "profile_context": profile_context,
                                        "pairs": pairs,
                                    }
                                ),
                            ),
                        ],
                        response_type=_TopicIdentityJudgementsPayload,
                        stage="topic_identity",
                        invalid_code="knowledge_map_topic_identity_invalid",
                        first_max_output_tokens=min(
                            identity_parameters.max_output_tokens_per_request,
                            max(
                                identity_parameters.output_tokens_per_pair_reserve,
                                len(pairs) * identity_parameters.output_tokens_per_pair_reserve,
                            ),
                        ),
                        retry_max_output_tokens=(identity_parameters.max_output_tokens_per_request),
                        validator=lambda result, expected=expected_pairs: (
                            _validate_topic_identity_judgements(
                                result,
                                expected_pairs=expected,
                            )
                        ),
                    )
                return (
                    payload,
                    {
                        **batch_usage,
                        "input_pair_count": len(pairs),
                        "work_item_node_ref": current_item_ref,
                    },
                    diagnostics,
                )

            batch_results = await asyncio.gather(*(judge_batch(batch) for batch in request_batches))
            trace_ids: list[str] = []
            for payload, batch_usage, diagnostics in batch_results:
                request_ids = [
                    str(request_id)
                    for request_id in cast(list[object], batch_usage["request_ids"])
                    if str(request_id)
                ]
                trace_ids.extend(request_ids)
                usage.append(batch_usage)
                usage.extend(diagnostics)
                request_trace_id = request_ids[-1] if request_ids else ""
                for judgement in payload.judgements:
                    pair = (judgement.left_node_ref, judgement.right_node_ref)
                    if item.node_type == "topic":
                        pair_key = _topic_pair_key(pair[0], pair[1], judge_contract_hash)
                    else:
                        pair_key = stable_hash(
                            {
                                "kind": "candidate-topic-identity-pair-v1",
                                "left": pair[0],
                                "right": pair[1],
                                "judge_contract_hash": judge_contract_hash,
                            }
                        )
                    judgement_hash = stable_hash(
                        {
                            "compile_attempt_id": compile_attempt_id or "attemptless",
                            "pair_key": pair_key,
                            "relation": judgement.relation,
                        }
                    )
                    judgement_id = f"tij_{judgement_hash[:16]}"
                    audit = TopicIdentityJudgementAudit(
                        judgement_id=judgement_id,
                        pair_key=pair_key,
                        left_node_ref=judgement.left_node_ref,
                        right_node_ref=judgement.right_node_ref,
                        relation=judgement.relation,
                        reason=judgement.reason,
                        judge_contract_id=judge_contract_id,
                        judge_contract_hash=judge_contract_hash,
                        request_trace_id=request_trace_id,
                        estimated_input_tokens=pair_estimated_tokens[pair],
                        estimated_output_tokens=estimate_tokens(judgement.reason) + 8,
                    )
                    judgements.append(audit)
                    if item.node_type == "topic":
                        topic_pair_cache[pair_key] = _TopicPairDecisionCacheEntry(
                            pair_key=pair_key,
                            left_node_ref=audit.left_node_ref,
                            right_node_ref=audit.right_node_ref,
                            relation=audit.relation,
                            judge_contract_hash=audit.judge_contract_hash,
                            judgement_id=audit.judgement_id,
                        )
                    judgements_by_pair[pair] = judgement
                    judgement_id_by_pair[pair] = judgement_id

            same_targets = [
                proposal
                for proposal in proposals
                if judgements_by_pair[(item.node_ref, proposal.target_node_ref)].relation == "same"
            ]
            same_target_refs = [proposal.target_node_ref for proposal in same_targets]
            selected = (
                sorted(
                    same_targets,
                    key=lambda proposal: (
                        -proposal.embedding_similarity,
                        proposal.target_node_ref,
                    ),
                )[0]
                if same_targets
                else None
            )
            different_count = sum(
                1
                for proposal in proposals
                if judgements_by_pair[(item.node_ref, proposal.target_node_ref)].relation
                == "different"
            )
            item_uncertain_count = sum(
                1
                for proposal in proposals
                if judgements_by_pair[(item.node_ref, proposal.target_node_ref)].relation
                == "uncertain"
            )
            uncertain_count += item_uncertain_count
            if current_topic is not None and different_count:
                rejected_merge_count += different_count

            operation_ids: list[str] = []
            if candidate is not None:
                processed_candidates.add((candidate.extraction_run_id, candidate.candidate_id))
                if selected is None:
                    lineage_id = _new_topic_lineage_id(
                        (candidate.extraction_run_id, candidate.candidate_id)
                    )
                    topic = _materialize_topic(lineage_id=lineage_id, rows=[candidate.row])
                    await install_topic(topic=topic, previous=[])
                    outcome: Literal["attach", "create", "merge", "keep"] = "create"
                    result_topic = topic
                    operation_ids.append(
                        record_operation(
                            kind="create_topic",
                            input_refs=[candidate.node_ref],
                            output_refs=[_topic_node_ref(topic)],
                            expected_revisions=[],
                            payload={
                                "initial_candidate_refs": [
                                    {
                                        "extraction_run_id": candidate.extraction_run_id,
                                        "candidate_id": candidate.candidate_id,
                                    }
                                ]
                            },
                            reason="all target judgements completed without same",
                            provenance={
                                "decision_id": decision_id,
                                "judgement_ids": [
                                    judgement_id_by_pair[(item.node_ref, proposal.target_node_ref)]
                                    for proposal in proposals
                                ],
                            },
                            decision_id=decision_id,
                        )
                    )
                else:
                    target_topic = selected.topic
                    revised = _materialize_topic(
                        lineage_id=target_topic.topic_id,
                        rows=[*topic_rows(target_topic), candidate.row],
                    )
                    await install_topic(topic=revised, previous=[target_topic])
                    enqueue_topic(revised, reason="candidate_attached")
                    outcome = "attach"
                    result_topic = revised
                    operation_ids.append(
                        record_operation(
                            kind="attach_candidate",
                            input_refs=[candidate.node_ref, _topic_node_ref(target_topic)],
                            output_refs=[_topic_node_ref(revised)],
                            expected_revisions=[target_topic.revision_id],
                            payload={
                                "candidate_ref": {
                                    "extraction_run_id": candidate.extraction_run_id,
                                    "candidate_id": candidate.candidate_id,
                                }
                            },
                            reason="candidate and Topic revision judged same",
                            provenance={
                                "decision_id": decision_id,
                                "selected_same_target_ref": selected.target_node_ref,
                                "judgement_ids": [
                                    judgement_id_by_pair[(item.node_ref, proposal.target_node_ref)]
                                    for proposal in proposals
                                ],
                            },
                            decision_id=decision_id,
                        )
                    )
            else:
                assert current_topic is not None
                if selected is None:
                    outcome = "keep"
                    result_topic = current_topic
                else:
                    target_topic = selected.topic
                    lineage_id = _merged_topic_lineage_id([current_topic, target_topic])
                    merged_rows = [*topic_rows(current_topic), *topic_rows(target_topic)]
                    merged = _materialize_topic(lineage_id=lineage_id, rows=merged_rows)
                    remove_topic_work(current_topic.topic_id)
                    remove_topic_work(target_topic.topic_id)
                    await install_topic(topic=merged, previous=[current_topic, target_topic])
                    enqueue_topic(merged, reason="topics_merged")
                    outcome = "merge"
                    result_topic = merged
                    accepted_merge_count += 1
                    operation_ids.append(
                        record_operation(
                            kind="merge_topics",
                            input_refs=sorted(
                                [
                                    _topic_node_ref(current_topic),
                                    _topic_node_ref(target_topic),
                                ]
                            ),
                            output_refs=[_topic_node_ref(merged)],
                            expected_revisions=sorted(
                                [current_topic.revision_id, target_topic.revision_id]
                            ),
                            payload={
                                "input_topic_lineage_ids": sorted(
                                    [current_topic.topic_id, target_topic.topic_id]
                                ),
                                "output_topic_lineage_id": merged.topic_id,
                            },
                            reason="two Topic revisions judged same",
                            provenance={
                                "decision_id": decision_id,
                                "selected_same_target_ref": selected.target_node_ref,
                                "judgement_ids": [
                                    judgement_id_by_pair[(item.node_ref, proposal.target_node_ref)]
                                    for proposal in proposals
                                ],
                            },
                            decision_id=decision_id,
                        )
                    )

            decisions.append(
                TopicWorkItemDecision(
                    decision_id=decision_id,
                    work_item_node_ref=item.node_ref,
                    judge_contract_id=judge_contract_id,
                    judge_contract_hash=judge_contract_hash,
                    target_count=len(proposals),
                    same_target_refs=same_target_refs,
                    selected_same_target_ref=(
                        None if selected is None else selected.target_node_ref
                    ),
                    different_count=different_count,
                    uncertain_count=item_uncertain_count,
                    outcome=outcome,
                    result_topic_ref=_topic_node_ref(result_topic),
                    operation_ids=operation_ids,
                    judgement_ids=[
                        judgement_id_by_pair[(item.node_ref, proposal.target_node_ref)]
                        for proposal in proposals
                    ],
                    trace_ids=ordered_unique(trace_ids),
                )
            )
            if candidate is not None:
                candidate_vectors.pop(
                    (candidate.extraction_run_id, candidate.candidate_id),
                    None,
                )

        final_topics = sorted(topic_by_id.values(), key=lambda topic: topic.topic_id)
        final_candidate_refs = {
            (source.extraction_run_id, source.candidate_id)
            for topic in final_topics
            for source in topic.sources
        }
        if final_candidate_refs != target_refs:
            missing = sorted(target_refs - final_candidate_refs)
            extra = sorted(final_candidate_refs - target_refs)
            raise ProfileKnowledgeMapError(
                "knowledge_map_candidate_coverage_invalid",
                "final Topic Ledger does not cover the target CandidateRefs exactly",
                diagnostics={
                    "missing_candidate_refs": [
                        f"{run_id}/{candidate_id}" for run_id, candidate_id in missing
                    ],
                    "unexpected_candidate_refs": [
                        f"{run_id}/{candidate_id}" for run_id, candidate_id in extra
                    ],
                },
            )
        final_vectors = {
            topic.topic_id: topic_vector_by_id[topic.topic_id] for topic in final_topics
        }
        final_topic_node_refs = {_topic_node_ref(topic) for topic in final_topics}
        usage.append(
            {
                "stage": "topic_work_set_convergence",
                "processed_work_item_count": processed_count,
                "topic_pair_cache_evictions": topic_pair_cache_evictions,
                "topic_pair_cache_hits": topic_pair_cache_hits,
                "topic_pair_cache_misses": topic_pair_cache_misses,
            }
        )
        self._emit_progress(
            "topic_ledger",
            "Topic WorkSet 已收敛",
            processed_work_items=processed_count,
            topics=len(final_topics),
            merges=accepted_merge_count,
        )
        return _ResolvedTopicLedger(
            topics=final_topics,
            topic_vectors=final_vectors,
            candidate_identity_vectors=candidate_identity_vectors,
            topic_identity_vectors={
                node_ref: entry
                for node_ref, entry in topic_identity_vectors.items()
                if node_ref in final_topic_node_refs
            },
            usage=usage,
            convergence=MapConvergenceReport(
                status=("converged_with_uncertain" if uncertain_count else "converged"),
                passes_used=1 if processed_count else 0,
                accepted_merge_count=accepted_merge_count,
                rejected_merge_count=rejected_merge_count,
                uncertain_pair_count=uncertain_count,
                pending_candidate_count=0,
                last_pass_new_merge_count=accepted_merge_count,
                termination_reason="work_set_empty",
            ),
            judgements=sorted(judgements, key=lambda judgement: judgement.judgement_id),
            decisions=sorted(decisions, key=lambda decision: decision.decision_id),
            operations=sorted(operations, key=lambda operation: operation.operation_id),
        )

    async def _build_groups(
        self,
        *,
        topics: list[ProfileMapTopic],
        topic_vectors: list[list[float]],
        topic_aspects_by_topic: dict[str, list[TopicAspect]],
        provider: ChatProvider,
        embedding_provider: EmbeddingProvider,
        profile_context: str,
    ) -> tuple[
        list[ProfileMapGroup],
        list[dict[str, object]],
        list[list[float]],
        MapConvergenceReport,
    ]:
        topic_by_id = {topic.topic_id: topic for topic in topics}
        usage: list[dict[str, object]] = []
        topic_batches = _semantic_batches(
            topics,
            topic_vectors,
            GROUP_INPUT_MAX_ITEMS,
            key=lambda topic: topic.topic_id,
        )
        semaphore = asyncio.Semaphore(MAP_BUILD_MAX_PARALLEL_CHAT_CALLS)

        async def group_topic_batch(
            batch: list[ProfileMapTopic],
        ) -> tuple[list[ProfileMapGroup], list[dict[str, object]]]:
            cards = [
                {
                    "topic_id": topic.topic_id,
                    "title": topic.title,
                    "aliases": topic.aliases,
                    "summaries": topic.summaries,
                    "source_doc_ids": topic.source_doc_ids,
                    "evidence_count": len(topic.source_block_ids),
                    "aspect_kinds": ordered_unique(
                        aspect.kind for aspect in topic_aspects_by_topic.get(topic.topic_id, [])
                    ),
                    "aspect_labels": [
                        aspect.label
                        for aspect in topic_aspects_by_topic.get(topic.topic_id, [])[:5]
                    ],
                }
                for topic in batch
            ]
            async with semaphore:
                payload, batch_usage = await self._group_items(
                    cards=cards,
                    id_field="topic_id",
                    mode="topic",
                    provider=provider,
                    profile_context=profile_context,
                )
            created = self._groups_from_topic_drafts(
                groups=payload.groups,
                topic_by_id=topic_by_id,
            )
            return (
                [
                    _derive_group_aspect_fields(
                        group,
                        topic_aspects_by_topic=topic_aspects_by_topic,
                    )
                    for group in created
                ],
                batch_usage,
            )

        current_groups: list[ProfileMapGroup] = []
        initial_results = await asyncio.gather(
            *(group_topic_batch(batch) for batch in topic_batches)
        )
        for groups, batch_usage in initial_results:
            current_groups.extend(groups)
            usage.extend(batch_usage)
        initial_group_count = len(current_groups)
        accepted_merge_count = 0
        rejected_merge_count = 0
        uncertain_pair_count = 0
        last_pass_new_merge_count = 0
        passes_used = 0
        termination_reason = "no global candidate edges"
        group_vectors_by_id: dict[str, list[float]] = {}
        reviewed_edge_keys: set[tuple[str, str]] = set()
        for merge_pass in range(GROUP_MERGE_MAX_PASSES):
            passes_used = merge_pass + 1
            if len(current_groups) <= 1:
                termination_reason = "single final group"
                break
            missing_vectors = [
                group for group in current_groups if group.group_id not in group_vectors_by_id
            ]
            if missing_vectors:
                embedded = await _embed_all(
                    embedding_provider,
                    [_group_search_text(group) for group in missing_vectors],
                )
                for group, vector in zip(missing_vectors, embedded, strict=True):
                    group_vectors_by_id[group.group_id] = vector
            group_vectors = [group_vectors_by_id[group.group_id] for group in current_groups]
            semantic_edges = _global_candidate_edges(
                current_groups,
                group_vectors,
                item_id=lambda group: group.group_id,
                score_floor=GLOBAL_NEIGHBOR_SCORE_FLOOR,
                top_k=GLOBAL_NEIGHBOR_TOP_K,
            )
            auto_merged_groups, auto_merge_stats = self._auto_merge_high_overlap_groups(
                groups=current_groups,
                vectors=group_vectors_by_id,
                edges=semantic_edges,
            )
            if auto_merge_stats["groups_removed"] > 0:
                current_groups = [
                    _derive_group_aspect_fields(
                        group,
                        topic_aspects_by_topic=topic_aspects_by_topic,
                    )
                    for group in auto_merged_groups
                ]
                last_pass_new_merge_count = auto_merge_stats["groups_removed"]
                accepted_merge_count += last_pass_new_merge_count
                usage.append(
                    {
                        "stage": "group_auto_merge",
                        "merge_pass": merge_pass + 1,
                        "input_groups": len(current_groups) + auto_merge_stats["groups_removed"],
                        "output_groups": len(current_groups),
                        **auto_merge_stats,
                    }
                )
                continue
            edges = _group_candidate_edges(groups=current_groups, edges=semantic_edges)
            if not edges:
                termination_reason = "no global candidate edges"
                break
            unreviewed_edges = [
                edge for edge in edges if (edge[0], edge[1]) not in reviewed_edge_keys
            ]
            reviewed_edge_keys.update((left, right) for left, right, _score in unreviewed_edges)
            if not unreviewed_edges:
                termination_reason = "all global candidate edges reviewed"
                last_pass_new_merge_count = 0
                break
            batches = _neighbor_review_batches(
                ids=[group.group_id for group in current_groups],
                edges=unreviewed_edges,
                max_size=GROUP_MERGE_BATCH_SIZE,
            )

            async def merge_group_batch(
                batch_ids: list[str],
                *,
                build_semaphore: asyncio.Semaphore = semaphore,
                current_pass: int = merge_pass,
                groups_snapshot: list[ProfileMapGroup] = current_groups,
            ) -> tuple[_GroupMergePayload, list[dict[str, object]]]:
                by_group_id = {group.group_id: group for group in groups_snapshot}
                batch = [by_group_id[group_id] for group_id in batch_ids]
                cards = [
                    {
                        "group_id": group.group_id,
                        "label": group.label,
                        "aliases": group.aliases,
                        "summary": group.summary,
                        "retrieval_questions": group.retrieval_questions,
                        "source_doc_ids": group.source_doc_ids,
                        "topic_titles": [
                            topic_by_id[topic_id].title for topic_id in group.child_topic_ids[:8]
                        ],
                    }
                    for group in batch
                ]
                async with build_semaphore:
                    payload, batch_usage = await self._merge_group_items(
                        cards=cards,
                        provider=provider,
                        profile_context=profile_context,
                    )
                return (
                    payload,
                    [{**batch_usage[0], "merge_pass": current_pass + 1}, *batch_usage[1:]],
                )

            batch_results = await asyncio.gather(*(merge_group_batch(batch) for batch in batches))
            all_actions: list[_GroupMergeAction] = []
            pass_uncertain_pair_count = 0
            for payload, batch_usage in batch_results:
                usage.extend(batch_usage)
                pass_uncertain_pair_count += len(payload.uncertain_pairs)
                uncertain_pair_count += len(payload.uncertain_pairs)
                all_actions.extend(payload.actions)
            next_groups, merge_stats = self._merge_groups_by_actions(
                all_actions,
                current_groups,
            )
            next_groups = [
                _derive_group_aspect_fields(
                    group,
                    topic_aspects_by_topic=topic_aspects_by_topic,
                )
                for group in next_groups
            ]
            membership_before = sum(len(group.child_topic_ids) for group in current_groups)
            membership_after = sum(len(group.child_topic_ids) for group in next_groups)
            usage.append(
                {
                    "stage": "group_convergence_pass",
                    "merge_pass": merge_pass + 1,
                    "input_groups": len(current_groups),
                    "output_groups": len(next_groups),
                    "membership_before": membership_before,
                    "membership_after": membership_after,
                    "uncertain_pairs": pass_uncertain_pair_count,
                    **merge_stats,
                }
            )
            last_pass_new_merge_count = len(current_groups) - len(next_groups)
            accepted_merge_count += max(0, last_pass_new_merge_count)
            rejected_merge_count += merge_stats["kept_group_count"]
            current_groups = sorted(next_groups, key=lambda group: group.group_id)
            if last_pass_new_merge_count <= 0:
                termination_reason = "fixed point reached"
                break
        missing_final_vectors = [
            group for group in current_groups if group.group_id not in group_vectors_by_id
        ]
        if missing_final_vectors:
            embedded = await _embed_all(
                embedding_provider,
                [_group_search_text(group) for group in missing_final_vectors],
            )
            for group, vector in zip(missing_final_vectors, embedded, strict=True):
                group_vectors_by_id[group.group_id] = vector
        final_vectors = [group_vectors_by_id[group.group_id] for group in current_groups]
        usage.append(
            {
                "stage": "group_overlap_audit",
                "initial_groups": initial_group_count,
                "final_groups": len(current_groups),
                **_group_overlap_audit(
                    groups=current_groups,
                    vectors=final_vectors,
                ),
            }
        )
        if last_pass_new_merge_count > 0 and passes_used >= GROUP_MERGE_MAX_PASSES:
            status: Literal[
                "converged",
                "converged_with_uncertain",
                "ready_partial",
                "failed",
            ] = "ready_partial"
            pending_candidate_count = max(1, last_pass_new_merge_count)
        elif uncertain_pair_count > 0:
            status = "converged_with_uncertain"
            pending_candidate_count = 0
        else:
            status = "converged"
            pending_candidate_count = 0
        return (
            current_groups,
            usage,
            final_vectors,
            MapConvergenceReport(
                status=status,
                passes_used=passes_used,
                accepted_merge_count=accepted_merge_count,
                rejected_merge_count=rejected_merge_count,
                uncertain_pair_count=uncertain_pair_count,
                pending_candidate_count=pending_candidate_count,
                last_pass_new_merge_count=max(0, last_pass_new_merge_count),
                termination_reason=termination_reason,
            ),
        )

    async def _group_items(
        self,
        *,
        cards: list[dict[str, object]],
        id_field: str,
        mode: Literal["topic", "global"],
        provider: ChatProvider,
        profile_context: str,
    ) -> tuple[_GroupingPayload, list[dict[str, object]]]:
        if len(cards) > GROUP_INPUT_MAX_ITEMS:
            raise ProfileKnowledgeMapError(
                "knowledge_map_group_limit_exceeded",
                f"{mode} grouping input exceeds {GROUP_INPUT_MAX_ITEMS} items",
                diagnostics={"mode": mode, "item_count": len(cards)},
            )
        messages = [
            ChatMessage(role="system", content=_grouping_prompt(mode, profile_context)),
            ChatMessage(
                role="user",
                content=json_dumps({"items": cards, "id_field": id_field}),
            ),
        ]
        payload, usage, diagnostics = await _complete_structured_json(
            provider=provider,
            messages=messages,
            response_type=_GroupingPayload,
            stage=f"{mode}_grouping",
            invalid_code="knowledge_map_grouping_invalid",
            first_max_output_tokens=GROUPING_MAX_OUTPUT_TOKENS,
            retry_max_output_tokens=GROUPING_RETRY_MAX_OUTPUT_TOKENS,
            validator=lambda payload: _validate_group_coverage(
                payload,
                allowed_ids={str(card[id_field]) for card in cards},
                stage=mode,
            ),
            max_attempts=GROUPING_MAX_ATTEMPTS,
        )
        return payload, [
            {
                **usage,
                "input_items": len(cards),
                "output_groups": len(payload.groups),
            },
            *diagnostics,
        ]

    async def _merge_group_items(
        self,
        *,
        cards: list[dict[str, object]],
        provider: ChatProvider,
        profile_context: str,
    ) -> tuple[_GroupMergePayload, list[dict[str, object]]]:
        """对一批 Group cards 执行只允许 merge／keep 的全局收敛。"""

        if len(cards) > GROUP_MERGE_BATCH_SIZE:
            raise ProfileKnowledgeMapError(
                "knowledge_map_group_limit_exceeded",
                f"group merge input exceeds {GROUP_MERGE_BATCH_SIZE} items",
                diagnostics={"item_count": len(cards)},
            )
        messages = [
            ChatMessage(role="system", content=GROUP_MERGE_PROMPT),
            ChatMessage(
                role="user",
                content=json_dumps(
                    {
                        "profile_context": profile_context,
                        "groups": cards,
                    }
                ),
            ),
        ]
        payload, usage, diagnostics = await _complete_structured_json(
            provider=provider,
            messages=messages,
            response_type=_GroupMergePayload,
            stage="global_grouping",
            invalid_code="knowledge_map_group_merge_invalid",
            first_max_output_tokens=GROUPING_MAX_OUTPUT_TOKENS,
            retry_max_output_tokens=GROUPING_RETRY_MAX_OUTPUT_TOKENS,
            validator=lambda value: _validate_group_merge(
                value,
                allowed_ids={str(card["group_id"]) for card in cards},
            ),
            max_attempts=GROUPING_MAX_ATTEMPTS,
        )
        merge_count = sum(action.decision == "merge" for action in payload.actions)
        keep_count = sum(action.decision == "keep" for action in payload.actions)
        return payload, [
            {
                **usage,
                "input_items": len(cards),
                "merge_actions": merge_count,
                "keep_actions": keep_count,
                "uncertain_pairs": len(payload.uncertain_pairs),
            },
            *diagnostics,
        ]

    def _groups_from_topic_drafts(
        self,
        *,
        groups: list[_GroupDraft],
        topic_by_id: dict[str, ProfileMapTopic],
    ) -> list[ProfileMapGroup]:
        result: list[ProfileMapGroup] = []
        for draft in groups:
            topic_ids = ordered_unique(draft.item_ids)
            source_doc_ids = sorted(
                {
                    doc_id
                    for topic_id in topic_ids
                    for doc_id in topic_by_id[topic_id].source_doc_ids
                }
            )
            group_hash = stable_hash(
                {
                    "label": normalize_name(draft.label),
                    "topic_ids": sorted(topic_ids),
                }
            )
            result.append(
                ProfileMapGroup(
                    group_id=f"wg_{group_hash[:16]}",
                    label=draft.label.strip(),
                    aliases=ordered_unique(
                        alias.strip() for alias in draft.aliases if alias.strip()
                    ),
                    summary=draft.summary.strip(),
                    retrieval_questions=ordered_unique(
                        question.strip()
                        for question in draft.retrieval_questions
                        if question.strip()
                    ),
                    child_topic_ids=topic_ids,
                    source_doc_ids=source_doc_ids,
                )
            )
        return result

    @staticmethod
    def _merge_groups_by_actions(
        actions: list[_GroupMergeAction],
        source_groups: list[ProfileMapGroup],
    ) -> tuple[list[ProfileMapGroup], dict[str, int]]:
        """把 merge／keep actions 应用成单调的 Group clusters。"""

        group_by_id = {group.group_id: group for group in source_groups}
        parent = {group_id: group_id for group_id in group_by_id}

        def find(group_id: str) -> str:
            while parent[group_id] != group_id:
                parent[group_id] = parent[parent[group_id]]
                group_id = parent[group_id]
            return group_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        merge_actions = [action for action in actions if action.decision == "merge"]
        for action in merge_actions:
            first = action.group_ids[0]
            for group_id in action.group_ids[1:]:
                union(first, group_id)

        clusters: dict[str, list[str]] = defaultdict(list)
        for group_id in group_by_id:
            clusters[find(group_id)].append(group_id)

        action_by_ids = {tuple(sorted(action.group_ids)): action for action in merge_actions}
        merged_groups: list[ProfileMapGroup] = []
        incomplete_canonical_count = 0
        for cluster_ids in sorted(
            (sorted(cluster) for cluster in clusters.values()),
            key=lambda cluster: tuple(cluster),
        ):
            source = [group_by_id[group_id] for group_id in cluster_ids]
            if len(source) == 1:
                merged_groups.append(source[0])
                continue

            exact_action = action_by_ids.get(tuple(cluster_ids))
            if exact_action is None:
                incomplete_canonical_count += 1
                compatible_actions = [
                    action
                    for action in merge_actions
                    if set(action.group_ids).issubset(cluster_ids)
                ]
                exact_action = max(
                    compatible_actions,
                    key=lambda action: (
                        len(action.group_ids),
                        tuple(sorted(action.group_ids)),
                    ),
                )
            topic_ids = ordered_unique(
                topic_id for group in source for topic_id in group.child_topic_ids
            )
            source_doc_ids = sorted({doc_id for group in source for doc_id in group.source_doc_ids})
            label = (exact_action.label or "").strip()
            summary = (exact_action.summary or "").strip()
            if not label or not summary:
                raise ProfileKnowledgeMapError(
                    "knowledge_map_group_merge_invalid",
                    "merged Group cluster has no canonical label or summary",
                )
            group_hash = stable_hash(
                {
                    "label": normalize_name(label),
                    "topic_ids": topic_ids,
                }
            )
            merged_groups.append(
                ProfileMapGroup(
                    group_id=f"wg_{group_hash[:16]}",
                    label=label,
                    aliases=ordered_unique(
                        [
                            *(alias for group in source for alias in group.aliases),
                            *exact_action.aliases,
                        ]
                    ),
                    summary=summary,
                    retrieval_questions=ordered_unique(
                        [
                            *(
                                question
                                for group in source
                                for question in group.retrieval_questions
                            ),
                            *exact_action.retrieval_questions,
                        ]
                    ),
                    child_topic_ids=topic_ids,
                    source_doc_ids=source_doc_ids,
                )
            )
        return (
            sorted(merged_groups, key=lambda group: group.group_id),
            {
                "merge_action_count": len(merge_actions),
                "kept_group_count": sum(len(cluster) == 1 for cluster in clusters.values()),
                "merged_cluster_count": sum(len(cluster) > 1 for cluster in clusters.values()),
                "incomplete_canonical_cluster_count": incomplete_canonical_count,
            },
        )

    @staticmethod
    def _auto_merge_high_overlap_groups(
        *,
        groups: list[ProfileMapGroup],
        vectors: dict[str, list[float]],
        edges: list[tuple[str, str, float]],
    ) -> tuple[list[ProfileMapGroup], dict[str, int]]:
        """确定性合并 cosine 与 child Topic 重叠都高的 Groups。"""

        group_by_id = {group.group_id: group for group in groups}
        matrix_ids = [group.group_id for group in groups]
        matrix = _normalized_vector_matrix([vectors[group_id] for group_id in matrix_ids])
        vector_by_id = {group_id: matrix[index] for index, group_id in enumerate(matrix_ids)}
        parent = {group_id: group_id for group_id in group_by_id}

        def find(group_id: str) -> str:
            while parent[group_id] != group_id:
                parent[group_id] = parent[parent[group_id]]
                group_id = parent[group_id]
            return group_id

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        selected_pairs: list[tuple[str, str]] = []
        for left_id, right_id, _score in edges:
            cosine = float(vector_by_id[left_id] @ vector_by_id[right_id])
            left_topics = set(group_by_id[left_id].child_topic_ids)
            right_topics = set(group_by_id[right_id].child_topic_ids)
            jaccard = len(left_topics & right_topics) / max(
                1,
                len(left_topics | right_topics),
            )
            if (
                cosine >= GROUP_AUTO_MERGE_COSINE_FLOOR
                and jaccard >= GROUP_AUTO_MERGE_TOPIC_JACCARD_FLOOR
            ):
                selected_pairs.append((left_id, right_id))
                union(left_id, right_id)

        clusters: dict[str, list[str]] = defaultdict(list)
        for group_id in group_by_id:
            clusters[find(group_id)].append(group_id)
        merged_count = sum(len(cluster) > 1 for cluster in clusters.values())
        if merged_count == 0:
            return groups, {
                "auto_merge_pairs": 0,
                "auto_merged_clusters": 0,
                "groups_removed": 0,
            }

        result: list[ProfileMapGroup] = []
        for cluster_ids in clusters.values():
            source = [group_by_id[group_id] for group_id in sorted(cluster_ids)]
            if len(source) == 1:
                result.append(source[0])
                continue
            label = "；".join(ordered_unique(group.label for group in source))[:200]
            summary = "；".join(ordered_unique(group.summary for group in source))[:1_000]
            topic_ids = ordered_unique(
                topic_id for group in source for topic_id in group.child_topic_ids
            )
            group_hash = stable_hash(
                {
                    "label": normalize_name(label),
                    "topic_ids": topic_ids,
                }
            )
            result.append(
                ProfileMapGroup(
                    group_id=f"wg_{group_hash[:16]}",
                    label=label,
                    aliases=ordered_unique(alias for group in source for alias in group.aliases),
                    summary=summary,
                    retrieval_questions=ordered_unique(
                        question for group in source for question in group.retrieval_questions
                    ),
                    child_topic_ids=topic_ids,
                    source_doc_ids=sorted(
                        {doc_id for group in source for doc_id in group.source_doc_ids}
                    ),
                )
            )
        return sorted(result, key=lambda group: group.group_id), {
            "auto_merge_pairs": len(selected_pairs),
            "auto_merged_clusters": merged_count,
            "groups_removed": len(groups) - len(result),
        }

    def _map_path(self, snapshot_id: str, map_id: str) -> Path:
        return self._root / snapshot_id / map_id / "map.json"

    def _vector_bundle_path(
        self,
        snapshot_id: str,
        map_id: str,
        *,
        bundle_ref: str = "vectors.npz",
    ) -> Path:
        return self._root / snapshot_id / map_id / bundle_ref

    @staticmethod
    def _write_candidate_identity_vector_bundle(
        path: Path,
        *,
        candidate_identity_vectors: dict[str, PersistedIdentityVector],
    ) -> str:
        """原子写入 CandidateRef 绑定的不可变身份向量 bundle。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        refs = sorted(candidate_identity_vectors)
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix="candidate-vectors-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            np.savez_compressed(
                temporary_file,
                candidate_identity_refs=np.asarray(refs, dtype=np.str_),
                candidate_identity_vectors=np.asarray(
                    [candidate_identity_vectors[item_id].vector for item_id in refs],
                    dtype=np.float32,
                ),
                candidate_identity_embedding_contract_hashes=np.asarray(
                    [
                        candidate_identity_vectors[item_id].embedding_contract_hash
                        for item_id in refs
                    ],
                    dtype=np.str_,
                ),
                candidate_identity_summary_input_hashes=np.asarray(
                    [candidate_identity_vectors[item_id].summary_input_hash for item_id in refs],
                    dtype=np.str_,
                ),
                candidate_identity_vector_hashes=np.asarray(
                    [candidate_identity_vectors[item_id].vector_hash for item_id in refs],
                    dtype=np.str_,
                ),
            )
        try:
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return _vector_bundle_hash(path)

    @staticmethod
    def _read_candidate_identity_vector_bundle(
        path: Path,
        *,
        expected_hash: str,
    ) -> dict[str, PersistedIdentityVector]:
        """读取并校验 CandidateRef 身份向量 bundle。"""

        if _vector_bundle_hash(path) != expected_hash:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "candidate identity vector bundle hash does not match",
            )
        try:
            with np.load(path, allow_pickle=False) as bundle:
                return _identity_vectors_from_bundle(
                    bundle,
                    prefix="candidate_identity",
                )
        except ProfileKnowledgeMapError:
            raise
        except Exception as error:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "candidate identity vector bundle is unreadable",
            ) from error

    @staticmethod
    def _write_vector_bundle(
        path: Path,
        *,
        group_vectors: dict[str, list[float]],
        topic_vectors: dict[str, list[float]],
        aspect_vectors: dict[str, list[float]],
        topic_identity_vectors: dict[str, PersistedIdentityVector] | None = None,
    ) -> str:
        """原子写入归一化 vectors，并返回 bundle hash。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        group_ids = sorted(group_vectors)
        topic_ids = sorted(topic_vectors)
        aspect_ids = sorted(aspect_vectors)
        topic_identity_entries = topic_identity_vectors or {}
        topic_identity_refs = sorted(topic_identity_entries)
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix="vectors-",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            np.savez_compressed(
                temporary_file,
                group_ids=np.asarray(group_ids, dtype=np.str_),
                group_vectors=np.asarray(
                    [group_vectors[item_id] for item_id in group_ids],
                    dtype=np.float32,
                ),
                topic_ids=np.asarray(topic_ids, dtype=np.str_),
                topic_vectors=np.asarray(
                    [topic_vectors[item_id] for item_id in topic_ids],
                    dtype=np.float32,
                ),
                aspect_ids=np.asarray(aspect_ids, dtype=np.str_),
                aspect_vectors=np.asarray(
                    [aspect_vectors[item_id] for item_id in aspect_ids],
                    dtype=np.float32,
                ),
                topic_identity_refs=np.asarray(topic_identity_refs, dtype=np.str_),
                topic_identity_vectors=np.asarray(
                    [topic_identity_entries[item_id].vector for item_id in topic_identity_refs],
                    dtype=np.float32,
                ),
                topic_identity_embedding_contract_hashes=np.asarray(
                    [
                        topic_identity_entries[item_id].embedding_contract_hash
                        for item_id in topic_identity_refs
                    ],
                    dtype=np.str_,
                ),
                topic_identity_summary_input_hashes=np.asarray(
                    [
                        topic_identity_entries[item_id].summary_input_hash
                        for item_id in topic_identity_refs
                    ],
                    dtype=np.str_,
                ),
                topic_identity_vector_hashes=np.asarray(
                    [
                        topic_identity_entries[item_id].vector_hash
                        for item_id in topic_identity_refs
                    ],
                    dtype=np.str_,
                ),
            )
        try:
            os.replace(temporary_path, path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return _vector_bundle_hash(path)

    @staticmethod
    def _read_vector_bundle(
        path: Path,
        *,
        expected_hash: str,
    ) -> tuple[
        dict[str, list[float]],
        dict[str, list[float]],
        dict[str, list[float]],
        dict[str, PersistedIdentityVector],
    ]:
        """读取并校验 vector bundle。"""

        if _vector_bundle_hash(path) != expected_hash:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "knowledge map vector bundle hash does not match",
            )
        try:
            with np.load(path, allow_pickle=False) as bundle:
                group_ids = [str(value) for value in bundle["group_ids"].tolist()]
                topic_ids = [str(value) for value in bundle["topic_ids"].tolist()]
                aspect_ids = [str(value) for value in bundle["aspect_ids"].tolist()]
                group_vectors = {
                    item_id: [float(value) for value in vector]
                    for item_id, vector in zip(
                        group_ids,
                        bundle["group_vectors"],
                        strict=True,
                    )
                }
                topic_vectors = {
                    item_id: [float(value) for value in vector]
                    for item_id, vector in zip(
                        topic_ids,
                        bundle["topic_vectors"],
                        strict=True,
                    )
                }
                aspect_vectors = {
                    item_id: [float(value) for value in vector]
                    for item_id, vector in zip(
                        aspect_ids,
                        bundle["aspect_vectors"],
                        strict=True,
                    )
                }
                topic_identity_vectors = _identity_vectors_from_bundle(
                    bundle,
                    prefix="topic_identity",
                )
        except ProfileKnowledgeMapError:
            raise
        except Exception as error:
            raise ProfileKnowledgeMapError(
                "knowledge_map_invalid",
                "knowledge map vector bundle is unreadable",
            ) from error
        return (
            group_vectors,
            topic_vectors,
            aspect_vectors,
            topic_identity_vectors,
        )

    def _write_manifest(self, path: Path, manifest: ProfileKnowledgeMapManifest) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)


class SearchWikiTopicV2Service:
    """在知识地图上执行有界主题检索，只返回原始 blocks。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._maps = ProfileKnowledgeMapService(self._data_root)
        self._retrieval = RetrievalReadService(self._data_root)

    def latest_map_id(self, snapshot_id: str) -> str | None:
        """返回当前 snapshot 最新知识地图 ID。"""

        latest = self._maps.latest(snapshot_id)
        return latest.manifest.map_id if latest is not None else None

    def active_map_id_for_selected_profile(self, snapshot_id: str) -> str | None:
        """返回当前 selected Profile 对应的 active map ID。"""

        active = self._maps.active_for_selected_profile(snapshot_id)
        return active.manifest.map_id if active is not None else None

    async def search(
        self,
        *,
        snapshot_id: str,
        map_id: str,
        topic: str,
        embedding_provider: EmbeddingProvider,
        provider: ChatProvider,
        forced_group_ids: list[str] | None = None,
        enable_group_coverage_audit: bool = False,
    ) -> SearchWikiTopicV2Execution:
        """执行 topic → groups → topics → evidence blocks 的有界检索。

        ``forced_group_ids`` 和 ``enable_group_coverage_audit`` 只用于离线诊断。
        forced 模式绕过 LLM Group Router，但仍要求 Group 已经通过正式候选生成；
        coverage audit 在当前样本上没有证明稳定收益，因此默认关闭。两者都不由产品
        ``search_wiki_topic_v2`` 工具暴露。
        """

        normalized_topic = topic.strip()
        if not normalized_topic:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_invalid",
                "topic must not be empty",
            )
        map_result = self._maps.load(snapshot_id, map_id)
        manifest = map_result.manifest
        retrieval_query = "\n".join(
            [
                normalized_topic,
                manifest.profile_context,
                *manifest.representative_questions,
            ]
        ).strip()
        topic_by_id = {item.topic_id: item for item in manifest.topics}
        group_texts = [_group_search_text(group) for group in manifest.groups]
        vector_by_group_id = map_result.group_vectors
        if embedding_provider.profile.model_id != manifest.group_embedding_model_id:
            raise ProfileKnowledgeMapError(
                "knowledge_map_embedding_mismatch",
                "knowledge map group vectors use a different embedding model",
                diagnostics={
                    "map_embedding_model_id": manifest.group_embedding_model_id,
                    "query_embedding_model_id": embedding_provider.profile.model_id,
                },
            )
        if embedding_provider.profile.model_id != manifest.topic_embedding_model_id:
            raise ProfileKnowledgeMapError(
                "knowledge_map_embedding_mismatch",
                "knowledge map topic vectors use a different embedding model",
                diagnostics={
                    "map_topic_embedding_model_id": manifest.topic_embedding_model_id,
                    "query_embedding_model_id": embedding_provider.profile.model_id,
                },
            )
        if embedding_provider.profile.model_id != manifest.aspect_embedding_model_id:
            raise ProfileKnowledgeMapError(
                "knowledge_map_embedding_mismatch",
                "knowledge map aspect vectors use a different embedding model",
                diagnostics={
                    "map_aspect_embedding_model_id": manifest.aspect_embedding_model_id,
                    "query_embedding_model_id": embedding_provider.profile.model_id,
                },
            )
        try:
            query_embedding = await embedding_provider.embed_query(retrieval_query)
        except EmbeddingProviderError as error:
            raise ProfileKnowledgeMapError(error.code, error.message) from error
        ordered_group_vectors = [vector_by_group_id[group.group_id] for group in manifest.groups]
        query_vector = _normalized_vector_matrix([query_embedding.vector])[0]
        normalized_group_vectors = _normalized_vector_matrix(ordered_group_vectors)
        dense_scores = normalized_group_vectors @ query_vector
        topic_vector_by_id = map_result.topic_vectors
        scores = [
            _hybrid_score_with_dense(
                retrieval_query,
                text,
                float(dense),
            )
            for text, dense in zip(group_texts, dense_scores, strict=True)
        ]
        group_scores = {
            group.group_id: score for group, score in zip(manifest.groups, scores, strict=True)
        }
        candidate_groups, topic_scores = _adaptive_group_candidates(
            groups=manifest.groups,
            group_scores=group_scores,
            topic_by_id=topic_by_id,
            topic_vector_by_id=topic_vector_by_id,
            retrieval_query=retrieval_query,
            query_vector=query_vector,
        )
        group_matches = [
            SearchWikiTopicV2GroupMatch(
                group_id=group.group_id,
                score=group_scores[group.group_id],
            )
            for group in candidate_groups
        ]
        eligible_group_count = len(manifest.groups)
        if not candidate_groups:
            return SearchWikiTopicV2Execution(
                result=SearchWikiTopicV2Result(
                    topic=normalized_topic,
                    blocks=[],
                    empty_reason="no_match",
                ),
                matched_group_ids=[],
                matched_topic_ids=[],
                group_matches=[],
                eligible_group_count=eligible_group_count,
                candidate_block_count=0,
                selected_block_count=0,
                usage={},
            )
        group_by_id = {group.group_id: group for group in candidate_groups}
        if forced_group_ids:
            forced_ids = ordered_unique(forced_group_ids)
            unknown_forced_ids = sorted(set(forced_ids) - set(group_by_id))
            if unknown_forced_ids:
                raise ProfileKnowledgeMapError(
                    "search_wiki_topic_forced_group_not_candidate",
                    "forced diagnostic groups must exist in the routed candidate set",
                    diagnostics={"group_ids": unknown_forced_ids},
                )
            if len(forced_ids) > GROUP_SELECTION_SAFETY_LIMIT:
                raise ProfileKnowledgeMapError(
                    "search_wiki_topic_forced_group_budget_exceeded",
                    "forced diagnostic group count exceeds the selection safety limit",
                    diagnostics={
                        "group_count": len(forced_ids),
                        "safety_limit": GROUP_SELECTION_SAFETY_LIMIT,
                    },
                )
            group_selection = _GroupSelectionPayload(
                groups=[
                    _SelectedGroupItem(
                        group_id=group_id,
                        relevance="partial",
                        supported_topic_ids=[
                            min(
                                group_by_id[group_id].child_topic_ids,
                                key=lambda topic_id: (
                                    -topic_scores.get(topic_id, 0.0),
                                    topic_id,
                                ),
                            )
                        ],
                        reason="离线诊断：调用方强制选择该候选 Group，绕过 LLM Router。",
                    )
                    for group_id in forced_ids
                ]
            )
            group_selection_usage: dict[str, object] = {
                "input_tokens": 0,
                "output_tokens": 0,
                "attempts": 0,
                "mode": "forced",
                "forced_group_ids": forced_ids,
            }
        else:
            base_group_selection, group_selection_usage = await self._select_groups(
                topic=normalized_topic,
                candidate_groups=candidate_groups,
                profile_context=manifest.profile_context,
                representative_questions=manifest.representative_questions,
                group_scores=group_scores,
                topic_scores=topic_scores,
                topic_by_id=topic_by_id,
                provider=provider,
            )
            if enable_group_coverage_audit:
                supplemental_groups, coverage_audit_usage = await self._audit_group_coverage(
                    topic=normalized_topic,
                    profile_context=manifest.profile_context,
                    representative_questions=manifest.representative_questions,
                    candidate_groups=candidate_groups,
                    selected_groups=base_group_selection,
                    group_scores=group_scores,
                    topic_scores=topic_scores,
                    topic_by_id=topic_by_id,
                    provider=provider,
                )
            else:
                supplemental_groups = []
                coverage_audit_usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "attempts": 0,
                }
            group_selection = _GroupSelectionPayload(
                groups=[*base_group_selection.groups, *supplemental_groups]
            )
            group_selection_usage = {
                **group_selection_usage,
                "mode": "llm",
                "coverage_audit_input_tokens": coverage_audit_usage["input_tokens"],
                "coverage_audit_output_tokens": coverage_audit_usage["output_tokens"],
                "coverage_audit_attempts": coverage_audit_usage["attempts"],
                "supplemental_group_ids": [item.group_id for item in supplemental_groups],
            }
        selected_group_ids = ordered_unique(item.group_id for item in group_selection.groups)[
            :GROUP_SELECTION_SAFETY_LIMIT
        ]
        model_selection_by_id = {item.group_id: item for item in group_selection.groups}
        selected_groups = [group_by_id[group_id] for group_id in selected_group_ids]
        group_selections = [
            SearchWikiTopicV2GroupSelection(
                group_id=group_id,
                relevance=(model_selection_by_id[group_id].relevance),
                supported_topic_ids=model_selection_by_id[group_id].supported_topic_ids,
                reason=model_selection_by_id[group_id].reason,
            )
            for group_id in selected_group_ids
        ]
        if not selected_groups:
            return SearchWikiTopicV2Execution(
                result=SearchWikiTopicV2Result(
                    topic=normalized_topic,
                    blocks=[],
                    empty_reason="no_match",
                ),
                matched_group_ids=[],
                matched_topic_ids=[],
                group_matches=group_matches,
                group_selections=[],
                eligible_group_count=eligible_group_count,
                candidate_block_count=0,
                selected_block_count=0,
                usage={
                    "group_selection_input_tokens": group_selection_usage["input_tokens"],
                    "group_selection_output_tokens": group_selection_usage["output_tokens"],
                },
            )
        eligible_topic_ids = ordered_unique(
            topic_id for group in selected_groups for topic_id in group.child_topic_ids
        )
        eligible_topic_count = len(eligible_topic_ids)
        if len(eligible_topic_ids) > TOPIC_CANDIDATE_SAFETY_LIMIT:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_budget_exceeded",
                "eligible topic count exceeds the safety limit",
                diagnostics={"topic_count": len(eligible_topic_ids)},
            )
        selected_topic_ids = eligible_topic_ids
        topic_matches = [
            SearchWikiTopicV2TopicMatch(
                topic_id=topic_id,
                score=topic_scores.get(topic_id, 0.0),
            )
            for topic_id in selected_topic_ids
        ]
        topics = [topic_by_id[topic_id] for topic_id in selected_topic_ids]
        aspects_by_topic: dict[str, list[TopicAspect]] = defaultdict(list)
        for aspect in manifest.topic_aspects:
            if aspect.topic_id in set(selected_topic_ids):
                aspects_by_topic[aspect.topic_id].append(aspect)
        selected_aspects, aspect_scores = _select_aspects_by_relevance(
            topics=topics,
            aspects_by_topic=aspects_by_topic,
            aspect_vector_by_id=map_result.aspect_vectors,
            retrieval_query=retrieval_query,
            query_vector=query_vector,
        )
        aspect_matches = [
            SearchWikiTopicV2AspectMatch(
                aspect_id=aspect.aspect_id,
                topic_id=aspect.topic_id,
                query_score=aspect_scores[aspect.aspect_id][0],
                priority=aspect_scores[aspect.aspect_id][1],
            )
            for aspect in selected_aspects
        ]
        selected_aspect_ids = [aspect.aspect_id for aspect in selected_aspects]
        selected_aspect_by_id = {aspect.aspect_id: aspect for aspect in selected_aspects}
        support_by_aspect: dict[str, list[BlockAspectSupport]] = defaultdict(list)
        for support in manifest.block_aspect_supports:
            if support.aspect_id in selected_aspect_by_id:
                support_by_aspect[support.aspect_id].append(support)
        candidate_block_ids = ordered_unique(
            support.block_id
            for aspect in selected_aspects
            for support in support_by_aspect.get(aspect.aspect_id, [])
        )
        if len(candidate_block_ids) > EVIDENCE_CANDIDATE_LIMIT:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_budget_exceeded",
                "evidence candidate count exceeds the high-water guard",
                diagnostics={"candidate_block_count": len(candidate_block_ids)},
            )
        group_details_by_topic: dict[str, list[dict[str, object]]] = defaultdict(list)
        for group in selected_groups:
            for topic_id in group.child_topic_ids:
                group_details_by_topic[topic_id].append(
                    {
                        "group_id": group.group_id,
                        "group": group.label,
                        "group_summary": group.summary,
                        "retrieval_questions": group.retrieval_questions,
                    }
                )
        retrieval_paths_by_block: dict[str, list[dict[str, object]]] = defaultdict(list)
        group_ids_by_block: dict[str, set[str]] = defaultdict(set)
        for topic_id in selected_topic_ids:
            topic_entry = topic_by_id[topic_id]
            for block_id in topic_entry.source_block_ids:
                for group_details in group_details_by_topic[topic_id]:
                    path = {
                        **group_details,
                        "topic": topic_entry.title,
                    }
                    if path not in retrieval_paths_by_block[block_id]:
                        retrieval_paths_by_block[block_id].append(path)
                    group_ids_by_block[block_id].add(str(group_details["group_id"]))
        reader = self._retrieval.bind_snapshot(snapshot_id)
        if manifest.snapshot_manifest_hash != reader.manifest.manifest_hash:
            raise ProfileKnowledgeMapError(
                "knowledge_map_snapshot_mismatch",
                "knowledge map snapshot hash does not match the current collection snapshot",
            )
        try:
            blocks = reader.get_blocks(candidate_block_ids).blocks
        except RetrievalFailure as error:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_evidence_invalid",
                error.message,
            ) from error
        if not blocks:
            return SearchWikiTopicV2Execution(
                result=SearchWikiTopicV2Result(
                    topic=normalized_topic,
                    blocks=[],
                    empty_reason="no_selected_evidence",
                ),
                matched_group_ids=[group.group_id for group in selected_groups],
                matched_topic_ids=selected_topic_ids,
                group_matches=group_matches,
                topic_matches=topic_matches,
                group_selections=group_selections,
                eligible_group_count=eligible_group_count,
                eligible_topic_count=eligible_topic_count,
                candidate_block_count=0,
                selected_block_count=0,
                usage={},
            )
        try:
            block_vector_by_id = reader.read_block_vectors(
                candidate_block_ids,
                provider=embedding_provider,
            )
        except RetrievalFailure as error:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_evidence_invalid",
                error.message,
            ) from error
        block_versions = reader.manifest.versions
        if block_versions is None:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_evidence_invalid",
                "collection block vector profile is missing",
            )
        block_embeddings = [block_vector_by_id[block.block_id] for block in blocks]
        normalized_block_vectors = _normalized_vector_matrix(block_embeddings)
        dense_block_scores = normalized_block_vectors @ query_vector
        block_by_id = {block.block_id: block for block in blocks}
        dense_norm_by_block_id = {
            block.block_id: (float(dense_block_scores[index]) + 1.0) / 2.0
            for index, block in enumerate(blocks)
        }
        representative_by_aspect: dict[str, tuple[BlockAspectSupport, str, float]] = {}
        for aspect in selected_aspects:
            supports = support_by_aspect.get(aspect.aspect_id, [])
            if not supports:
                continue
            scored_supports = []
            for support in supports:
                block = block_by_id[support.block_id]
                lexical = _lexical_similarity(retrieval_query, block.text)
                provenance = 1.0 if block.doc_title and block.section_path else 0.5
                representative_score = (
                    0.55 * support.support_strength
                    + 0.25 * dense_norm_by_block_id[support.block_id]
                    + 0.10 * lexical
                    + 0.10 * provenance
                )
                scored_supports.append((support, support.block_id, representative_score))
            representative_by_aspect[aspect.aspect_id] = max(
                scored_supports,
                key=lambda item: (item[2], -int(item[0].support_id[4:], 16)),
            )
        representative_block_ids = ordered_unique(
            block_id for _support, block_id, _score in representative_by_aspect.values()
        )
        audit, selection_usage = await self._audit_aspect_evidence(
            topic=normalized_topic,
            profile_context=manifest.profile_context,
            representative_questions=manifest.representative_questions,
            selected_aspects=selected_aspects,
            representative_by_aspect=representative_by_aspect,
            supports_by_aspect=support_by_aspect,
            blocks=blocks,
            retrieval_paths_by_block=retrieval_paths_by_block,
            provider=provider,
        )
        selected_block_ids = ordered_unique(item.block_id for item in audit.selected_blocks)
        selected_blocks = [block_by_id[block_id] for block_id in selected_block_ids]
        if len(selected_blocks) > EVIDENCE_OUTPUT_LIMIT:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_budget_exceeded",
                "selected evidence count exceeds the high-water guard",
                diagnostics={"selected_block_count": len(selected_blocks)},
            )
        total_tokens = sum(estimate_evidence_tokens(block) for block in selected_blocks)
        if total_tokens > EVIDENCE_OUTPUT_TOKEN_LIMIT:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_budget_exceeded",
                "selected evidence tokens exceed the high-water guard",
                diagnostics={"selected_evidence_tokens": total_tokens},
            )
        return SearchWikiTopicV2Execution(
            result=SearchWikiTopicV2Result(
                topic=normalized_topic,
                blocks=selected_blocks,
                empty_reason=None if selected_blocks else "no_selected_evidence",
            ),
            matched_group_ids=[group.group_id for group in selected_groups],
            matched_topic_ids=selected_topic_ids,
            group_matches=group_matches,
            topic_matches=topic_matches,
            aspect_matches=aspect_matches,
            group_selections=group_selections,
            selected_aspect_ids=selected_aspect_ids,
            eligible_group_count=eligible_group_count,
            group_candidate_count=len(candidate_groups),
            eligible_topic_count=eligible_topic_count,
            candidate_block_ids=candidate_block_ids,
            candidate_block_count=len(blocks),
            selected_block_count=len(selected_blocks),
            selected_evidence=[
                SearchWikiTopicV2EvidenceSelection(
                    block_id=item.block_id,
                    relevance=item.relevance,
                    aspect="；".join(
                        selected_aspect_by_id[aspect_id].label
                        for aspect_id in item.supported_aspect_ids
                    ),
                    reason=item.reason,
                    aspect_ids=item.supported_aspect_ids,
                    duplicate_of=item.duplicate_of,
                    conflicts_with=item.conflicts_with,
                )
                for item in audit.selected_blocks
            ],
            aspect_coverage=[
                SearchWikiTopicV2AspectCoverage(
                    aspect_id=item.aspect_id,
                    status=item.status,
                )
                for item in audit.coverage
            ],
            usage={
                "query_embedding_tokens": query_embedding.input_tokens,
                "block_embedding_tokens": 0,
                "persisted_block_vector_count": len(block_vector_by_id),
                "block_vector_provider_calls": 0,
                "block_embedding_provider": block_versions.embedding_profile.provider,
                "block_embedding_model_id": block_versions.embedding_profile.model_id,
                "block_embedding_input_policy_version": (
                    block_versions.embedding_input_policy.version
                ),
                "group_embedding_tokens": 0,
                "persisted_group_vector_count": len(vector_by_group_id),
                "persisted_topic_vector_count": len(topic_vector_by_id),
                "group_candidate_count": len(candidate_groups),
                "selected_group_count": len(selected_groups),
                "selected_topic_count": len(selected_topic_ids),
                "selected_aspect_count": len(selected_aspects),
                "representative_block_count": len(representative_block_ids),
                "topic_convergence_status": manifest.topic_convergence.status,
                "group_convergence_status": manifest.group_convergence.status,
                "group_selection_input_tokens": group_selection_usage["input_tokens"],
                "group_selection_output_tokens": group_selection_usage["output_tokens"],
                "group_selection_attempts": group_selection_usage["attempts"],
                "group_selection_mode": group_selection_usage["mode"],
                "forced_group_ids": group_selection_usage.get("forced_group_ids", []),
                "coverage_audit_input_tokens": group_selection_usage.get(
                    "coverage_audit_input_tokens", 0
                ),
                "coverage_audit_output_tokens": group_selection_usage.get(
                    "coverage_audit_output_tokens", 0
                ),
                "coverage_audit_attempts": group_selection_usage.get("coverage_audit_attempts", 0),
                "supplemental_group_ids": group_selection_usage.get("supplemental_group_ids", []),
                **selection_usage,
            },
        )

    async def _audit_aspect_evidence(
        self,
        *,
        topic: str,
        profile_context: str,
        representative_questions: list[str],
        selected_aspects: list[TopicAspect],
        representative_by_aspect: dict[str, tuple[BlockAspectSupport, str, float]],
        supports_by_aspect: dict[str, list[BlockAspectSupport]],
        blocks: list[RetrievalBlock],
        retrieval_paths_by_block: dict[str, list[dict[str, object]]],
        provider: ChatProvider,
    ) -> tuple[_AspectEvidenceAuditPayload, dict[str, object]]:
        """对 Aspect 代表 Blocks 执行一次集合级证据审核。"""

        block_by_id = {block.block_id: block for block in blocks}
        candidate_blocks = []
        support_by_block: dict[str, list[dict[str, object]]] = defaultdict(list)
        for aspect in selected_aspects:
            representative = representative_by_aspect.get(aspect.aspect_id)
            if representative is None:
                continue
            support, block_id, score = representative
            support_by_block[block_id].append(
                {
                    "aspect_id": aspect.aspect_id,
                    "kind": aspect.kind,
                    "label": aspect.label,
                    "support_kind": support.support_kind,
                    "support_strength": support.support_strength,
                    "representative_score": score,
                    "reason": support.reason,
                }
            )
        for block_id in sorted(support_by_block):
            block = block_by_id[block_id]
            candidate_blocks.append(
                {
                    "block_id": block.block_id,
                    "doc_title": block.doc_title,
                    "section_path": block.section_path,
                    "text": block.text,
                    "retrieval_paths": retrieval_paths_by_block.get(block_id, []),
                    "aspect_supports": support_by_block[block_id],
                }
            )
        payload = {
            "topic": topic,
            "profile_context": profile_context,
            "representative_questions": representative_questions,
            "aspects": [
                {
                    "aspect_id": aspect.aspect_id,
                    "topic_id": aspect.topic_id,
                    "kind": aspect.kind,
                    "label": aspect.label,
                    "description": aspect.description,
                    "importance": aspect.importance,
                }
                for aspect in selected_aspects
            ],
            "candidate_blocks": candidate_blocks,
        }
        allowed_block_ids = set(support_by_block)
        allowed_aspect_ids = {aspect.aspect_id for aspect in selected_aspects}

        def validate(value: _AspectEvidenceAuditPayload) -> None:
            block_ids = [item.block_id for item in value.selected_blocks]
            if len(block_ids) != len(set(block_ids)):
                raise ValueError("aspect evidence audit returned duplicate blocks")
            if not set(block_ids).issubset(allowed_block_ids):
                raise ValueError("aspect evidence audit returned unknown blocks")
            coverage_ids = [item.aspect_id for item in value.coverage]
            if len(coverage_ids) != len(set(coverage_ids)):
                raise ValueError("aspect evidence audit returned duplicate coverage")
            if set(coverage_ids) != allowed_aspect_ids:
                raise ValueError("aspect evidence audit did not cover all aspects")
            for item in value.selected_blocks:
                if not set(item.supported_aspect_ids).issubset(allowed_aspect_ids):
                    raise ValueError("aspect evidence audit returned unknown aspects")
                if item.duplicate_of is not None and item.duplicate_of not in allowed_block_ids:
                    raise ValueError("aspect evidence audit returned unknown duplicate")
                if not set(item.conflicts_with).issubset(allowed_block_ids):
                    raise ValueError("aspect evidence audit returned unknown conflict")

        audit, usage, diagnostics = await _complete_structured_json(
            provider=provider,
            messages=[
                ChatMessage(role="system", content=ASPECT_EVIDENCE_AUDIT_PROMPT),
                ChatMessage(role="user", content=json_dumps(payload)),
            ],
            response_type=_AspectEvidenceAuditPayload,
            stage="aspect_evidence_audit",
            invalid_code="search_wiki_topic_aspect_evidence_invalid",
            first_max_output_tokens=BLOCK_SELECTION_MAX_OUTPUT_TOKENS,
            retry_max_output_tokens=BLOCK_SELECTION_RETRY_MAX_OUTPUT_TOKENS,
            validator=validate,
        )
        selected_aspect_ids = {
            aspect_id for item in audit.selected_blocks for aspect_id in item.supported_aspect_ids
        }
        coverage_by_aspect_id = {item.aspect_id: item.status for item in audit.coverage}
        fallback_blocks: list[dict[str, str]] = []
        coverage_repairs: list[dict[str, str]] = []
        selected_block_ids = {item.block_id for item in audit.selected_blocks}
        for aspect in selected_aspects:
            if coverage_by_aspect_id.get(aspect.aspect_id) == "missing":
                representative_block_id = representative_by_aspect.get(aspect.aspect_id)
                rejected_representative_id = (
                    representative_block_id[1] if representative_block_id is not None else None
                )
                repair = next(
                    (
                        support
                        for support in sorted(
                            supports_by_aspect.get(aspect.aspect_id, []),
                            key=lambda item: (
                                -item.support_strength,
                                -int(item.support_id[4:], 16),
                            ),
                        )
                        if support.block_id not in selected_block_ids
                        and support.block_id != rejected_representative_id
                    ),
                    None,
                )
                if repair is not None:
                    audit.selected_blocks.append(
                        _AspectEvidenceAuditItem(
                            block_id=repair.block_id,
                            relevance=repair.support_kind,
                            supported_aspect_ids=[aspect.aspect_id],
                            reason="Coverage repair from compiled Aspect support.",
                        )
                    )
                    selected_block_ids.add(repair.block_id)
                    selected_aspect_ids.add(aspect.aspect_id)
                    coverage_repairs.append(
                        {
                            "aspect_id": aspect.aspect_id,
                            "block_id": repair.block_id,
                        }
                    )
                    for coverage_item in audit.coverage:
                        if coverage_item.aspect_id == aspect.aspect_id:
                            coverage_item.status = (
                                "covered" if repair.support_kind == "direct" else "partial"
                            )
                            break
                continue
            if aspect.aspect_id in selected_aspect_ids:
                continue
            representative = representative_by_aspect.get(aspect.aspect_id)
            if representative is None:
                continue
            support, block_id, _score = representative
            if block_id not in selected_block_ids:
                audit.selected_blocks.append(
                    _AspectEvidenceAuditItem(
                        block_id=block_id,
                        relevance=support.support_kind,
                        supported_aspect_ids=[aspect.aspect_id],
                        reason="Deterministic fallback for compiled Aspect support.",
                    )
                )
                selected_block_ids.add(block_id)
                selected_aspect_ids.add(aspect.aspect_id)
            fallback_blocks.append(
                {
                    "aspect_id": aspect.aspect_id,
                    "block_id": block_id,
                }
            )
        return audit, {
            **usage,
            "aspect_evidence_audit_attempts": usage["attempts"],
            "aspect_evidence_candidate_count": len(candidate_blocks),
            "aspect_evidence_fallbacks": fallback_blocks,
            "aspect_evidence_coverage_repairs": coverage_repairs,
            "aspect_evidence_retry_diagnostics": diagnostics,
        }

    async def _select_groups(
        self,
        *,
        topic: str,
        candidate_groups: list[ProfileMapGroup],
        profile_context: str,
        representative_questions: list[str],
        group_scores: dict[str, float],
        topic_scores: dict[str, float],
        topic_by_id: dict[str, ProfileMapTopic],
        provider: ChatProvider,
    ) -> tuple[_GroupSelectionPayload, dict[str, object]]:
        """从有界 group 候选中筛选主题相关分组。"""

        payload = {
            "topic": topic,
            "profile_context": profile_context,
            "representative_questions": representative_questions,
            "candidate_groups": [
                {
                    "group_id": group.group_id,
                    "label": group.label,
                    "aliases": group.aliases,
                    "summary": group.summary,
                    "retrieval_questions": group.retrieval_questions,
                    "aspect_kinds": group.aspect_kinds,
                    "aspect_labels": group.aspect_labels,
                    "group_score": round(group_scores[group.group_id], 6),
                    "candidate_rank": rank,
                    "child_topic_count": len(group.child_topic_ids),
                    "top_child_topics": [
                        {
                            "topic_id": topic_id,
                            "title": topic_by_id[topic_id].title,
                            "query_score": round(topic_scores.get(topic_id, 0.0), 6),
                        }
                        for topic_id in sorted(
                            group.child_topic_ids,
                            key=lambda item_id: (
                                -topic_scores.get(item_id, 0.0),
                                item_id,
                            ),
                        )[:8]
                    ],
                }
                for rank, group in enumerate(candidate_groups, start=1)
            ],
        }
        messages = [
            ChatMessage(role="system", content=GROUP_SELECTION_PROMPT),
            ChatMessage(role="user", content=json_dumps(payload)),
        ]
        selected, usage, _diagnostics = await _complete_structured_json(
            provider=provider,
            messages=messages,
            response_type=_GroupSelectionPayload,
            stage="group_selection",
            invalid_code="search_wiki_topic_group_selection_invalid",
            first_max_output_tokens=8_192,
            retry_max_output_tokens=16_384,
            validator=lambda payload: _validate_group_selection(
                payload,
                allowed_ids={group.group_id for group in candidate_groups},
                allowed_topic_ids={
                    group.group_id: set(group.child_topic_ids) for group in candidate_groups
                },
            ),
        )
        return selected, usage

    async def _audit_group_coverage(
        self,
        *,
        topic: str,
        profile_context: str,
        representative_questions: list[str],
        candidate_groups: list[ProfileMapGroup],
        selected_groups: _GroupSelectionPayload,
        group_scores: dict[str, float],
        topic_scores: dict[str, float],
        topic_by_id: dict[str, ProfileMapTopic],
        provider: ChatProvider,
    ) -> tuple[list[_SelectedGroupItem], dict[str, object]]:
        """审查未选候选是否补足基础 Router 遗漏的独立检索维度。"""

        selected_ids = {item.group_id for item in selected_groups.groups}
        candidate_by_id = {group.group_id: group for group in candidate_groups}
        unselected_groups = [
            group for group in candidate_groups if group.group_id not in selected_ids
        ]
        if not unselected_groups:
            return [], {
                "input_tokens": 0,
                "output_tokens": 0,
                "attempts": 0,
            }

        payload = {
            "topic": topic,
            "profile_context": profile_context,
            "representative_questions": representative_questions,
            "selected_groups": [
                {
                    "group_id": item.group_id,
                    "label": candidate_by_id[item.group_id].label,
                    "summary": candidate_by_id[item.group_id].summary,
                    "retrieval_questions": candidate_by_id[item.group_id].retrieval_questions[:8],
                    "selected_topics": [
                        {
                            "topic_id": topic_id,
                            "title": topic_by_id[topic_id].title,
                        }
                        for topic_id in item.supported_topic_ids
                    ],
                }
                for item in selected_groups.groups
            ],
            "unselected_candidate_groups": [
                {
                    "group_id": group.group_id,
                    "label": group.label,
                    "summary": group.summary,
                    "retrieval_questions": group.retrieval_questions[:8],
                    "aspect_labels": group.aspect_labels[:12],
                    "group_score": round(group_scores[group.group_id], 6),
                    "candidate_rank": rank,
                    "top_child_topics": [
                        {
                            "topic_id": topic_id,
                            "title": topic_by_id[topic_id].title,
                            "query_score": round(topic_scores.get(topic_id, 0.0), 6),
                        }
                        for topic_id in sorted(
                            group.child_topic_ids,
                            key=lambda item_id: (
                                -topic_scores.get(item_id, 0.0),
                                item_id,
                            ),
                        )[:8]
                    ],
                }
                for rank, group in enumerate(candidate_groups, start=1)
                if group.group_id not in selected_ids
            ],
        }
        messages = [
            ChatMessage(role="system", content=GROUP_COVERAGE_AUDIT_PROMPT),
            ChatMessage(role="user", content=json_dumps(payload)),
        ]
        audited, usage, _diagnostics = await _complete_structured_json(
            provider=provider,
            messages=messages,
            response_type=_GroupCoverageAuditPayload,
            stage="group_coverage_audit",
            invalid_code="search_wiki_topic_group_coverage_audit_invalid",
            first_max_output_tokens=8_192,
            retry_max_output_tokens=16_384,
            validator=lambda payload: _validate_group_coverage_audit(
                payload,
                selected_group_ids=selected_ids,
                allowed_topic_ids={
                    group.group_id: set(group.child_topic_ids) for group in unselected_groups
                },
            ),
        )
        if len(selected_ids) + len(audited.supplemental_groups) > GROUP_SELECTION_SAFETY_LIMIT:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_group_selection_budget_exceeded",
                "combined routed and coverage-audited group count exceeds the safety limit",
                diagnostics={
                    "selected_group_count": len(selected_ids),
                    "supplemental_group_count": len(audited.supplemental_groups),
                    "safety_limit": GROUP_SELECTION_SAFETY_LIMIT,
                },
            )
        return [
            _SelectedGroupItem(
                group_id=item.group_id,
                relevance="partial",
                supported_topic_ids=item.supported_topic_ids,
                reason=f"覆盖补选｜{item.new_dimension}｜{item.reason}",
            )
            for item in audited.supplemental_groups
        ], usage


def _topic_identity_text(topic: ProfileMapTopic) -> str:
    return _summary_identity_text(topic.summaries)


def _topic_grouping_text(topic: ProfileMapTopic) -> str:
    return "\n".join([topic.title, *topic.aliases, *topic.summaries])


def _normalized_vector_matrix(vectors: list[list[float]]) -> np.ndarray:
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2:
        raise ValueError("vectors must form a matrix")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)


def _normalize_vector(vector: list[float]) -> list[float]:
    """把一个 embedding 转成独立归一化向量。"""

    return _normalized_vector_matrix([vector])[0].tolist()


def _semantic_batches[Item](
    items: list[Item],
    vectors: list[list[float]],
    max_size: int,
    *,
    key: Callable[[Item], str],
) -> list[list[Item]]:
    """用 farthest-first seeds 建立有界语义 batch，再按最近 centroid 分配。"""

    if not items:
        return []
    if len(items) != len(vectors):
        raise ValueError("item and vector counts must match")
    if max_size < 1:
        raise ValueError("max_size must be positive")
    matrix = _normalized_vector_matrix(vectors)
    ordered_indices = sorted(range(len(items)), key=lambda index: key(items[index]))
    if len(items) <= max_size:
        return [[items[index] for index in ordered_indices]]

    seed_count = math.ceil(len(items) / max_size)
    seeds = [ordered_indices[0]]
    remaining = set(ordered_indices[1:])
    while len(seeds) < seed_count and remaining:
        remaining_ordered = sorted(remaining)
        similarities = matrix[remaining_ordered] @ matrix[seeds].T
        farthest_local_index = int(np.argmin(np.max(similarities, axis=1)))
        next_seed = remaining_ordered[farthest_local_index]
        seeds.append(next_seed)
        remaining.remove(next_seed)

    batches: list[list[int]] = [[seed] for seed in seeds]
    seed_set = set(seeds)
    similarities = matrix @ matrix[seeds].T
    non_seed_indices = [index for index in ordered_indices if index not in seed_set]
    for index in non_seed_indices:
        ranked_batches = np.argsort(similarities[index])[::-1]
        available = [
            int(batch_index)
            for batch_index in ranked_batches
            if len(batches[int(batch_index)]) < max_size
        ]
        if not available:
            raise AssertionError("semantic batch capacity must cover every item")
        batch_index = available[0]
        batches[batch_index].append(index)
    return [
        [items[index] for index in sorted(batch, key=lambda item_index: key(items[item_index]))]
        for batch in batches
    ]


async def _embed_all(
    provider: EmbeddingProvider,
    texts: list[str],
) -> list[list[float]]:
    batches = [
        texts[start : start + provider.batch_size]
        for start in range(0, len(texts), provider.batch_size)
    ]
    semaphore = asyncio.Semaphore(MAP_BUILD_MAX_PARALLEL_EMBEDDING_CALLS)
    vectors_by_batch: list[list[list[float]]] = [[] for _ in batches]

    async def embed_batch(batch_index: int, batch: list[str]) -> None:
        async with semaphore:
            result = await provider.embed_documents(batch)
        vectors_by_batch[batch_index] = result.vectors

    await asyncio.gather(*(embed_batch(index, batch) for index, batch in enumerate(batches)))
    return [vector for batch_vectors in vectors_by_batch for vector in batch_vectors]


def _group_search_text(group: ProfileMapGroup) -> str:
    return "\n".join(
        [
            group.label,
            *group.aliases,
            group.summary,
            *group.retrieval_questions,
            *group.aspect_labels,
        ]
    )


def _topic_search_text(topic: ProfileMapTopic) -> str:
    return "\n".join([topic.title, *topic.aliases, *topic.summaries])


def _aspect_search_text(aspect: TopicAspect) -> str:
    return "\n".join(
        [
            aspect.kind,
            aspect.label,
            aspect.description,
        ]
    )


def _adaptive_group_candidates(
    *,
    groups: list[ProfileMapGroup],
    group_scores: dict[str, float],
    topic_by_id: dict[str, ProfileMapTopic],
    topic_vector_by_id: dict[str, list[float]],
    retrieval_query: str,
    query_vector: list[float],
) -> tuple[list[ProfileMapGroup], dict[str, float]]:
    """逐个加入 Group，直到相关性与新增 Topic 覆盖不再满足准入条件。"""

    if not groups:
        return [], {}
    candidate_topic_ids = ordered_unique(
        topic_id for group in groups for topic_id in group.child_topic_ids
    )
    topic_scores: dict[str, float] = {}
    if candidate_topic_ids:
        normalized = _normalized_vector_matrix(
            [
                query_vector,
                *(topic_vector_by_id[topic_id] for topic_id in candidate_topic_ids),
            ]
        )
        dense = normalized @ normalized[0]
        topic_scores = {
            topic_id: (
                ((float(dense[index + 1]) + 1.0) / 2.0) * 0.65
                + _lexical_similarity(retrieval_query, _topic_search_text(topic_by_id[topic_id]))
                * 0.35
            )
            for index, topic_id in enumerate(candidate_topic_ids)
        }

    ordered = sorted(groups, key=lambda group: (-group_scores[group.group_id], group.group_id))
    selected = [ordered[0]]
    selected_ids = {ordered[0].group_id}
    covered_topics = set(ordered[0].child_topic_ids)
    while len(selected) < GROUP_CANDIDATE_SAFETY_LIMIT:
        best_group: ProfileMapGroup | None = None
        best_marginal = -1.0
        for group in ordered:
            if group.group_id in selected_ids:
                continue
            uncovered = [
                topic_scores[topic_id]
                for topic_id in group.child_topic_ids
                if topic_id not in covered_topics and topic_id in topic_scores
            ]
            topic_gain = max(uncovered, default=0.0)
            group_score = group_scores[group.group_id]
            eligible = group_score >= GROUP_HIGH_RELEVANCE_FLOOR or (
                group_score >= GROUP_RESCUE_RELEVANCE_FLOOR
                and topic_gain >= GROUP_TOPIC_COVERAGE_FLOOR
            )
            if not eligible:
                continue
            marginal = (
                GROUP_MARGINAL_RELEVANCE_WEIGHT * group_score
                + GROUP_MARGINAL_TOPIC_WEIGHT * topic_gain
            )
            if marginal > best_marginal:
                best_marginal = marginal
                best_group = group
        if best_group is None:
            break
        selected.append(best_group)
        selected_ids.add(best_group.group_id)
        covered_topics.update(best_group.child_topic_ids)
    if len(selected) >= GROUP_CANDIDATE_SAFETY_LIMIT:
        still_eligible = any(
            group.group_id not in selected_ids
            and (
                group_scores[group.group_id] >= GROUP_HIGH_RELEVANCE_FLOOR
                or (
                    group_scores[group.group_id] >= GROUP_RESCUE_RELEVANCE_FLOOR
                    and any(
                        topic_id not in covered_topics
                        and topic_scores.get(topic_id, 0.0) >= GROUP_TOPIC_COVERAGE_FLOOR
                        for topic_id in group.child_topic_ids
                    )
                )
            )
            for group in ordered
        )
        if still_eligible:
            raise ProfileKnowledgeMapError(
                "search_wiki_topic_group_candidate_exhausted",
                "adaptive group candidate selection reached its safety limit",
            )
    return selected, topic_scores


def _select_aspects_by_relevance(
    *,
    topics: list[ProfileMapTopic],
    aspects_by_topic: dict[str, list[TopicAspect]],
    aspect_vector_by_id: dict[str, list[float]],
    retrieval_query: str,
    query_vector: list[float],
) -> tuple[list[TopicAspect], dict[str, tuple[float, float]]]:
    """先为每个 Topic 选一个 Aspect seed，再按 query／importance 补充。"""

    aspect_candidates = [
        aspect for topic in topics for aspect in aspects_by_topic.get(topic.topic_id, [])
    ]
    if not aspect_candidates:
        return [], {}
    normalized = _normalized_vector_matrix(
        [query_vector, *(aspect_vector_by_id[aspect.aspect_id] for aspect in aspect_candidates)]
    )
    query_normalized = normalized[0]
    scores: dict[str, tuple[float, float]] = {}
    for index, aspect in enumerate(aspect_candidates):
        dense = (float(query_normalized @ normalized[index + 1]) + 1.0) / 2.0
        lexical = _lexical_similarity(retrieval_query, _aspect_search_text(aspect))
        query_score = dense * 0.65 + lexical * 0.35
        priority = query_score * 0.70 + aspect.importance * 0.30
        scores[aspect.aspect_id] = (query_score, priority)
    selected_ids: set[str] = set()
    selected: list[TopicAspect] = []
    for topic in topics:
        topic_aspects = aspects_by_topic.get(topic.topic_id, [])
        if not topic_aspects:
            continue
        seed = max(
            topic_aspects,
            key=lambda aspect: (-scores[aspect.aspect_id][1], aspect.aspect_id),
        )
        selected.append(seed)
        selected_ids.add(seed.aspect_id)
    for aspect in sorted(
        aspect_candidates,
        key=lambda item: (-scores[item.aspect_id][1], item.aspect_id),
    ):
        if aspect.aspect_id in selected_ids:
            continue
        query_score, priority = scores[aspect.aspect_id]
        if (
            query_score >= ASPECT_QUERY_RELEVANCE_FLOOR
            or priority >= ASPECT_IMPORTANCE_RESCUE_FLOOR
        ):
            selected.append(aspect)
            selected_ids.add(aspect.aspect_id)
    if len(selected) > ASPECT_CANDIDATE_SAFETY_LIMIT:
        raise ProfileKnowledgeMapError(
            "search_wiki_topic_aspect_candidate_exhausted",
            "adaptive aspect selection reached its safety limit",
        )
    selected.sort(key=lambda aspect: (-scores[aspect.aspect_id][1], aspect.aspect_id))
    return selected, scores


def _hybrid_score_with_dense(query: str, text: str, dense: float) -> float:
    query_terms = _lexical_terms(query)
    text_terms = _lexical_terms(text)
    overlap = len(query_terms & text_terms) / max(1, len(query_terms))
    normalized_dense = (dense + 1.0) / 2.0
    return normalized_dense * 0.65 + overlap * 0.35


def _lexical_similarity(query: str, text: str) -> float:
    query_terms = _lexical_terms(query)
    text_terms = _lexical_terms(text)
    return len(query_terms & text_terms) / max(1, len(query_terms))


def _lexical_terms(value: str) -> set[str]:
    normalized = normalize_name(value)
    ascii_terms = set(re.findall(r"[a-z0-9_]+", normalized))
    cjk_terms: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", normalized):
        if len(run) == 1:
            cjk_terms.add(run)
            continue
        cjk_terms.update(run[index : index + 2] for index in range(len(run) - 1))
    return ascii_terms | cjk_terms


def _chunks[Item](items: list[Item], size: int) -> list[list[Item]]:
    """把输入按固定最大长度切块；只用于模型批处理，不表达身份边界。"""

    if size < 1:
        raise ValueError("chunk size must be positive")
    return [items[index : index + size] for index in range(0, len(items), size)]


def _topic_batches_by_block_budget(
    topics: list[ProfileMapTopic],
    *,
    max_topics: int,
    max_blocks: int,
    block_count: Callable[[ProfileMapTopic], int] | None = None,
) -> list[list[ProfileMapTopic]]:
    """按 Topic 数和 Block 数双重预算切批，避免结构化输出因输出上限漏项。"""

    if max_topics < 1 or max_blocks < 1:
        raise ValueError("batch budgets must be positive")
    batches: list[list[ProfileMapTopic]] = []
    current: list[ProfileMapTopic] = []
    current_blocks = 0
    for topic in topics:
        topic_blocks = (
            block_count(topic) if block_count is not None else len(topic.source_block_ids)
        )
        if current and (len(current) >= max_topics or current_blocks + topic_blocks > max_blocks):
            batches.append(current)
            current = []
            current_blocks = 0
        current.append(topic)
        current_blocks += topic_blocks
    if current:
        batches.append(current)
    return batches


def _global_candidate_edges[Item](
    items: list[Item],
    vectors: list[list[float]],
    *,
    item_id: Callable[[Item], str],
    score_floor: float,
    top_k: int,
) -> list[tuple[str, str, float]]:
    """用全局向量邻居构造稀疏候选边，不把 semantic batch 当身份边界。"""

    if len(items) < 2:
        return []
    matrix = _normalized_vector_matrix(vectors)
    edges: dict[tuple[str, str], float] = {}
    ids = [item_id(item) for item in items]
    for start in range(0, len(items), GLOBAL_NEIGHBOR_CHUNK_SIZE):
        end = min(start + GLOBAL_NEIGHBOR_CHUNK_SIZE, len(items))
        similarities = matrix[start:end] @ matrix.T
        for local_index, row in enumerate(similarities):
            absolute_index = start + local_index
            row[absolute_index] = -np.inf
            candidate_count = min(top_k, max(0, len(items) - 1))
            if candidate_count == 0:
                continue
            candidate_indices = np.argpartition(row, -candidate_count)[-candidate_count:]
            for candidate_index in candidate_indices:
                normalized_score = (float(row[int(candidate_index)]) + 1.0) / 2.0
                if normalized_score < score_floor:
                    continue
                left = ids[absolute_index]
                right = ids[int(candidate_index)]
                if left == right:
                    continue
                key = (left, right) if left <= right else (right, left)
                edges[key] = max(edges.get(key, 0.0), normalized_score)
    return [(left, right, score) for (left, right), score in sorted(edges.items())]


def _group_candidate_edges(
    *,
    groups: list[ProfileMapGroup],
    edges: list[tuple[str, str, float]],
) -> list[tuple[str, str, float]]:
    """补充 Group 的 lexical、child Topic 和来源文档阻塞边。"""

    edge_map = {(left, right): score for left, right, score in edges}
    _add_token_blocking_edges(
        ids=[group.group_id for group in groups],
        texts=[
            "\n".join(
                [
                    group.label,
                    *group.aliases,
                    group.summary,
                    *group.retrieval_questions,
                ]
            )
            for group in groups
        ],
        edges=edge_map,
        floor=GLOBAL_LEXICAL_EDGE_FLOOR,
        top_k=GLOBAL_BLOCKING_NEIGHBOR_TOP_K,
    )
    _add_jaccard_blocking_edges(
        ids=[group.group_id for group in groups],
        values_by_id={group.group_id: set(group.child_topic_ids) for group in groups},
        edges=edge_map,
        floor=GROUP_TOPIC_OVERLAP_FLOOR,
        top_k=GLOBAL_BLOCKING_NEIGHBOR_TOP_K,
    )
    _add_jaccard_blocking_edges(
        ids=[group.group_id for group in groups],
        values_by_id={group.group_id: set(group.source_doc_ids) for group in groups},
        edges=edge_map,
        floor=GLOBAL_SOURCE_OVERLAP_FLOOR,
        top_k=GLOBAL_BLOCKING_NEIGHBOR_TOP_K,
    )
    return _sorted_candidate_edges(edge_map)


def _group_overlap_audit(
    *,
    groups: list[ProfileMapGroup],
    vectors: list[list[float]],
) -> dict[str, object]:
    """统计当前 Group 候选图中的语义与 child Topic 重叠。"""

    if len(groups) < 2:
        return {
            "candidate_pairs": 0,
            "max_cosine": 0.0,
            "max_child_topic_jaccard": 0.0,
            "groups_with_cosine_ge_0_8": 0,
            "groups_with_cosine_ge_0_9": 0,
            "groups_with_cosine_ge_0_95": 0,
            "groups_with_child_jaccard_ge_0_34": 0,
            "groups_with_child_jaccard_ge_0_5": 0,
            "groups_with_child_jaccard_ge_0_7": 0,
            "topic_membership_count": sum(len(group.child_topic_ids) for group in groups),
            "multi_membership_topic_count": 0,
        }
    semantic_edges = _global_candidate_edges(
        groups,
        vectors,
        item_id=lambda group: group.group_id,
        score_floor=GLOBAL_NEIGHBOR_SCORE_FLOOR,
        top_k=GLOBAL_NEIGHBOR_TOP_K,
    )
    edges = _group_candidate_edges(groups=groups, edges=semantic_edges)
    vector_by_id = {
        group.group_id: _normalized_vector_matrix([vectors[index]])[0]
        for index, group in enumerate(groups)
    }
    topic_set_by_id = {group.group_id: set(group.child_topic_ids) for group in groups}
    cosine_values: list[float] = []
    jaccard_values: list[float] = []
    cosine_groups_08: set[str] = set()
    cosine_groups_09: set[str] = set()
    cosine_groups_095: set[str] = set()
    jaccard_groups_034: set[str] = set()
    jaccard_groups_05: set[str] = set()
    jaccard_groups_07: set[str] = set()
    for left_id, right_id, _score in edges:
        cosine = float(vector_by_id[left_id] @ vector_by_id[right_id])
        left_topics = topic_set_by_id[left_id]
        right_topics = topic_set_by_id[right_id]
        jaccard = len(left_topics & right_topics) / max(
            1,
            len(left_topics | right_topics),
        )
        cosine_values.append(cosine)
        jaccard_values.append(jaccard)
        if cosine >= 0.8:
            cosine_groups_08.update((left_id, right_id))
        if cosine >= 0.9:
            cosine_groups_09.update((left_id, right_id))
        if cosine >= 0.95:
            cosine_groups_095.update((left_id, right_id))
        if jaccard >= 0.34:
            jaccard_groups_034.update((left_id, right_id))
        if jaccard >= 0.5:
            jaccard_groups_05.update((left_id, right_id))
        if jaccard >= 0.7:
            jaccard_groups_07.update((left_id, right_id))
    memberships: dict[str, int] = defaultdict(int)
    for group in groups:
        for topic_id in group.child_topic_ids:
            memberships[topic_id] += 1
    return {
        "candidate_pairs": len(edges),
        "max_cosine": round(max(cosine_values, default=0.0), 6),
        "max_child_topic_jaccard": round(max(jaccard_values, default=0.0), 6),
        "groups_with_cosine_ge_0_8": len(cosine_groups_08),
        "groups_with_cosine_ge_0_9": len(cosine_groups_09),
        "groups_with_cosine_ge_0_95": len(cosine_groups_095),
        "groups_with_child_jaccard_ge_0_34": len(jaccard_groups_034),
        "groups_with_child_jaccard_ge_0_5": len(jaccard_groups_05),
        "groups_with_child_jaccard_ge_0_7": len(jaccard_groups_07),
        "topic_membership_count": sum(memberships.values()),
        "multi_membership_topic_count": sum(count > 1 for count in memberships.values()),
    }


def _add_token_blocking_edges(
    *,
    ids: list[str],
    texts: list[str],
    edges: dict[tuple[str, str], float],
    floor: float,
    top_k: int,
) -> None:
    """用倒排 token 命中补充稀疏候选边，避免把常用词变成长度失控的全连接。"""

    tokens_by_id = {item_id: _lexical_terms(text) for item_id, text in zip(ids, texts, strict=True)}
    postings: dict[str, list[str]] = defaultdict(list)
    for item_id in ids:
        for token in tokens_by_id[item_id]:
            postings[token].append(item_id)
    for item_id in ids:
        candidates: dict[str, int] = defaultdict(int)
        for token in tokens_by_id[item_id]:
            for other_id in postings[token]:
                if other_id != item_id:
                    candidates[other_id] += 1
        ranked = sorted(
            (
                (
                    overlap / max(1, len(tokens_by_id[item_id] | tokens_by_id[other_id])),
                    other_id,
                )
                for other_id, overlap in candidates.items()
            ),
            reverse=True,
        )
        for score, other_id in ranked[:top_k]:
            if score >= floor:
                _set_candidate_edge(edges, item_id, other_id, score)


def _add_jaccard_blocking_edges(
    *,
    ids: list[str],
    values_by_id: dict[str, set[str]],
    edges: dict[tuple[str, str], float],
    floor: float,
    top_k: int,
) -> None:
    """按集合 Jaccard 近邻补充候选边，避免全量两两 Prompt。"""

    postings: dict[str, list[str]] = defaultdict(list)
    for item_id in ids:
        for value in values_by_id[item_id]:
            postings[value].append(item_id)
    for item_id in ids:
        intersections: dict[str, int] = defaultdict(int)
        for value in values_by_id[item_id]:
            for other_id in postings[value]:
                if other_id != item_id:
                    intersections[other_id] += 1
        ranked = sorted(
            (
                (
                    intersection / max(1, len(values_by_id[item_id] | values_by_id[other_id])),
                    other_id,
                )
                for other_id, intersection in intersections.items()
            ),
            reverse=True,
        )
        for score, other_id in ranked[:top_k]:
            if score >= floor:
                _set_candidate_edge(edges, item_id, other_id, score)


def _set_candidate_edge(
    edges: dict[tuple[str, str], float],
    left: str,
    right: str,
    score: float,
) -> None:
    key = (left, right) if left <= right else (right, left)
    edges[key] = max(edges.get(key, 0.0), score)


def _sorted_candidate_edges(
    edges: dict[tuple[str, str], float],
) -> list[tuple[str, str, float]]:
    return [(left, right, score) for (left, right), score in sorted(edges.items())]


def _neighbor_review_batches(
    *,
    ids: list[str],
    edges: list[tuple[str, str, float]],
    max_size: int = GROUP_INPUT_MAX_ITEMS,
) -> list[list[str]]:
    """用候选边覆盖批次；每个 object 可以在多个批次中重复出现。"""

    remaining = {(left, right): score for left, right, score in edges}
    adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in remaining:
        adjacency[left].add(right)
        adjacency[right].add(left)
    batches: list[list[str]] = []
    while remaining:
        left, right = max(remaining, key=lambda pair: (remaining[pair], pair))
        batch = {left, right}
        frontier = sorted(
            adjacency[left] | adjacency[right],
            key=lambda item_id: (
                -(
                    remaining.get(
                        (left, item_id) if left <= item_id else (item_id, left),
                        0.0,
                    )
                ),
                -(
                    remaining.get(
                        (right, item_id) if right <= item_id else (item_id, right),
                        0.0,
                    )
                ),
                item_id,
            ),
        )
        for item_id in frontier:
            if len(batch) >= max_size:
                break
            batch.add(item_id)
        ordered_batch = sorted(batch)
        batches.append(ordered_batch)
        batch_set = set(ordered_batch)
        remaining = {
            pair: score
            for pair, score in remaining.items()
            if not (pair[0] in batch_set and pair[1] in batch_set)
        }
    return batches


def _cluster_ids_from_decisions(
    *,
    ids: list[str],
    clusters: list[list[str]],
) -> list[list[str]]:
    """把多个重叠 batch 的 same 关系合并为当前轮簇。"""

    parent = {item_id: item_id for item_id in ids}

    def find(item_id: str) -> str:
        root = item_id
        while parent[root] != root:
            root = parent[root]
        while parent[item_id] != item_id:
            next_item = parent[item_id]
            parent[item_id] = root
            item_id = next_item
        return root

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for cluster in clusters:
        for item_id in cluster[1:]:
            union(cluster[0], item_id)
    grouped: dict[str, list[str]] = defaultdict(list)
    for item_id in ids:
        grouped[find(item_id)].append(item_id)
    return [sorted(group) for group in grouped.values()]


def _load_json_list(value: str) -> list[object]:
    """读取持久化 JSON 列表；非法值返回空列表供诊断而非冒充语义事实。"""

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def estimate_evidence_tokens(block: RetrievalBlock) -> int:
    """提供知识地图检索的本地保守 token 估算。"""

    return max(1, math.ceil(len(block.text) / 3))
