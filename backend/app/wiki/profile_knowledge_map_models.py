from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, model_validator

from app.profile_extraction.util import normalize_name, stable_hash
from app.retrieval.models import RetrievalBlock


class ProfileMapTopicSource(BaseModel):
    """一个 Topic 聚合的文档局部 candidate 及其证据来源。"""

    extraction_run_id: str
    candidate_id: str
    doc_id: str
    doc_version: int = Field(ge=1)
    source_block_ids: list[str] = Field(min_length=1)
    channel_ids: list[str] = Field(default_factory=list)


class TopicCandidateMembership(BaseModel):
    """Topic 当前成员关系的权威 CandidateRef 记录。"""

    topic_lineage_id: str
    extraction_run_id: str
    candidate_id: str


class TopicSummaryCandidateRef(BaseModel):
    """一条 Topic 摘要对应的成员 CandidateRef。"""

    extraction_run_id: str
    candidate_id: str


class TopicSummarySource(BaseModel):
    """同级 Topic 摘要文本及其全部来源 CandidateRefs。"""

    text: str = Field(min_length=1)
    source_candidate_refs: list[TopicSummaryCandidateRef] = Field(min_length=1)


class ProfileMapTopic(BaseModel):
    """当前 profile＋snapshot 范围内跨文档合并后的 Wiki 主题条目。"""

    topic_id: str
    revision_id: str
    title: str
    aliases: list[str] = Field(default_factory=list)
    summaries: list[str] = Field(min_length=1)
    summary_sources: list[TopicSummarySource] = Field(default_factory=list)
    sources: list[ProfileMapTopicSource] = Field(min_length=1)
    source_doc_ids: list[str] = Field(min_length=1)
    source_block_ids: list[str] = Field(min_length=1)
    channel_ids: list[str] = Field(default_factory=list)


class ProfileMapGroup(BaseModel):
    """知识地图中的多父主题分组；成员仅引用 topic IDs。"""

    group_id: str = Field(pattern=r"^wg_[0-9a-f]{16}$")
    label: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)
    retrieval_questions: list[str] = Field(default_factory=list)
    child_topic_ids: list[str] = Field(min_length=1)
    source_doc_ids: list[str] = Field(min_length=1)
    aspect_kinds: list[str] = Field(default_factory=list)
    aspect_labels: list[str] = Field(default_factory=list)


class ProfileAspectDefinition(BaseModel):
    """Profile 允许使用的一种证据方面及其领域重要度。"""

    kind: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=500)
    importance: float = Field(ge=0, le=1)


class ProfileAspectSchema(BaseModel):
    """冻结 Profile 派生出的受控证据方面集合。"""

    schema_version: Literal["profile-aspect-schema-v1"] = "profile-aspect-schema-v1"
    definitions: list[ProfileAspectDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_kinds(self) -> ProfileAspectSchema:
        """拒绝同一 Profile 内重复的 Aspect Kind。"""

        kinds = [definition.kind for definition in self.definitions]
        if len(kinds) != len(set(kinds)):
            raise ValueError("profile aspect kinds must be unique")
        return self


class TopicAspect(BaseModel):
    """一个 Topic 上实际存在的可复用证据方面。"""

    aspect_id: str = Field(pattern=r"^ta_[0-9a-f]{16}$")
    topic_id: str
    topic_revision_id: str = ""
    kind: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    importance: float = Field(ge=0, le=1)
    source_candidate_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_aspect_id(self) -> TopicAspect:
        """要求 Aspect ID 绑定 Topic revision、kind 和规范化名称。"""

        expected_id = (
            "ta_"
            + stable_hash(
                {
                    "topic_revision_id": self.topic_revision_id,
                    "kind": normalize_name(self.kind),
                    "label": normalize_name(self.label),
                }
            )[:16]
        )
        if self.aspect_id != expected_id:
            raise ValueError("TopicAspect ID does not match its revision and content")
        return self


class BlockAspectSupport(BaseModel):
    """一个原始 Block 对 TopicAspect 的支持关系。"""

    support_id: str = Field(pattern=r"^bas_[0-9a-f]{16}$")
    block_id: str
    topic_id: str
    topic_revision_id: str = ""
    aspect_id: str = Field(pattern=r"^ta_[0-9a-f]{16}$")
    support_kind: Literal["direct", "partial"]
    support_strength: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def validate_support_id(self) -> BlockAspectSupport:
        """要求 Support ID 绑定 Topic revision、Aspect 和 Block。"""

        expected_id = (
            "bas_"
            + stable_hash(
                {
                    "topic_revision_id": self.topic_revision_id,
                    "aspect_id": self.aspect_id,
                    "block_id": self.block_id,
                }
            )[:16]
        )
        if self.support_id != expected_id:
            raise ValueError("BlockAspectSupport ID does not match its revision")
        return self


class BlockAspectExclusion(BaseModel):
    """明确未映射到 Aspect 的 Block 及原因。"""

    exclusion_id: str = ""
    block_id: str
    topic_id: str
    topic_revision_id: str = ""
    reason_code: Literal[
        "weak_background",
        "duplicate",
        "outside_topic",
        "non_evidence",
    ] = Field(validation_alias=AliasChoices("reason_code", "classification"))
    reason: str = Field(min_length=1, max_length=600)

    @property
    def classification(self) -> str:
        """保留过渡期内部代码对 reason_code 的只读访问。"""

        return self.reason_code

    @model_validator(mode="after")
    def validate_exclusion_id(self) -> BlockAspectExclusion:
        """要求 Exclusion ID 绑定 Topic revision 与 Block；旧地图允许空值。"""

        if not self.exclusion_id:
            return self
        expected_id = (
            "bae_"
            + stable_hash(
                {
                    "topic_revision_id": self.topic_revision_id,
                    "block_id": self.block_id,
                }
            )[:16]
        )
        if self.exclusion_id != expected_id:
            raise ValueError("BlockAspectExclusion ID does not match its revision")
        return self


class TopicIdentityJudgementAudit(BaseModel):
    """A validated Topic identity judgement retained with one map version."""

    judgement_id: str
    pair_key: str
    left_node_ref: str
    right_node_ref: str
    relation: Literal["same", "different", "uncertain"]
    reason: str
    judge_contract_id: str = ""
    judge_contract_hash: str
    request_trace_id: str
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)


class TopicWorkItemDecision(BaseModel):
    """The outcome and audit references for one processed Topic work item."""

    decision_id: str
    work_item_node_ref: str
    judge_contract_id: str = ""
    judge_contract_hash: str
    target_count: int = Field(ge=0)
    same_target_refs: list[str] = Field(default_factory=list)
    selected_same_target_ref: str | None = None
    different_count: int = Field(ge=0)
    uncertain_count: int = Field(ge=0)
    outcome: Literal["attach", "create", "merge", "keep"]
    result_topic_ref: str
    operation_ids: list[str] = Field(default_factory=list)
    judgement_ids: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)


class TopicLedgerOperation(BaseModel):
    """A typed, immutable Topic Ledger mutation retained with one map version."""

    operation_id: str
    kind: Literal[
        "create_topic",
        "attach_candidate",
        "detach_candidate",
        "merge_topics",
        "retire_topic",
    ]
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    expected_revisions: list[str] = Field(default_factory=list)
    payload: dict[str, object] = Field(default_factory=dict)
    reason: str = ""
    provenance: dict[str, object] = Field(default_factory=dict)
    decision_id: str | None = None


class MapConvergenceReport(BaseModel):
    """一次 Topic 或 Group 全局固定点收敛诊断。"""

    status: Literal["converged", "converged_with_uncertain", "ready_partial", "failed"]
    passes_used: int = Field(ge=0)
    accepted_merge_count: int = Field(ge=0)
    rejected_merge_count: int = Field(ge=0)
    uncertain_pair_count: int = Field(ge=0)
    pending_candidate_count: int = Field(ge=0)
    last_pass_new_merge_count: int = Field(ge=0)
    termination_reason: str


class MapEmbeddingContract(BaseModel):
    """冻结一种地图向量输入及其供应商契约。"""

    provider: str
    model_id: str
    model_version: str
    dimension: int = Field(ge=1)
    output_type: str
    input_version: str
    normalization_version: Literal["l2-float32-v1"] = "l2-float32-v1"


class TopicIdentityJudgeContract(BaseModel):
    """冻结 Topic 身份判官的模型、Prompt、协议和参数身份。"""

    contract_id: str = Field(pattern=r"^tjc_[0-9a-f]{16}$")
    provider: str
    model_id: str
    base_url: str
    temperature: float
    thinking_enabled: bool
    prompt_version: str
    input_schema_version: Literal["topic-identity-input-v1"]
    output_schema_version: Literal["topic-identity-output-v1"]
    relation_enum_version: Literal["same-different-uncertain-v1"]
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_contract_id(self) -> TopicIdentityJudgeContract:
        """拒绝与判官契约哈希不一致的稳定 ID。"""

        if self.contract_id != f"tjc_{self.contract_hash[:16]}":
            raise ValueError("Topic identity judge contract ID does not match its hash")
        return self


class TopicIdentityCompileParameters(BaseModel):
    """冻结 Topic 身份解析的召回阈值、预算和批次语义。"""

    similarity_threshold: float = Field(ge=-1, le=1)
    max_target_pairs_per_work_item: int = Field(ge=1)
    max_judge_total_tokens_per_work_item: int = Field(ge=1)
    max_pairs_per_request: int = Field(ge=1)
    max_input_tokens_per_request: int = Field(ge=1)
    max_output_tokens_per_request: int = Field(ge=1)
    output_tokens_per_pair_reserve: int = Field(ge=1)


class ProfileMapCompileContract(BaseModel):
    """一次地图编译开始时冻结、并随不可变地图发布的完整契约。"""

    schema_version: Literal["profile-map-compile-contract-v1"] = "profile-map-compile-contract-v1"
    contract_id: str = Field(pattern=r"^mcc_[0-9a-f]{16}$")
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_id: str
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_aspect_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_pipeline_version: str
    name_normalization_version: str
    compiler_version: str
    candidate_prompt_version: str
    candidate_prompt_hash: str
    topic_identity_policy_version: str
    grouping_prompt_version: str
    grouping_policy_version: str
    aspect_prompt_version: str
    aspect_policy_version: str
    group_embedding: MapEmbeddingContract
    topic_embedding: MapEmbeddingContract
    aspect_embedding: MapEmbeddingContract
    topic_identity_judge: TopicIdentityJudgeContract
    topic_identity_parameters: TopicIdentityCompileParameters

    @model_validator(mode="after")
    def validate_contract_hash(self) -> ProfileMapCompileContract:
        """拒绝内容与 ID／hash 不一致的 compile contract。"""

        expected_hash = stable_hash(
            self.model_dump(
                mode="json",
                exclude={"contract_id", "contract_hash"},
            )
        )
        if self.contract_hash != expected_hash:
            raise ValueError("compile contract hash does not match its content")
        if self.contract_id != f"mcc_{expected_hash[:16]}":
            raise ValueError("compile contract ID does not match its hash")
        return self


class PersistedIdentityVector(BaseModel):
    """从不可变地图 bundle 读取并验证的身份向量条目。"""

    node_ref: str
    embedding_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_dimension: int = Field(ge=1)
    summary_input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    vector: list[float] = Field(min_length=1)


class ProfileKnowledgeMapActivePointer(BaseModel):
    """绑定 snapshot、selected Profile 与 Aspect 编译契约的当前地图指针。"""

    schema_version: Literal["profile-knowledge-map-active-v2"] = "profile-knowledge-map-active-v2"
    snapshot_id: str
    profile_id: str
    profile_hash: str
    profile_aspect_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    aspect_policy_version: str
    map_id: str = Field(pattern=r"^km_[0-9a-f]{16}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    activated_at: str


class ProfileKnowledgeMapManifest(BaseModel):
    """绑定 Profile、Preview Run 和 snapshot 的不可变知识地图清单。"""

    schema_version: Literal[
        "profile-knowledge-map-v4",
        "profile-knowledge-map-v5",
        "profile-knowledge-map-v6",
    ] = "profile-knowledge-map-v6"
    map_id: str = Field(pattern=r"^km_[0-9a-f]{16}$")
    base_map_id: str | None = Field(default=None, pattern=r"^km_[0-9a-f]{16}$")
    compilation_mode: Literal["full", "incremental", "noop"] = "full"
    delta_summary: dict[str, object] = Field(default_factory=dict)
    snapshot_id: str
    snapshot_manifest_hash: str
    profile_id: str
    profile_hash: str
    compile_contract: ProfileMapCompileContract | None = None
    compile_input_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    compile_attempt_id: str | None = Field(
        default=None,
        pattern=r"^mca_[0-9a-f]{16}$",
    )
    profile_context: str = ""
    representative_questions: list[str] = Field(default_factory=list)
    preview_run_ids: list[str] = Field(min_length=1)
    document_ids: list[str] = Field(min_length=1)
    grouping_prompt_version: str
    grouping_policy_version: str
    aspect_prompt_version: str
    aspect_policy_version: str
    profile_aspect_schema: ProfileAspectSchema
    profile_aspect_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    usage: list[dict[str, object]]
    topics: list[ProfileMapTopic]
    groups: list[ProfileMapGroup]
    topic_aspects: list[TopicAspect]
    block_aspect_supports: list[BlockAspectSupport]
    block_aspect_exclusions: list[BlockAspectExclusion] = Field(default_factory=list)
    topic_identity_judgements: list[TopicIdentityJudgementAudit] = Field(default_factory=list)
    topic_work_item_decisions: list[TopicWorkItemDecision] = Field(default_factory=list)
    topic_operations: list[TopicLedgerOperation] = Field(default_factory=list)
    topic_candidate_memberships: list[TopicCandidateMembership] = Field(default_factory=list)
    topic_convergence: MapConvergenceReport
    group_convergence: MapConvergenceReport
    group_embedding_model_id: str
    group_embedding_input_version: str
    topic_embedding_model_id: str
    topic_embedding_input_version: str
    aspect_embedding_model_id: str
    aspect_embedding_input_version: str
    vector_bundle_ref: str
    vector_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_identity_vector_bundle_ref: str | None = None
    candidate_identity_vector_bundle_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str

    @model_validator(mode="after")
    def validate_references(self) -> ProfileKnowledgeMapManifest:
        """拒绝重复身份、悬空引用和未被任何分组覆盖的 topic。"""

        if self.compile_contract is None:
            if self.schema_version == "profile-knowledge-map-v6":
                raise ValueError("knowledge map v6 requires a compile contract")
        else:
            if (
                self.compile_contract.profile_id != self.profile_id
                or self.compile_contract.profile_hash != self.profile_hash
                or self.compile_contract.profile_aspect_schema_hash
                != self.profile_aspect_schema_hash
            ):
                raise ValueError("knowledge map compile contract Profile identity is inconsistent")
            if self.compile_contract.topic_embedding.model_id != self.topic_embedding_model_id:
                raise ValueError("knowledge map compile contract Topic embedding is inconsistent")
            if self.compile_contract.group_embedding.model_id != self.group_embedding_model_id:
                raise ValueError("knowledge map compile contract Group embedding is inconsistent")
            if (
                self.compile_contract.group_embedding.input_version
                != self.group_embedding_input_version
            ):
                raise ValueError(
                    "knowledge map compile contract Group embedding input is inconsistent"
                )
            if self.compile_contract.aspect_embedding.model_id != self.aspect_embedding_model_id:
                raise ValueError("knowledge map compile contract Aspect embedding is inconsistent")
            if (
                self.compile_contract.aspect_embedding.input_version
                != self.aspect_embedding_input_version
            ):
                raise ValueError(
                    "knowledge map compile contract Aspect embedding input is inconsistent"
                )
        if self.schema_version == "profile-knowledge-map-v6" and (
            self.compile_input_key is None or self.compile_attempt_id is None
        ):
            raise ValueError("knowledge map v6 requires compile attempt identity")
        if self.schema_version == "profile-knowledge-map-v6" and (
            self.candidate_identity_vector_bundle_ref is None
            or self.candidate_identity_vector_bundle_hash is None
        ):
            raise ValueError("knowledge map v6 requires a candidate identity vector bundle")
        if self.schema_version == "profile-knowledge-map-v6" and self.compile_contract:
            judge_contract = self.compile_contract.topic_identity_judge
            if any(
                judgement.judge_contract_id != judge_contract.contract_id
                or judgement.judge_contract_hash != judge_contract.contract_hash
                for judgement in self.topic_identity_judgements
            ):
                raise ValueError("knowledge map judgement contract identity is inconsistent")
            if any(
                decision.judge_contract_id != judge_contract.contract_id
                or decision.judge_contract_hash != judge_contract.contract_hash
                for decision in self.topic_work_item_decisions
            ):
                raise ValueError("knowledge map decision contract identity is inconsistent")

        judgement_ids = [judgement.judgement_id for judgement in self.topic_identity_judgements]
        decision_ids = [decision.decision_id for decision in self.topic_work_item_decisions]
        operation_ids = [operation.operation_id for operation in self.topic_operations]
        if len(judgement_ids) != len(set(judgement_ids)):
            raise ValueError("knowledge map judgement IDs must be unique")
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("knowledge map decision IDs must be unique")
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("knowledge map operation IDs must be unique")
        if self.schema_version == "profile-knowledge-map-v6" and any(
            not operation.reason or not operation.provenance for operation in self.topic_operations
        ):
            raise ValueError("knowledge map v6 operations require reason and provenance")
        judgement_id_set = set(judgement_ids)
        decision_id_set = set(decision_ids)
        operation_id_set = set(operation_ids)
        for decision in self.topic_work_item_decisions:
            if not set(decision.judgement_ids).issubset(judgement_id_set):
                raise ValueError("knowledge map decision references an unknown judgement")
            if not set(decision.operation_ids).issubset(operation_id_set):
                raise ValueError("knowledge map decision references an unknown operation")
        for operation in self.topic_operations:
            if operation.decision_id is not None and operation.decision_id not in decision_id_set:
                raise ValueError("knowledge map operation references an unknown decision")

        topic_ids = [topic.topic_id for topic in self.topics]
        candidate_refs = [
            (source.extraction_run_id, source.candidate_id)
            for topic in self.topics
            for source in topic.sources
        ]
        membership_refs = [
            (
                membership.topic_lineage_id,
                membership.extraction_run_id,
                membership.candidate_id,
            )
            for membership in self.topic_candidate_memberships
        ]
        group_ids = [group.group_id for group in self.groups]
        aspect_ids = [aspect.aspect_id for aspect in self.topic_aspects]
        if len(topic_ids) != len(set(topic_ids)):
            raise ValueError("knowledge map topic IDs must be unique")
        if len(candidate_refs) != len(set(candidate_refs)):
            raise ValueError("knowledge map candidate references must be unique")
        if len(membership_refs) != len(set(membership_refs)):
            raise ValueError("knowledge map Topic memberships must be unique")
        expected_membership_refs = {
            (
                topic.topic_id,
                source.extraction_run_id,
                source.candidate_id,
            )
            for topic in self.topics
            for source in topic.sources
        }
        if (
            self.schema_version == "profile-knowledge-map-v6"
            and set(membership_refs) != expected_membership_refs
        ):
            raise ValueError("knowledge map Topic memberships do not match Topic sources")
        if len(group_ids) != len(set(group_ids)):
            raise ValueError("knowledge map group IDs must be unique")
        if len(aspect_ids) != len(set(aspect_ids)):
            raise ValueError("knowledge map aspect IDs must be unique")

        topic_by_id = {topic.topic_id: topic for topic in self.topics}
        for topic in self.topics:
            source_doc_ids = {source.doc_id for source in topic.sources}
            if set(topic.source_doc_ids) != source_doc_ids:
                raise ValueError("knowledge map topic source documents are inconsistent")
            source_block_ids = {
                block_id for source in topic.sources for block_id in source.source_block_ids
            }
            if set(topic.source_block_ids) != source_block_ids:
                raise ValueError("knowledge map topic source blocks are inconsistent")
            source_channel_ids = {
                channel_id for source in topic.sources for channel_id in source.channel_ids
            }
            if set(topic.channel_ids) != source_channel_ids:
                raise ValueError("knowledge map topic channel provenance is inconsistent")
            if self.schema_version == "profile-knowledge-map-v6":
                source_refs = {
                    (source.extraction_run_id, source.candidate_id) for source in topic.sources
                }
                summary_sources_by_text = {
                    summary.text: summary for summary in topic.summary_sources
                }
                if set(summary_sources_by_text) != set(topic.summaries):
                    raise ValueError("knowledge map Topic summary sources are incomplete")
                for summary_source in topic.summary_sources:
                    summary_refs = {
                        (
                            candidate_ref.extraction_run_id,
                            candidate_ref.candidate_id,
                        )
                        for candidate_ref in summary_source.source_candidate_refs
                    }
                    if not summary_refs.issubset(source_refs):
                        raise ValueError(
                            "knowledge map Topic summary references a non-member candidate"
                        )
        referenced_topic_ids: set[str] = set()
        for group in self.groups:
            unknown = [
                topic_id for topic_id in group.child_topic_ids if topic_id not in topic_by_id
            ]
            if unknown:
                raise ValueError(
                    f"knowledge map group references unknown topics: {', '.join(unknown)}"
                )
            expected_doc_ids = {
                doc_id
                for topic_id in group.child_topic_ids
                for doc_id in topic_by_id[topic_id].source_doc_ids
            }
            if set(group.source_doc_ids) != expected_doc_ids:
                raise ValueError("knowledge map group source documents do not match child topics")
            child_aspect_kinds = {
                aspect.kind
                for aspect in self.topic_aspects
                if aspect.topic_id in set(group.child_topic_ids)
            }
            if not set(group.aspect_kinds).issubset(child_aspect_kinds):
                raise ValueError("knowledge map group aspect kinds do not match child topics")
            referenced_topic_ids.update(group.child_topic_ids)

        if referenced_topic_ids != set(topic_ids):
            missing = sorted(set(topic_ids) - referenced_topic_ids)
            raise ValueError(f"knowledge map topics are orphaned: {', '.join(missing)}")
        if set(self.document_ids) != {
            doc_id for topic in self.topics for doc_id in topic.source_doc_ids
        }:
            raise ValueError("knowledge map document scope does not match topic documents")
        allowed_aspect_kinds = {
            definition.kind for definition in self.profile_aspect_schema.definitions
        }
        topic_by_aspect_id = {aspect.aspect_id: aspect.topic_id for aspect in self.topic_aspects}
        for aspect in self.topic_aspects:
            if aspect.topic_id not in topic_by_id:
                raise ValueError("knowledge map aspect references an unknown topic")
            if (
                self.schema_version == "profile-knowledge-map-v6"
                and aspect.topic_revision_id != topic_by_id[aspect.topic_id].revision_id
            ):
                raise ValueError("knowledge map aspect does not bind the current Topic revision")
            if aspect.kind not in allowed_aspect_kinds:
                raise ValueError("knowledge map aspect kind is not allowed by Profile schema")
            if not set(aspect.source_candidate_ids).issubset(
                {source.candidate_id for source in topic_by_id[aspect.topic_id].sources}
            ):
                raise ValueError("knowledge map aspect source candidates are inconsistent")
        aspect_support_ids = [support.support_id for support in self.block_aspect_supports]
        if len(aspect_support_ids) != len(set(aspect_support_ids)):
            raise ValueError("knowledge map aspect support IDs must be unique")
        supported_block_keys: set[tuple[str, str]] = set()
        for support in self.block_aspect_supports:
            expected_topic_id = topic_by_aspect_id.get(support.aspect_id)
            if expected_topic_id is None or expected_topic_id != support.topic_id:
                raise ValueError("knowledge map aspect support references an invalid aspect")
            topic = topic_by_id[support.topic_id]
            if (
                self.schema_version == "profile-knowledge-map-v6"
                and support.topic_revision_id != topic.revision_id
            ):
                raise ValueError(
                    "knowledge map aspect support does not bind the current Topic revision"
                )
            if support.block_id not in topic.source_block_ids:
                raise ValueError("knowledge map aspect support block is not in the topic")
            supported_block_keys.add((support.topic_id, support.block_id))
        excluded_block_keys: set[tuple[str, str]] = set()
        exclusion_ids = [
            exclusion.exclusion_id
            for exclusion in self.block_aspect_exclusions
            if exclusion.exclusion_id
        ]
        if len(exclusion_ids) != len(set(exclusion_ids)):
            raise ValueError("knowledge map exclusion IDs must be unique")
        for exclusion in self.block_aspect_exclusions:
            if exclusion.topic_id not in topic_by_id:
                raise ValueError("knowledge map exclusion references an unknown topic")
            topic = topic_by_id[exclusion.topic_id]
            if (
                self.schema_version == "profile-knowledge-map-v6"
                and exclusion.topic_revision_id != topic.revision_id
            ):
                raise ValueError("knowledge map exclusion does not bind the current Topic revision")
            if self.schema_version == "profile-knowledge-map-v6" and not exclusion.exclusion_id:
                raise ValueError("knowledge map v6 exclusion requires an exclusion ID")
            if (
                self.schema_version == "profile-knowledge-map-v6"
                and exclusion.classification == "missing_aspect"
            ):
                raise ValueError("published knowledge maps cannot use missing_aspect exclusions")
            if exclusion.block_id not in topic.source_block_ids:
                raise ValueError("knowledge map exclusion block is not in the topic")
            key = (exclusion.topic_id, exclusion.block_id)
            if key in excluded_block_keys:
                raise ValueError("knowledge map exclusion must be unique")
            excluded_block_keys.add(key)
        if supported_block_keys & excluded_block_keys:
            raise ValueError("knowledge map Block cannot be both supported and excluded")
        missing_support = (
            {
                (topic.topic_id, block_id)
                for topic in self.topics
                for block_id in topic.source_block_ids
            }
            - supported_block_keys
            - excluded_block_keys
        )
        if missing_support:
            raise ValueError("knowledge map blocks are neither aspect-supported nor excluded")
        for aspect_id in aspect_ids:
            if not any(support.aspect_id == aspect_id for support in self.block_aspect_supports):
                raise ValueError("knowledge map aspect has no block support")
        return self


class ProfileKnowledgeMapResult(BaseModel):
    """一次知识地图构建结果。"""

    manifest: ProfileKnowledgeMapManifest
    group_vectors: dict[str, list[float]] = Field(default_factory=dict)
    topic_vectors: dict[str, list[float]] = Field(default_factory=dict)
    aspect_vectors: dict[str, list[float]] = Field(default_factory=dict)
    candidate_identity_vectors: dict[str, PersistedIdentityVector] = Field(default_factory=dict)
    topic_identity_vectors: dict[str, PersistedIdentityVector] = Field(default_factory=dict)
    reused: bool = False


class SearchWikiTopicV2Result(BaseModel):
    """`search_wiki_topic_v2` 对主 Agent 暴露的最小证据结果。"""

    topic: str
    blocks: list[RetrievalBlock]
    empty_reason: Literal["no_match", "no_selected_evidence"] | None = None


class SearchWikiTopicV2GroupMatch(BaseModel):
    """内部诊断中的一个 group 排序结果。"""

    group_id: str
    score: float


class SearchWikiTopicV2TopicMatch(BaseModel):
    """内部诊断中的一个 child topic 排序结果。"""

    topic_id: str
    score: float


class SearchWikiTopicV2AspectMatch(BaseModel):
    """内部诊断中的一个 TopicAspect 排序结果。"""

    aspect_id: str
    topic_id: str
    query_score: float
    priority: float


class SearchWikiTopicV2GroupSelection(BaseModel):
    """内部诊断中模型对 group 的相关性判断。"""

    group_id: str
    relevance: Literal["direct", "partial"]
    supported_topic_ids: list[str] = Field(default_factory=list)
    reason: str


class SearchWikiTopicV2EvidenceSelection(BaseModel):
    """内部诊断中一条候选 block 的筛选结论。"""

    block_id: str
    relevance: Literal["direct", "partial"]
    aspect: str
    reason: str
    aspect_ids: list[str] = Field(default_factory=list)
    duplicate_of: str | None = None
    conflicts_with: list[str] = Field(default_factory=list)


class SearchWikiTopicV2AspectCoverage(BaseModel):
    """内部诊断中一个已选 Aspect 的最终证据覆盖状态。"""

    aspect_id: str
    status: Literal["covered", "partial", "missing"]


class SearchWikiTopicV2Execution(BaseModel):
    """搜索结果的内部诊断，不进入主 Agent payload。"""

    result: SearchWikiTopicV2Result
    matched_group_ids: list[str]
    matched_topic_ids: list[str]
    group_matches: list[SearchWikiTopicV2GroupMatch] = Field(default_factory=list)
    topic_matches: list[SearchWikiTopicV2TopicMatch] = Field(default_factory=list)
    aspect_matches: list[SearchWikiTopicV2AspectMatch] = Field(default_factory=list)
    group_selections: list[SearchWikiTopicV2GroupSelection] = Field(default_factory=list)
    selected_aspect_ids: list[str] = Field(default_factory=list)
    eligible_group_count: int = Field(default=0, ge=0)
    group_candidate_count: int = Field(default=0, ge=0)
    eligible_topic_count: int = Field(default=0, ge=0)
    candidate_block_ids: list[str] = Field(default_factory=list)
    candidate_block_count: int
    selected_block_count: int
    selected_evidence: list[SearchWikiTopicV2EvidenceSelection] = Field(default_factory=list)
    aspect_coverage: list[SearchWikiTopicV2AspectCoverage] = Field(default_factory=list)
    usage: dict[str, object]
