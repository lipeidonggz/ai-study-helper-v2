from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.wiki.profile_knowledge_map as profile_knowledge_map_module
from app.agent.tool_adapter import ToolUnavailableError
from app.main import create_app
from app.profile_extraction.util import estimate_tokens, json_dumps
from app.providers.chat import (
    ChatCompletionResult,
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
)
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.models import RetrievalBlock, RetrievalSectionContext
from app.retrieval.service import RetrievalReadService
from app.storage.database import StorageDatabase
from app.storage.models import utc_now
from app.storage.profile_extraction_models import (
    ExtractionProfileRow,
    KnowledgeBaseProfileSelectionRow,
    PreviewCandidateRow,
    ProfilePreviewRunRow,
)
from app.wiki.profile_knowledge_map import (
    ProfileKnowledgeMapError,
    ProfileKnowledgeMapService,
    SearchWikiTopicV2Service,
    _group_candidate_edges,
)
from app.wiki.profile_knowledge_map_models import (
    BlockAspectExclusion,
    BlockAspectSupport,
    MapConvergenceReport,
    ProfileKnowledgeMapManifest,
    ProfileKnowledgeMapResult,
    ProfileMapGroup,
    ProfileMapTopic,
    ProfileMapTopicSource,
    TopicAspect,
)
from app.wiki.tool_adapter import WIKI_TOOL_CONTRACT_VERSION, WikiToolAdapter, wiki_tool_descriptors

TOKEN = "profile-knowledge-map-test-token"


class _GroupingProvider:
    """按本地、全局和证据筛选三阶段返回稳定测试数据。"""

    def __init__(
        self,
        *,
        invalid_first_grouping: bool = False,
        invalid_first_selection: bool = False,
        invalid_first_selection_unknown: bool = False,
        merge_topic_batch: bool = False,
        partial_selection: bool = False,
        retain_all_partials_in_coverage_review: bool = False,
        drop_all_aspect_audit_blocks: bool = False,
        requires_aspect_extension: bool = False,
        omits_first_aspect_extension_topic_once: bool = False,
        emits_redundant_mapping_exclusions: bool = False,
        emits_out_of_scope_mapping_exclusion: bool = False,
        emits_conflict_diagnostics: bool = False,
        marks_all_aspects_missing: bool = False,
        omits_first_mapping_block_once: bool = False,
        fails_first_chat_call_retryable: bool = False,
        select_first_group_only: bool = False,
        supplement_first_unselected_group: bool = False,
        split_grouping_items: bool = False,
    ) -> None:
        self.requests: list[tuple[ChatMessage, ...]] = []
        self.invalid_first_grouping = invalid_first_grouping
        self.invalid_first_selection = invalid_first_selection
        self.invalid_first_selection_unknown = invalid_first_selection_unknown
        self.merge_topic_batch = merge_topic_batch
        self.partial_selection = partial_selection
        self.retain_all_partials_in_coverage_review = retain_all_partials_in_coverage_review
        self.drop_all_aspect_audit_blocks = drop_all_aspect_audit_blocks
        self.requires_aspect_extension = requires_aspect_extension
        self.omits_first_aspect_extension_topic_once = omits_first_aspect_extension_topic_once
        self.emits_redundant_mapping_exclusions = emits_redundant_mapping_exclusions
        self.emits_out_of_scope_mapping_exclusion = emits_out_of_scope_mapping_exclusion
        self.emits_conflict_diagnostics = emits_conflict_diagnostics
        self.marks_all_aspects_missing = marks_all_aspects_missing
        self.omits_first_mapping_block_once = omits_first_mapping_block_once
        self.fails_first_chat_call_retryable = fails_first_chat_call_retryable
        self.select_first_group_only = select_first_group_only
        self.supplement_first_unselected_group = supplement_first_unselected_group
        self.split_grouping_items = split_grouping_items
        self.retryable_failure_count = 0
        self.topic_identity_attempts = 0
        self.grouping_attempts = 0
        self.group_selection_attempts = 0
        self.group_coverage_audit_attempts = 0
        self.selection_attempts = 0
        self.aspect_mapping_attempts = 0
        self.aspect_extension_attempts = 0
        self._profile = ChatProviderProfile(
            provider="fake",
            model_id="fake-profile-knowledge-map",
            base_url="fake://chat",
            temperature=0.0,
            max_output_tokens=12_000,
            thinking_enabled=False,
        )

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
        if self.fails_first_chat_call_retryable and self.retryable_failure_count == 0:
            self.retryable_failure_count += 1
            raise ChatProviderError(
                code="chat_upstream_unavailable",
                message="temporary provider failure",
                retryable=True,
            )
        system = messages[0].content
        payload_message = next(
            message
            for message in reversed(messages)
            if message.role == "user" and message.content.lstrip().startswith("{")
        )
        payload = json.loads(payload_message.content)
        if "知识库 Topic 身份判定器" in system:
            self.topic_identity_attempts += 1
            content = {
                "judgements": [
                    {
                        "left_node_ref": pair["left_node_ref"],
                        "right_node_ref": pair["right_node_ref"],
                        "relation": "same" if self.merge_topic_batch else "different",
                        "reason": "测试判官按固定开关决定 Topic 身份。",
                    }
                    for pair in payload["pairs"]
                ]
            }
        elif "Wiki Topic 的证据方面编译器" in system:
            topics = []
            for topic in payload["topics"]:
                topics.append(
                    {
                        "topic_id": topic["topic_id"],
                        "aspects": [
                            {
                                "kind": "definition",
                                "label": f"{topic['title']}的核心定义",
                                "description": f"{topic['title']}的文档内定义。",
                                "importance": 0.8,
                                "source_candidate_ids": [
                                    candidate["candidate_id"]
                                    for candidate in topic["source_candidates"]
                                ],
                            }
                        ],
                    }
                )
            content = {"topics": topics}
        elif "Wiki Topic Block 的 Aspect 映射器" in system:
            self.aspect_mapping_attempts += 1
            supports = []
            exclusions = []
            omitted_block_id = None
            if self.omits_first_mapping_block_once and self.aspect_mapping_attempts == 1:
                omitted_block_id = payload["topics"][-1]["blocks"][-1]["block_id"]
            if self.requires_aspect_extension and self.aspect_mapping_attempts == 1:
                for topic in payload["topics"]:
                    for block in topic["blocks"]:
                        if block["block_id"] == omitted_block_id:
                            continue
                        exclusions.append(
                            {
                                "block_id": block["block_id"],
                                "topic_id": topic["topic_id"],
                                "classification": "missing_aspect",
                                "reason": "现有 catalog 没有覆盖该 Block 的独立机制。",
                            }
                        )
            else:
                for topic in payload["topics"]:
                    aspect_id = topic["aspects"][0]["aspect_id"]
                    for block in topic["blocks"]:
                        if block["block_id"] == omitted_block_id:
                            continue
                        supports.append(
                            {
                                "block_id": block["block_id"],
                                "topic_id": topic["topic_id"],
                                "aspect_id": aspect_id,
                                "support_kind": "direct",
                                "support_strength": 0.9,
                                "reason": "测试证据直接支持该 Aspect。",
                            }
                        )
                        if self.emits_redundant_mapping_exclusions:
                            exclusions.append(
                                {
                                    "block_id": block["block_id"],
                                    "topic_id": topic["topic_id"],
                                    "classification": "duplicate",
                                    "reason": "模型重复给出的 block-level exclusion。",
                                }
                            )
                if self.emits_out_of_scope_mapping_exclusion:
                    first_topic = payload["topics"][0]
                    exclusions.append(
                        {
                            "block_id": "block_not_in_current_mapping_batch",
                            "topic_id": first_topic["topic_id"],
                            "classification": "weak_background",
                            "reason": "模型虚构的越界引用。",
                        }
                    )
            content = {"supports": supports, "exclusions": exclusions}
        elif "Wiki TopicAspect catalog 的受控扩展器" in system:
            self.aspect_extension_attempts += 1
            extension_topics = [
                {
                    "topic_id": topic["topic_id"],
                    "aspects": [
                        {
                            "kind": "mechanism",
                            "label": f"{topic['title']}的具体机制",
                            "description": f"{topic['title']}的机制证据。",
                            "importance": 0.9,
                            "source_candidate_ids": sorted(
                                {
                                    candidate_id
                                    for block in topic["unclassified_blocks"]
                                    for candidate_id in block["candidate_ids"]
                                }
                            ),
                        }
                    ],
                }
                for topic in payload["topics"]
            ]
            if self.omits_first_aspect_extension_topic_once and self.aspect_extension_attempts == 1:
                extension_topics = extension_topics[:-1]
            content = {"topics": extension_topics}
        elif "Wiki 主题 group 路由器" in system:
            self.group_selection_attempts += 1
            selected_groups = [
                {
                    "group_id": group["group_id"],
                    "relevance": "direct",
                    "supported_topic_ids": [group["top_child_topics"][0]["topic_id"]],
                    "reason": "该 group 与测试主题相关。",
                }
                for group in payload["candidate_groups"]
            ]
            if self.select_first_group_only:
                preferred = next(
                    (
                        group
                        for group in payload["candidate_groups"]
                        if group["top_child_topics"][0]["title"] != "导航"
                    ),
                    payload["candidate_groups"][0],
                )
                selected_groups = [
                    group for group in selected_groups if group["group_id"] == preferred["group_id"]
                ]
            content = {
                "groups": selected_groups,
                "uncertain_pairs": [],
            }
        elif "Wiki Group 路由的覆盖缺口审查器" in system:
            self.group_coverage_audit_attempts += 1
            supplemental_groups = []
            if self.supplement_first_unselected_group:
                candidate = payload["unselected_candidate_groups"][0]
                supplemental_groups.append(
                    {
                        "group_id": candidate["group_id"],
                        "new_dimension": "测试中的独立检索维度",
                        "supported_topic_ids": [candidate["top_child_topics"][0]["topic_id"]],
                        "reason": "现有选择未覆盖该候选中的独立检索维度。",
                    }
                )
            content = {"supplemental_groups": supplemental_groups}
        elif "Wiki Aspect evidence 集合审核器" in system:
            selected = (
                []
                if self.drop_all_aspect_audit_blocks
                else [
                    {
                        "block_id": block["block_id"],
                        "relevance": "direct",
                        "supported_aspect_ids": [
                            support["aspect_id"] for support in block["aspect_supports"]
                        ],
                        "reason": "测试审核保留该 Aspect 证据。",
                        "duplicate_of": None,
                        "conflicts_with": [],
                    }
                    for block in payload["candidate_blocks"]
                    if "导航" not in block["text"]
                ]
            )
            coverage_by_aspect_id = {
                aspect["aspect_id"]: "covered" for aspect in payload["aspects"]
            }
            if self.marks_all_aspects_missing:
                coverage_by_aspect_id = {
                    aspect["aspect_id"]: "missing" for aspect in payload["aspects"]
                }
            for block in payload["candidate_blocks"]:
                if "导航" in block["text"]:
                    for support in block["aspect_supports"]:
                        coverage_by_aspect_id[support["aspect_id"]] = "missing"
            if self.emits_conflict_diagnostics and len(selected) >= 2:
                selected[0]["conflicts_with"] = [selected[1]["block_id"]]
                selected[1]["duplicate_of"] = selected[0]["block_id"]
            content = {
                "selected_blocks": selected,
                "coverage": [
                    {
                        "aspect_id": aspect["aspect_id"],
                        "status": coverage_by_aspect_id[aspect["aspect_id"]],
                    }
                    for aspect in payload["aspects"]
                ],
            }
        elif "Wiki 主题证据筛选器" in system:
            self.selection_attempts += 1
            if self.invalid_first_selection and self.selection_attempts == 1:
                content = {"chosen": []}
            elif self.invalid_first_selection_unknown and self.selection_attempts == 1:
                content = {
                    "selected": [
                        {
                            "block_id": "block_not_in_candidates",
                            "relevance": "direct",
                            "aspect": "invalid",
                            "reason": "错误 ID 应触发一次结构化修复。",
                        }
                    ]
                }
            else:
                eligible_blocks = [
                    block for block in payload["candidate_blocks"] if "导航" not in block["text"]
                ]
                selected = []
                for index, block in enumerate(eligible_blocks):
                    relevance = "partial" if self.partial_selection and index > 0 else "direct"
                    selected.append(
                        {
                            "block_id": block["block_id"],
                            "relevance": relevance,
                            "aspect": f"aspect:{block['block_id']}",
                            "reason": "该块说明 Agent 控制边界。",
                        }
                    )
                content = {"selected": selected}
        elif "Wiki 主题证据覆盖与去重审核器" in system:
            review_candidates = [
                *payload["partial_evidence"],
                *payload["supplement_candidates"],
            ]
            content = {
                "selected": (
                    [
                        {
                            "block_id": block["block_id"],
                            "relevance": "partial",
                            "aspect": f"reviewed-aspect:{block['block_id']}",
                            "reason": "测试覆盖审核保留该局部证据。",
                        }
                        for block in review_candidates
                    ]
                    if self.retain_all_partials_in_coverage_review
                    else []
                )
            }
        elif "全局合并判定器" in system:
            self.grouping_attempts += 1
            cards = payload["groups"]
            if self.merge_topic_batch and len(cards) > 1:
                content = {
                    "actions": [
                        {
                            "group_ids": [card["group_id"] for card in cards],
                            "decision": "merge",
                            "label": "Agent 隔离与控制边界",
                            "aliases": ["AI Agent 边界"],
                            "summary": "汇总 Agent 的隔离与控制边界。",
                            "retrieval_questions": ["Agent 还有哪些隔离与控制边界？"],
                            "reason": "测试要求把同一检索区域合并。",
                        }
                    ],
                    "uncertain_pairs": [],
                }
            else:
                content = {
                    "actions": [
                        {
                            "group_ids": [card["group_id"]],
                            "decision": "keep",
                            "reason": "测试默认保留 Group。",
                        }
                        for card in cards
                    ],
                    "uncertain_pairs": [],
                }
        elif "Wiki 知识地图主题分组器" in system:
            self.grouping_attempts += 1
            item_field = "topic_id" if "topic_id" in payload["items"][0] else "group_id"
            item_ids = [item[item_field] for item in payload["items"]]
            if (
                self.invalid_first_grouping
                and self.grouping_attempts == 1
                and item_field == "topic_id"
            ):
                content = {"groups": [{"group_label": "错误字段名", "item_ids": item_ids}]}
            else:
                groups = (
                    [
                        {
                            "label": f"Agent 隔离与控制边界 {index}",
                            "aliases": [f"AI Agent 边界 {index}"],
                            "summary": "汇总 Agent 的隔离与控制边界。",
                            "retrieval_questions": ["Agent 还有哪些隔离与控制边界？"],
                            "item_ids": [item_id],
                        }
                        for index, item_id in enumerate(item_ids, start=1)
                    ]
                    if self.split_grouping_items
                    else [
                        {
                            "label": "Agent 隔离与控制边界",
                            "aliases": ["AI Agent 边界"],
                            "summary": "汇总 Agent 的隔离与控制边界。",
                            "retrieval_questions": ["Agent 还有哪些隔离与控制边界？"],
                            "item_ids": item_ids,
                        }
                    ]
                )
                content = {
                    "groups": groups,
                    "uncertain_pairs": [],
                }
        else:
            raise AssertionError(f"unexpected prompt: {system[:80]}")
        return ChatCompletionResult(
            content=json.dumps(content, ensure_ascii=False),
            request_id=f"fake-{len(self.requests)}",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=100,
            output_tokens=50,
        )


class _TopicIdentityProvider(_GroupingProvider):
    """按标题对返回固定 Topic 关系，并保留每次成对判断输入。"""

    def __init__(
        self,
        relation_by_titles: dict[frozenset[str], str] | None = None,
    ) -> None:
        super().__init__()
        self.relation_by_titles = relation_by_titles or {}
        self.identity_pairs: list[tuple[str, str, str, str]] = []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_output_tokens: int | None = None,
    ) -> ChatCompletionResult:
        if not messages or "知识库 Topic 身份判定器" not in messages[0].content:
            return await super().complete(messages, max_output_tokens=max_output_tokens)
        self.requests.append(tuple(messages))
        payload = json.loads(messages[-1].content)
        judgements = []
        for pair in payload["pairs"]:
            left_title = str(pair["left"]["title"])
            right_title = str(pair["right"]["title"])
            relation = self.relation_by_titles.get(
                frozenset((left_title, right_title)),
                "same" if left_title == right_title else "different",
            )
            self.identity_pairs.append(
                (
                    str(pair["left_node_ref"]),
                    str(pair["right_node_ref"]),
                    left_title,
                    right_title,
                )
            )
            judgements.append(
                {
                    "left_node_ref": pair["left_node_ref"],
                    "right_node_ref": pair["right_node_ref"],
                    "relation": relation,
                    "reason": "测试按标题对返回固定关系。",
                }
            )
        content = json.dumps({"judgements": judgements}, ensure_ascii=False)
        return ChatCompletionResult(
            content=content,
            request_id=f"identity-{len(self.identity_pairs)}",
            actual_model_id=self.profile.model_id,
            finish_reason="stop",
            input_tokens=100,
            output_tokens=50,
        )


def _html() -> bytes:
    sections = [
        ("文件挂载模式", "文件挂载模式限制 Agent 能读取、修改和删除的工作区范围。"),
        ("凭证隔离", "凭证隔离让宿主机钥匙串中的凭证不进入执行环境。"),
        ("工具输出检查", "工具输出检查在外部内容进入模型上下文前识别风险。"),
        ("导航", "导航和页脚是无关元数据，不属于知识证据。"),
    ]
    body = "".join(f"<h2>{title}</h2><p>{text}</p>" for title, text in sections)
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        "<title>Agent 边界</title></head><body><article>"
        f"<h1>Agent 边界</h1>{body}</article></body></html>"
    ).encode()


async def _wait_for_task(client: httpx.AsyncClient, task_id: str) -> dict[str, object]:
    for _ in range(300):
        response = await client.get(
            f"/api/tasks/{task_id}",
            headers={"x-launch-token": TOKEN},
        )
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "canceled", "interrupted"}:
            return task
        await asyncio.sleep(0.02)
    raise AssertionError("ingestion task did not finish")


def _publish_snapshot(tmp_path: Path) -> str:
    source = tmp_path / "agent-boundaries.html"
    source.write_bytes(_html())

    async def run() -> str:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/tasks/ingest",
                headers={"x-launch-token": TOKEN},
                json={"sources": [{"source_kind": "file", "source_value": str(source)}]},
            )
            created.raise_for_status()
            task = await _wait_for_task(client, created.json()["task_id"])
            assert task["status"] == "succeeded", json.dumps(task, ensure_ascii=False)
            return str(task["snapshot_id"])

    return asyncio.run(run())


def _seed_preview_run(
    tmp_path: Path,
    snapshot_id: str,
    *,
    run_id: str = "pr_knowledge_map_test",
    candidate_prefix: int = 1,
    candidate_pipeline_version: str = "profile-preview-pipeline-v9",
) -> str:
    reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    document = reader.list_documents().documents[0]
    blocks = reader.list_blocks(document.doc_id).blocks
    paragraph_blocks = [block for block in blocks if len(block.text) > 10]
    assert len(paragraph_blocks) >= 4
    profile_config = {
        "name": "测试 Profile",
        "description": "知识地图测试",
        "domain_context": "AI Agent 安全与工程",
        "primary_uses": ["学习 Agent 安全"],
        "representative_questions": ["Agent 有哪些控制边界？"],
        "extraction_channels": [],
        "exclude_criteria": [],
        "granularity": "standard",
        "additional_guidance": "",
        "language_policy": {
            "name": "preserve_source_term",
            "summary": "zh-CN",
        },
    }
    with StorageDatabase(tmp_path).session() as session, session.begin():
        session.add(
            ProfilePreviewRunRow(
                run_id=run_id,
                snapshot_id=snapshot_id,
                snapshot_manifest_hash=reader.manifest.manifest_hash,
                profile_id="profile_test",
                profile_hash="a" * 64,
                profile_config_snapshot_json=json_dumps(profile_config),
                document_scope_json=json_dumps([document.doc_id]),
                document_versions_json=json_dumps({document.doc_id: 1}),
                document_hashes_json="{}",
                candidate_pipeline_version=candidate_pipeline_version,
                name_normalization_version="nfkc-casefold-v1",
                compiler_version="test",
                prompt_version="test",
                prompt_hash="b" * 64,
                provider_profile_json="{}",
                window_policy_version="test",
                window_policy_json="{}",
                execution_policy_json="{}",
                preview_fingerprint="c" * 64,
                status="completed",
                semantic_audit_status="disabled",
            )
        )
        session.flush()
        names = [
            "文件挂载模式",
            "凭证隔离",
            "工具输出检查",
            "导航",
        ]
        for ordinal, (name, block) in enumerate(zip(names, paragraph_blocks[:4], strict=True)):
            candidate_id = f"pc_{ordinal + candidate_prefix:016x}"
            session.add(
                PreviewCandidateRow(
                    candidate_id=candidate_id,
                    run_id=run_id,
                    doc_id=document.doc_id,
                    doc_version=1,
                    ordinal=ordinal,
                    name=name,
                    normalized_name=name.casefold(),
                    summaries_json=json_dumps([f"{name} 的文档内说明。"]),
                    source_block_ids_json=json_dumps([block.block_id]),
                    evidence_blocks_json=json_dumps([block.model_dump(mode="json")]),
                    channel_ids_json=json_dumps(["ec_1111111111111111"]),
                    content_hash=hashlib.sha256(name.encode()).hexdigest(),
                )
            )
    return run_id


def _seed_selected_profile(
    tmp_path: Path,
    *,
    profile_id: str = "profile_test",
    profile_hash: str = "a" * 64,
) -> None:
    now = utc_now()
    with StorageDatabase(tmp_path).session() as session, session.begin():
        session.merge(
            ExtractionProfileRow(
                profile_id=profile_id,
                name="测试 Profile",
                description="知识地图测试",
                domain_context="AI Agent 安全与工程",
                primary_uses_json=json_dumps(["学习 Agent 安全"]),
                representative_questions_json=json_dumps(["Agent 有哪些控制边界？"]),
                extraction_channels_json="[]",
                exclude_criteria_json="[]",
                granularity="standard",
                additional_guidance="",
                created_from_profile_id=None,
                is_system=False,
                status="frozen",
                profile_hash=profile_hash,
                created_at=now,
                updated_at=now,
                frozen_at=now,
            )
        )
        session.merge(
            KnowledgeBaseProfileSelectionRow(
                knowledge_base_id="default",
                selected_profile_id=profile_id,
                selected_profile_hash=profile_hash,
                selected_at=now,
            )
        )


def test_search_wiki_topic_v2_builds_map_and_returns_bounded_evidence(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    progress_events: list[dict[str, object]] = []
    provider = _GroupingProvider(
        invalid_first_grouping=True,
        invalid_first_selection_unknown=True,
    )
    embedding = FakeEmbeddingProvider()

    async def run() -> None:
        built = await ProfileKnowledgeMapService(
            tmp_path,
            progress_callback=progress_events.append,
        ).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=embedding,
        )
        assert len(built.manifest.document_ids) == 1
        assert {"load", "topic_ledger", "aspects", "groups", "publish", "complete"}.issubset(
            {event["stage"] for event in progress_events}
        )
        assert built.manifest.topics
        assert built.manifest.groups
        assert built.manifest.profile_context == "AI Agent 安全与工程"
        assert built.manifest.representative_questions == ["Agent 有哪些控制边界？"]
        contract = built.manifest.compile_contract
        assert contract is not None
        assert contract.contract_id == f"mcc_{contract.contract_hash[:16]}"
        assert contract.candidate_pipeline_version == "profile-preview-pipeline-v9"
        assert contract.topic_identity_parameters.similarity_threshold == 0.78
        assert contract.topic_identity_parameters.max_target_pairs_per_work_item == 200
        assert contract.topic_embedding.dimension == 1024
        assert built.manifest.compile_input_key is not None
        assert built.manifest.compile_attempt_id is not None
        judge_contract = contract.topic_identity_judge
        assert all(
            judgement.judge_contract_id == judge_contract.contract_id
            for judgement in built.manifest.topic_identity_judgements
        )
        assert all(
            decision.judge_contract_id == judge_contract.contract_id
            for decision in built.manifest.topic_work_item_decisions
        )
        topic_revision_by_id = {
            topic.topic_id: topic.revision_id for topic in built.manifest.topics
        }
        assert all(
            aspect.topic_revision_id == topic_revision_by_id[aspect.topic_id]
            for aspect in built.manifest.topic_aspects
        )
        assert all(
            support.topic_revision_id == topic_revision_by_id[support.topic_id]
            for support in built.manifest.block_aspect_supports
        )
        assert all(
            exclusion.topic_revision_id == topic_revision_by_id[exclusion.topic_id]
            for exclusion in built.manifest.block_aspect_exclusions
        )
        assert built.manifest.topic_candidate_memberships
        assert {
            (
                membership.topic_lineage_id,
                membership.extraction_run_id,
                membership.candidate_id,
            )
            for membership in built.manifest.topic_candidate_memberships
        } == {
            (
                topic.topic_id,
                source.extraction_run_id,
                source.candidate_id,
            )
            for topic in built.manifest.topics
            for source in topic.sources
        }
        assert all(
            set(summary_source.text for summary_source in topic.summary_sources)
            == set(topic.summaries)
            for topic in built.manifest.topics
        )
        assert all(
            operation.reason and operation.provenance
            for operation in built.manifest.topic_operations
        )
        assert (
            built.manifest.candidate_identity_vector_bundle_ref != built.manifest.vector_bundle_ref
        )
        grouping_requests = [
            request
            for request in provider.requests
            if "Wiki 知识地图主题分组器" in request[0].content
        ]
        topic_cards = json.loads(grouping_requests[0][-1].content)["items"]
        assert {card["title"] for card in topic_cards} == {
            "文件挂载模式",
            "凭证隔离",
            "工具输出检查",
            "导航",
        }
        loaded = ProfileKnowledgeMapService(tmp_path).load(
            snapshot_id,
            built.manifest.map_id,
        )
        assert loaded.manifest.manifest_hash == built.manifest.manifest_hash
        assert set(loaded.topic_identity_vectors) == {
            f"topic:{topic.topic_id}:{topic.revision_id}" for topic in loaded.manifest.topics
        }
        assert set(loaded.candidate_identity_vectors) == {
            f"candidate:{source.extraction_run_id}:{source.candidate_id}"
            for topic in loaded.manifest.topics
            for source in topic.sources
        }
        assert all(
            entry.vector_dimension == 1024
            and entry.vector_hash
            and entry.summary_input_hash
            and entry.embedding_contract_hash
            for entry in [
                *loaded.topic_identity_vectors.values(),
                *loaded.candidate_identity_vectors.values(),
            ]
        )
        assert len(built.group_vectors) == len(built.manifest.groups)
        assert built.topic_vectors
        assert built.aspect_vectors
        assert built.manifest.topic_aspects
        assert built.manifest.block_aspect_supports
        document_embedding_calls = len(embedding.calls)
        query_embedding_calls = len(embedding.query_calls)

        execution = await SearchWikiTopicV2Service(tmp_path).search(
            snapshot_id=snapshot_id,
            map_id=built.manifest.map_id,
            topic="AI Agent 隔离与控制边界",
            embedding_provider=embedding,
            provider=provider,
        )
        assert execution.result.topic == "AI Agent 隔离与控制边界"
        assert execution.result.blocks
        assert all("导航" not in block.text for block in execution.result.blocks)
        assert execution.candidate_block_count <= 256
        assert execution.selected_block_count <= 96
        assert 0 < len(execution.group_matches) <= len(built.manifest.groups)
        assert execution.candidate_block_ids
        assert len(execution.selected_evidence) == execution.selected_block_count
        assert {item.relevance for item in execution.selected_evidence} == {"direct"}
        assert execution.usage["aspect_evidence_audit_attempts"] == 1
        assert execution.usage["selected_aspect_count"] > 0
        assert execution.usage["block_embedding_tokens"] == 0
        assert execution.usage["block_vector_provider_calls"] == 0
        assert execution.usage["persisted_block_vector_count"] == len(execution.candidate_block_ids)
        assert provider.group_selection_attempts == 1
        assert provider.group_coverage_audit_attempts == 0
        assert execution.usage["coverage_audit_attempts"] == 0
        assert execution.usage["supplemental_group_ids"] == []
        assert any(item.get("code") == "topic_grouping_retry" for item in built.manifest.usage)
        assert len(embedding.calls) == document_embedding_calls
        assert len(embedding.query_calls) == query_embedding_calls + 1

        forced_group_id = execution.group_matches[0].group_id
        forced_execution = await SearchWikiTopicV2Service(tmp_path).search(
            snapshot_id=snapshot_id,
            map_id=built.manifest.map_id,
            topic="AI Agent 隔离与控制边界",
            embedding_provider=embedding,
            provider=provider,
            forced_group_ids=[forced_group_id],
        )
        assert provider.group_selection_attempts == 1
        assert forced_execution.matched_group_ids == [forced_group_id]
        assert forced_execution.usage["group_selection_mode"] == "forced"
        assert forced_execution.usage["group_selection_attempts"] == 0
        assert forced_execution.usage["forced_group_ids"] == [forced_group_id]
        assert forced_execution.usage["coverage_audit_attempts"] == 0
        assert len(embedding.query_calls) == query_embedding_calls + 2

        with pytest.raises(ProfileKnowledgeMapError) as forced_error:
            await SearchWikiTopicV2Service(tmp_path).search(
                snapshot_id=snapshot_id,
                map_id=built.manifest.map_id,
                topic="AI Agent 隔离与控制边界",
                embedding_provider=embedding,
                provider=provider,
                forced_group_ids=["wg_not_a_candidate"],
            )
        assert forced_error.value.code == "search_wiki_topic_forced_group_not_candidate"
        assert provider.group_selection_attempts == 1

        assert len(embedding.query_calls) == query_embedding_calls + 3

        adapter = WikiToolAdapter(
            tmp_path,
            snapshot_id=snapshot_id,
            embedding_provider=embedding,
            chat_provider=provider,
            knowledge_map_id=built.manifest.map_id,
        )
        tool_execution = await adapter.execute(
            "search_wiki_topic_v2",
            {"topic": "AI Agent 隔离与控制边界"},
        )
        assert tool_execution.payload["blocks"]
        assert tool_execution.diagnostics["map_id"] == built.manifest.map_id
        assert provider.group_selection_attempts == 2
        assert provider.group_coverage_audit_attempts == 0
        assert len(embedding.calls) == document_embedding_calls
        assert len(embedding.query_calls) == query_embedding_calls + 4

        invalid_manifest = built.manifest.model_dump(mode="json")
        invalid_manifest["groups"] = []
        with pytest.raises(ValidationError, match="orphaned"):
            ProfileKnowledgeMapManifest.model_validate(invalid_manifest)

    asyncio.run(run())

    descriptors = wiki_tool_descriptors()
    assert WIKI_TOOL_CONTRACT_VERSION == "wiki-tools-v2"
    assert "search_wiki_topic_v2" in {descriptor.name for descriptor in descriptors}


def test_retryable_chat_provider_error_is_retried_once(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(fails_first_chat_call_retryable=True)

    built = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    assert provider.retryable_failure_count == 1
    assert built.manifest.topics
    retry_diagnostic = next(
        item
        for item in built.manifest.usage
        if item.get("provider_error_code") == "chat_upstream_unavailable"
    )
    assert retry_diagnostic["finish_reason"] == "provider_error"


def test_active_map_requires_selected_profile_hash(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    _seed_selected_profile(tmp_path)
    service = ProfileKnowledgeMapService(tmp_path)

    built = asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
            activate=True,
        )
    )

    active = service.active_for_selected_profile(snapshot_id)
    assert active is not None
    assert active.manifest.map_id == built.manifest.map_id

    with StorageDatabase(tmp_path).session() as session, session.begin():
        selection = session.get(KnowledgeBaseProfileSelectionRow, "default")
        assert selection is not None
        selection.selected_profile_hash = "b" * 64

    with pytest.raises(ProfileKnowledgeMapError, match="does not match"):
        service.active_for_selected_profile(snapshot_id)


def test_active_map_requires_current_aspect_schema_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    _seed_selected_profile(tmp_path)
    service = ProfileKnowledgeMapService(tmp_path)
    asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
            activate=True,
        )
    )

    real_schema = profile_knowledge_map_module._profile_aspect_schema

    def changed_schema(profile_config: dict[str, object]):
        schema, _schema_hash = real_schema(profile_config)
        return schema, "d" * 64

    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map._profile_aspect_schema",
        changed_schema,
    )
    with pytest.raises(ProfileKnowledgeMapError, match="Aspect contract"):
        service.active_for_selected_profile(snapshot_id)


def test_ready_partial_map_requires_explicit_activation_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    partial_report = MapConvergenceReport(
        status="ready_partial",
        passes_used=8,
        accepted_merge_count=3,
        rejected_merge_count=1,
        uncertain_pair_count=0,
        pending_candidate_count=2,
        last_pass_new_merge_count=1,
        termination_reason="iteration safety limit reached",
    )
    partial = built.model_copy(
        update={"manifest": built.manifest.model_copy(update={"group_convergence": partial_report})}
    )
    monkeypatch.setattr(
        service,
        "load",
        lambda _snapshot_id, _map_id: ProfileKnowledgeMapResult.model_validate(
            partial.model_dump(mode="json")
        ),
    )

    with pytest.raises(
        ProfileKnowledgeMapError,
        match="explicit human confirmation",
    ) as blocked:
        service.activate(snapshot_id, built.manifest.map_id)
    assert blocked.value.code == "knowledge_map_activation_not_ready"
    assert not service._active_pointer_path(snapshot_id).exists()

    pointer = service.activate(
        snapshot_id,
        built.manifest.map_id,
        confirm_ready_partial=True,
    )
    assert pointer.activated_at <= datetime.now(UTC).isoformat()
    assert service._active_pointer_path(snapshot_id).exists()


def test_aspect_evidence_audit_falls_back_to_compiled_representatives(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(drop_all_aspect_audit_blocks=True)
    embedding = FakeEmbeddingProvider()

    async def run() -> None:
        built = await ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=embedding,
        )
        execution = await SearchWikiTopicV2Service(tmp_path).search(
            snapshot_id=snapshot_id,
            map_id=built.manifest.map_id,
            topic="AI Agent 隔离与控制边界",
            embedding_provider=embedding,
            provider=provider,
        )

        assert execution.selected_evidence
        assert execution.usage["aspect_evidence_fallbacks"]
        assert all(
            item.reason == "Deterministic fallback for compiled Aspect support."
            for item in execution.selected_evidence
        )

    asyncio.run(run())


def test_aspect_evidence_audit_repairs_missing_coverage_from_compiled_support(
    tmp_path: Path,
) -> None:
    provider = _GroupingProvider(marks_all_aspects_missing=True)
    topic_revision_id = "a" * 64
    aspect = TopicAspect(
        aspect_id=(
            "ta_"
            + profile_knowledge_map_module.stable_hash(
                {
                    "topic_revision_id": topic_revision_id,
                    "kind": "boundary",
                    "label": "文件系统边界",
                }
            )[:16]
        ),
        topic_id="pt_1111111111111111",
        topic_revision_id=topic_revision_id,
        kind="boundary",
        label="文件系统边界",
        description="限制 Agent 的文件访问范围。",
        importance=0.9,
        source_candidate_ids=["pc_1111111111111111"],
    )
    first_support = BlockAspectSupport(
        support_id=(
            "bas_"
            + profile_knowledge_map_module.stable_hash(
                {
                    "topic_revision_id": topic_revision_id,
                    "aspect_id": aspect.aspect_id,
                    "block_id": "block_1",
                }
            )[:16]
        ),
        block_id="block_1",
        topic_id=aspect.topic_id,
        topic_revision_id=topic_revision_id,
        aspect_id=aspect.aspect_id,
        support_kind="direct",
        support_strength=0.9,
        reason="直接说明文件边界。",
    )
    second_support = BlockAspectSupport(
        support_id=(
            "bas_"
            + profile_knowledge_map_module.stable_hash(
                {
                    "topic_revision_id": topic_revision_id,
                    "aspect_id": aspect.aspect_id,
                    "block_id": "block_2",
                }
            )[:16]
        ),
        block_id="block_2",
        topic_id=aspect.topic_id,
        topic_revision_id=topic_revision_id,
        aspect_id=aspect.aspect_id,
        support_kind="direct",
        support_strength=0.8,
        reason="补充说明文件边界。",
    )
    section_context = RetrievalSectionContext(
        section_depth=1,
        has_child_sections=False,
        document_section_count=1,
    )
    blocks = [
        RetrievalBlock(
            block_id=f"block_{index}",
            doc_id="doc_1",
            doc_title="Agent 边界",
            source_label="fixture",
            section_path=["边界"],
            section_context=section_context,
            text=f"文件系统边界说明 {index}",
        )
        for index in (1, 2)
    ]

    audit, usage = asyncio.run(
        SearchWikiTopicV2Service(tmp_path)._audit_aspect_evidence(
            topic="Agent 文件系统边界",
            profile_context="AI Agent 安全",
            representative_questions=["Agent 如何限制文件访问？"],
            selected_aspects=[aspect],
            representative_by_aspect={aspect.aspect_id: (first_support, "block_1", 0.9)},
            supports_by_aspect={aspect.aspect_id: [first_support, second_support]},
            blocks=blocks,
            retrieval_paths_by_block={},
            provider=provider,
        )
    )

    assert {item.block_id for item in audit.selected_blocks} == {"block_1", "block_2"}
    assert usage["aspect_evidence_coverage_repairs"] == [
        {"aspect_id": aspect.aspect_id, "block_id": "block_2"}
    ]
    assert audit.coverage[0].status == "covered"


def test_missing_aspect_review_extends_catalog_and_remaps_blocks(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(requires_aspect_extension=True)

    built = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    assert provider.aspect_extension_attempts == 1
    assert provider.aspect_mapping_attempts >= 2
    assert built.manifest.block_aspect_supports
    assert not built.manifest.block_aspect_exclusions
    assert any(item.get("stage") == "topic_aspect_extension" for item in built.manifest.usage)


def test_aspect_extension_omission_is_recorded_as_no_new_aspect(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(
        requires_aspect_extension=True,
        omits_first_aspect_extension_topic_once=True,
    )

    built = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    extension_usage = [
        item for item in built.manifest.usage if item.get("stage") == "topic_aspect_extension"
    ]
    assert len(extension_usage) == 1
    assert len(extension_usage[0]["omitted_review_topics"]) == 1
    assert provider.aspect_extension_attempts == 1


def test_aspect_extension_payload_allows_no_new_aspects() -> None:
    payload = profile_knowledge_map_module._AspectExtensionPayload.model_validate({"topics": []})

    assert payload.topics == []


def test_redundant_mapping_exclusion_is_resolved_by_concrete_support(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(emits_redundant_mapping_exclusions=True)

    built = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    assert built.manifest.block_aspect_supports
    assert not built.manifest.block_aspect_exclusions
    assert any(
        item.get("code") == "topic_aspect_mapping_overlap_resolved" for item in built.manifest.usage
    )


def test_search_preserves_duplicate_and_conflict_diagnostics(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(emits_conflict_diagnostics=True)
    embedding = FakeEmbeddingProvider()

    async def run() -> None:
        built = await ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=embedding,
        )
        execution = await SearchWikiTopicV2Service(tmp_path).search(
            snapshot_id=snapshot_id,
            map_id=built.manifest.map_id,
            topic="AI Agent 隔离与控制边界",
            embedding_provider=embedding,
            provider=provider,
        )

        assert execution.aspect_coverage
        assert any(item.conflicts_with for item in execution.selected_evidence)
        assert any(item.duplicate_of for item in execution.selected_evidence)

    asyncio.run(run())


def test_out_of_scope_mapping_reference_is_dropped_without_losing_coverage(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(emits_out_of_scope_mapping_exclusion=True)

    built = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    assert built.manifest.block_aspect_supports
    assert not built.manifest.block_aspect_exclusions
    assert any(
        item.get("code") == "topic_aspect_mapping_out_of_scope_references"
        for item in built.manifest.usage
    )


def test_mapping_omission_uses_one_targeted_repair(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    provider = _GroupingProvider(omits_first_mapping_block_once=True)

    built = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    assert provider.aspect_mapping_attempts >= 2
    assert built.manifest.block_aspect_supports
    assert not built.manifest.block_aspect_exclusions
    assert any(item.get("code") == "topic_aspect_mapping_repair" for item in built.manifest.usage)


def test_aspect_support_and_exclusion_ids_bind_topic_revision() -> None:
    topic_id = "pt_1111111111111111"
    first_revision = "1" * 64
    second_revision = "2" * 64

    def build_aspect(revision_id: str) -> TopicAspect:
        aspect_id = (
            "ta_"
            + profile_knowledge_map_module.stable_hash(
                {
                    "topic_revision_id": revision_id,
                    "kind": "boundary",
                    "label": "文件系统边界",
                }
            )[:16]
        )
        return TopicAspect(
            aspect_id=aspect_id,
            topic_id=topic_id,
            topic_revision_id=revision_id,
            kind="boundary",
            label="文件系统边界",
            description="限制 Agent 的文件访问范围。",
            importance=0.9,
            source_candidate_ids=["pc_1111111111111111"],
        )

    first_aspect = build_aspect(first_revision)
    second_aspect = build_aspect(second_revision)
    assert first_aspect.aspect_id != second_aspect.aspect_id

    first_support_id = (
        "bas_"
        + profile_knowledge_map_module.stable_hash(
            {
                "topic_revision_id": first_revision,
                "aspect_id": first_aspect.aspect_id,
                "block_id": "block_a",
            }
        )[:16]
    )
    first_support = BlockAspectSupport(
        support_id=first_support_id,
        block_id="block_a",
        topic_id=topic_id,
        topic_revision_id=first_revision,
        aspect_id=first_aspect.aspect_id,
        support_kind="direct",
        support_strength=0.9,
        reason="原文直接支持该 Aspect。",
    )
    assert first_support.support_id == first_support_id

    with pytest.raises(ValidationError):
        BlockAspectExclusion(
            exclusion_id="bae_1111111111111111",
            block_id="block_a",
            topic_id=topic_id,
            topic_revision_id=first_revision,
            reason_code="missing_aspect",
            reason="不应进入最终地图。",
        )


def test_cross_document_topics_merge_candidates_and_feed_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        -1.0,
    )
    rows = [
        _preview_candidate_row(
            run_id="pr_topic_a",
            candidate_id="pc_0000000000000001",
            doc_id="doc_a",
            block_id="block_a",
            title="Agent sandbox",
            summaries=["限制 Agent 的运行环境。", "从环境层限制 Agent。"],
        ),
        _preview_candidate_row(
            run_id="pr_topic_b",
            candidate_id="pc_0000000000000002",
            doc_id="doc_b",
            block_id="block_b",
            title="Agent isolation environment",
            summaries=["隔离 Agent 的执行边界。", "运行环境隔离。"],
        ),
    ]
    provider = _GroupingProvider(merge_topic_batch=True)
    embedding = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)

    async def run() -> None:
        resolved = await service._resolve_topic_ledger(
            candidate_rows=rows,
            base_topics=[],
            base_topic_vectors={},
            provider=provider,
            embedding_provider=embedding,
            profile_context="AI Agent 安全与工程",
        )
        merged = resolved.topics
        assert len(merged) == 1
        assert resolved.convergence.status in {"converged", "converged_with_uncertain"}
        assert {source.candidate_id for source in merged[0].sources} == {
            "pc_0000000000000001",
            "pc_0000000000000002",
        }
        assert merged[0].source_doc_ids == ["doc_a", "doc_b"]
        vectors = (
            await embedding.embed_documents(["\n".join(topic.summaries) for topic in merged])
        ).vectors
        aspects_by_topic = {topic.topic_id: [] for topic in merged}
        groups, _group_usage, group_vectors, _report = await service._build_groups(
            topics=merged,
            topic_vectors=vectors,
            topic_aspects_by_topic=aspects_by_topic,
            provider=provider,
            embedding_provider=embedding,
            profile_context="AI Agent 安全与工程",
        )
        assert len(groups) == 1
        assert groups[0].source_doc_ids == ["doc_a", "doc_b"]
        assert len(group_vectors) == len(groups)

    asyncio.run(run())


def test_topic_work_set_compares_candidates_only_with_topics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        -1.0,
    )
    rows = [
        _preview_candidate_row(
            run_id="pr_work_set",
            candidate_id="pc_0000000000000011",
            doc_id="doc_a",
            block_id="block_a",
            title="Agent sandbox",
            summaries=["限制 Agent 运行环境。"],
        ),
        _preview_candidate_row(
            run_id="pr_work_set",
            candidate_id="pc_0000000000000012",
            doc_id="doc_b",
            block_id="block_b",
            title="Agent recovery",
            summaries=["从检查点恢复 Agent 状态。"],
        ),
        _preview_candidate_row(
            run_id="pr_work_set",
            candidate_id="pc_0000000000000013",
            doc_id="doc_c",
            block_id="block_c",
            title="Agent sandbox",
            summaries=["隔离 Agent 的文件与执行边界。"],
        ),
    ]
    provider = _TopicIdentityProvider()

    resolved = asyncio.run(
        ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
            candidate_rows=rows,
            base_topics=[],
            base_topic_vectors={},
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
            profile_context="AI Agent 安全与工程",
        )
    )

    assert {topic.title for topic in resolved.topics} == {
        "Agent sandbox",
        "Agent recovery",
    }
    assert {"attach", "create"}.issubset({decision.outcome for decision in resolved.decisions})
    for left_ref, right_ref, _left_title, _right_title in provider.identity_pairs:
        assert not (left_ref.startswith("candidate:") and right_ref.startswith("candidate:"))
    assert len(resolved.judgements) == len(provider.identity_pairs)
    assert all(judgement.request_trace_id for judgement in resolved.judgements)
    assert {"create_topic", "attach_candidate"}.issubset(
        {operation.kind for operation in resolved.operations}
    )


def test_stale_topic_work_item_is_discarded_after_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        -1.0,
    )
    base_rows = [
        _preview_candidate_row(
            run_id="pr_stale",
            candidate_id=f"pc_{index:016x}",
            doc_id=f"doc_{index}",
            block_id=f"block_{index}",
            title=f"Topic {index}",
            summaries=[f"Topic {index} 的边界。"],
        )
        for index in range(1, 5)
    ]
    base_topics = [
        profile_knowledge_map_module._materialize_topic(
            lineage_id="pt_0000000000000001",
            rows=base_rows[:2],
        ),
        profile_knowledge_map_module._materialize_topic(
            lineage_id="pt_0000000000000002",
            rows=base_rows[2:],
        ),
    ]
    provider = _TopicIdentityProvider(
        {
            frozenset(("Topic 1", "Topic 3")): "same",
            frozenset(("Topic 1", "Topic 2")): "different",
            frozenset(("Topic 3", "Topic 4")): "different",
        }
    )

    resolved = asyncio.run(
        ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
            candidate_rows=[base_rows[0], base_rows[2]],
            base_candidate_rows=base_rows,
            base_topics=base_topics,
            base_topic_vectors={},
            provider=provider,
            embedding_provider=FakeEmbeddingProvider(),
            profile_context="AI Agent 安全与工程",
        )
    )

    assert len(resolved.topics) == 1
    assert sum(decision.outcome == "merge" for decision in resolved.decisions) == 1
    assert sum(decision.outcome == "keep" for decision in resolved.decisions) == 1
    assert resolved.convergence.passes_used == 1
    assert resolved.convergence.accepted_merge_count == 1
    operation_counts = {
        kind: sum(operation.kind == kind for operation in resolved.operations)
        for kind in {operation.kind for operation in resolved.operations}
    }
    assert operation_counts["detach_candidate"] == 2
    assert operation_counts["merge_topics"] == 1
    convergence_usage = next(
        item for item in resolved.usage if item.get("stage") == "topic_work_set_convergence"
    )
    assert convergence_usage["processed_work_item_count"] == len(resolved.decisions)
    assert convergence_usage["topic_pair_cache_evictions"] >= 1


def test_topic_pair_cache_reuses_reverse_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        -1.0,
    )
    base_rows = [
        _preview_candidate_row(
            run_id="pr_pair_cache",
            candidate_id=f"pc_{index:016x}",
            doc_id=f"doc_{index}",
            block_id=f"block_{index}",
            title=f"Pair cache topic {index}",
            summaries=[f"Pair cache topic {index} 的边界。"],
        )
        for index in range(1, 5)
    ]
    base_topics = [
        profile_knowledge_map_module._materialize_topic(
            lineage_id="pt_0000000000000301",
            rows=base_rows[:2],
        ),
        profile_knowledge_map_module._materialize_topic(
            lineage_id="pt_0000000000000302",
            rows=base_rows[2:],
        ),
    ]

    resolved = asyncio.run(
        ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
            candidate_rows=[base_rows[0], base_rows[2]],
            base_candidate_rows=base_rows,
            base_topics=base_topics,
            base_topic_vectors={},
            provider=_TopicIdentityProvider(),
            embedding_provider=FakeEmbeddingProvider(),
            profile_context="AI Agent 安全与工程",
        )
    )

    convergence_usage = next(
        item for item in resolved.usage if item.get("stage") == "topic_work_set_convergence"
    )
    assert len(resolved.decisions) == 2
    assert convergence_usage["topic_pair_cache_misses"] == 1
    assert convergence_usage["topic_pair_cache_hits"] == 1


def test_topic_identity_target_guard_fails_without_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        -1.0,
    )
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_MAX_TARGET_PAIRS",
        1,
    )
    rows = [
        _preview_candidate_row(
            run_id="pr_budget",
            candidate_id=f"pc_{index:016x}",
            doc_id=f"doc_{index}",
            block_id=f"block_{index}",
            title=f"Budget topic {index}",
            summaries=[f"Budget topic {index} 的边界。"],
        )
        for index in range(1, 4)
    ]

    with pytest.raises(ProfileKnowledgeMapError) as failure:
        asyncio.run(
            ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
                candidate_rows=rows,
                base_topics=[],
                base_topic_vectors={},
                provider=_TopicIdentityProvider(),
                embedding_provider=FakeEmbeddingProvider(),
                profile_context="AI Agent 安全与工程",
            )
        )

    assert failure.value.code == "identity_target_budget_exceeded"


def test_topic_ledger_rejects_candidate_coverage_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        -1.0,
    )
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map._new_topic_lineage_id",
        lambda _ref: "pt_00000000000000ff",
    )
    rows = [
        _preview_candidate_row(
            run_id="pr_coverage",
            candidate_id=f"pc_{index:016x}",
            doc_id=f"doc_{index}",
            block_id=f"block_{index}",
            title=f"Coverage topic {index}",
            summaries=[f"Coverage topic {index} 的边界。"],
        )
        for index in range(1, 3)
    ]

    with pytest.raises(ProfileKnowledgeMapError) as failure:
        asyncio.run(
            ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
                candidate_rows=rows,
                base_topics=[],
                base_topic_vectors={},
                provider=_TopicIdentityProvider(),
                embedding_provider=FakeEmbeddingProvider(),
                profile_context="AI Agent 安全与工程",
            )
        )

    assert failure.value.code == "knowledge_map_candidate_coverage_invalid"


def test_work_item_token_budget_includes_prompt_overhead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = "AI Agent 安全与工程"
    base_row = _preview_candidate_row(
        run_id="pr_budget_base",
        candidate_id="pc_0000000000000201",
        doc_id="doc_base",
        block_id="block_base",
        title="预算目标 Topic",
        summaries=["预算目标 Topic 的边界。"],
    )
    target_row = _preview_candidate_row(
        run_id="pr_budget_target",
        candidate_id="pc_0000000000000202",
        doc_id="doc_target",
        block_id="block_target",
        title="预算候选 Topic",
        summaries=["预算候选 Topic 的边界。"],
    )
    base_topic = profile_knowledge_map_module._materialize_topic(
        lineage_id="pt_0000000000000201",
        rows=[base_row],
    )
    candidate = profile_knowledge_map_module._candidate_node(target_row)
    pair_payload = {
        "left_node_ref": candidate.node_ref,
        "right_node_ref": profile_knowledge_map_module._topic_node_ref(base_topic),
        "left": profile_knowledge_map_module._candidate_card(candidate),
        "right": profile_knowledge_map_module._topic_card(base_topic),
    }
    pair_only_tokens = estimate_tokens(json_dumps(pair_payload))
    configured_limit = pair_only_tokens + 1
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        -1.0,
    )
    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_MAX_TOTAL_TOKENS",
        configured_limit,
    )

    with pytest.raises(ProfileKnowledgeMapError) as failure:
        asyncio.run(
            ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
                candidate_rows=[base_row, target_row],
                base_candidate_rows=[base_row],
                base_topics=[base_topic],
                base_topic_vectors={},
                provider=_TopicIdentityProvider(),
                embedding_provider=FakeEmbeddingProvider(),
                profile_context=context,
            )
        )

    assert failure.value.code == "identity_target_budget_exceeded"
    assert failure.value.diagnostics["estimated_tokens"] > configured_limit


def test_topic_pair_cache_key_is_direction_independent() -> None:
    left_ref = "topic:pt_0000000000000001:" + "1" * 64
    right_ref = "topic:pt_0000000000000002:" + "2" * 64
    contract_hash = "a" * 64

    assert profile_knowledge_map_module._topic_pair_key(
        left_ref,
        right_ref,
        contract_hash,
    ) == profile_knowledge_map_module._topic_pair_key(
        right_ref,
        left_ref,
        contract_hash,
    )


def test_active_topic_vector_index_reuses_freed_rows() -> None:
    index = profile_knowledge_map_module._ActiveTopicVectorIndex(2)
    first_vector = [0.0] * 1023 + [1.0]
    second_vector = [0.0] * 1022 + [1.0, 0.0]
    third_vector = [1.0] + [0.0] * 1023
    index.upsert(
        lineage_id="pt_0000000000000001",
        node_ref="topic:pt_0000000000000001:" + "1" * 64,
        vector=first_vector,
    )
    index.upsert(
        lineage_id="pt_0000000000000002",
        node_ref="topic:pt_0000000000000002:" + "2" * 64,
        vector=second_vector,
    )
    index.remove("pt_0000000000000001")
    index.upsert(
        lineage_id="pt_0000000000000003",
        node_ref="topic:pt_0000000000000003:" + "3" * 64,
        vector=third_vector,
    )

    assert index._size == 2
    assert index._free_rows == []


def test_unchanged_topic_reuses_revision_vector_from_base_bundle(
    tmp_path: Path,
) -> None:
    row = _preview_candidate_row(
        run_id="pr_vector_reuse",
        candidate_id="pc_0000000000000021",
        doc_id="doc_vector",
        block_id="block_vector",
        title="向量复用边界",
        summaries=["只有当前 revision 的摘要向量可以复用。"],
    )
    base_topic = profile_knowledge_map_module._materialize_topic(
        lineage_id="pt_0000000000000021",
        rows=[row],
    )
    embedding = FakeEmbeddingProvider()
    base_vector = asyncio.run(
        embedding.embed_documents([profile_knowledge_map_module._topic_identity_text(base_topic)])
    ).vectors[0]
    embedding.calls.clear()

    resolved = asyncio.run(
        ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
            candidate_rows=[row],
            base_candidate_rows=[row],
            base_topics=[base_topic],
            base_topic_vectors={base_topic.topic_id: base_vector},
            provider=_TopicIdentityProvider(),
            embedding_provider=embedding,
            profile_context="AI Agent 安全与工程",
        )
    )

    assert resolved.topics == [base_topic]
    assert embedding.calls == []


def test_persisted_identity_vectors_are_reused_for_unchanged_ledger(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    loaded = service.load(snapshot_id, built.manifest.map_id)
    assert loaded.manifest.compile_contract is not None
    with StorageDatabase(tmp_path).session() as session:
        rows = list(
            session.execute(
                select(PreviewCandidateRow).where(PreviewCandidateRow.run_id == run_id)
            ).scalars()
        )
    embedding = FakeEmbeddingProvider()

    resolved = asyncio.run(
        service._resolve_topic_ledger(
            candidate_rows=rows,
            base_candidate_rows=rows,
            base_topics=loaded.manifest.topics,
            base_topic_vectors={},
            base_candidate_identity_vectors=loaded.candidate_identity_vectors,
            base_topic_identity_vectors=loaded.topic_identity_vectors,
            provider=_TopicIdentityProvider(),
            embedding_provider=embedding,
            profile_context=loaded.manifest.profile_context,
            compile_contract=loaded.manifest.compile_contract,
        )
    )

    assert embedding.calls == []
    assert resolved.candidate_identity_vectors == loaded.candidate_identity_vectors
    assert resolved.topic_identity_vectors == loaded.topic_identity_vectors


def test_new_extraction_run_recomputes_candidate_identity_vector(
    tmp_path: Path,
) -> None:
    base_row = _preview_candidate_row(
        run_id="pr_vector_base",
        candidate_id="pc_0000000000000031",
        doc_id="doc_vector",
        block_id="block_vector",
        title="跨运行向量",
        summaries=["相同摘要也绑定 Extraction Run 身份。"],
    )
    target_row = _preview_candidate_row(
        run_id="pr_vector_target",
        candidate_id="pc_0000000000000031",
        doc_id="doc_vector",
        block_id="block_vector",
        title="跨运行向量",
        summaries=["相同摘要也绑定 Extraction Run 身份。"],
    )
    base_topic = profile_knowledge_map_module._materialize_topic(
        lineage_id="pt_0000000000000031",
        rows=[base_row],
    )
    embedding = FakeEmbeddingProvider()

    asyncio.run(
        ProfileKnowledgeMapService(tmp_path)._resolve_topic_ledger(
            candidate_rows=[target_row],
            base_candidate_rows=[base_row],
            base_topics=[base_topic],
            base_topic_vectors={base_topic.topic_id: [0.0] * 1023 + [1.0]},
            provider=_TopicIdentityProvider(),
            embedding_provider=embedding,
            profile_context="AI Agent 安全与工程",
        )
    )

    assert any(
        "相同摘要也绑定 Extraction Run 身份。" in call
        for batch in embedding.calls
        for call in batch
    )


def test_topic_canonical_title_uses_doc_run_candidate_order() -> None:
    later_run = _preview_candidate_row(
        run_id="pr_z",
        candidate_id="pc_0000000000000101",
        doc_id="doc_same",
        block_id="block_a",
        title="按 ordinal 会更早的标题",
        summaries=["测试 canonical title。"],
    )
    later_run.ordinal = 0
    earlier_run = _preview_candidate_row(
        run_id="pr_a",
        candidate_id="pc_0000000000000102",
        doc_id="doc_same",
        block_id="block_b",
        title="按 run_id 应成为规范标题",
        summaries=["测试 canonical title。"],
    )
    earlier_run.ordinal = 99

    topic = profile_knowledge_map_module._materialize_topic(
        lineage_id="pt_0000000000000100",
        rows=[later_run, earlier_run],
    )

    assert topic.title == "按 run_id 应成为规范标题"


def test_global_group_candidate_graph_uses_lexical_and_topic_overlap_edges() -> None:
    groups = [
        _candidate_group(
            group_id="wg_0000000000000001",
            label="Agent isolation and boundary",
            child_topic_ids=["pt_0000000000000001", "pt_0000000000000002"],
            source_doc_ids=["doc_a", "doc_b"],
        ),
        _candidate_group(
            group_id="wg_0000000000000002",
            label="Agent isolation controls",
            child_topic_ids=["pt_0000000000000001", "pt_0000000000000003"],
            source_doc_ids=["doc_a", "doc_b"],
        ),
        _candidate_group(
            group_id="wg_0000000000000003",
            label="Navigation metadata",
            child_topic_ids=["pt_0000000000000004"],
            source_doc_ids=["doc_c"],
        ),
    ]

    group_edges = _group_candidate_edges(groups=groups, edges=[])
    assert ("wg_0000000000000001", "wg_0000000000000002") in {
        (left, right) for left, right, _score in group_edges
    }


def test_group_merge_must_consume_each_input_exactly_once() -> None:
    payload = profile_knowledge_map_module._GroupMergePayload.model_validate(
        {
            "actions": [
                {
                    "group_ids": ["wg_0000000000000001"],
                    "decision": "keep",
                    "reason": "保留。",
                },
                {
                    "group_ids": [
                        "wg_0000000000000001",
                        "wg_0000000000000002",
                    ],
                    "decision": "merge",
                    "label": "重复消费",
                    "summary": "同一输入被消费两次。",
                    "reason": "应被拒绝。",
                },
            ],
            "uncertain_pairs": [],
        }
    )

    with pytest.raises(ValueError, match="only one merge action"):
        profile_knowledge_map_module._validate_group_merge(
            payload,
            allowed_ids={
                "wg_0000000000000001",
                "wg_0000000000000002",
            },
        )


def test_group_merge_rejects_omitted_input_groups() -> None:
    payload = profile_knowledge_map_module._GroupMergePayload.model_validate(
        {
            "actions": [
                {
                    "group_ids": ["wg_0000000000000001"],
                    "decision": "keep",
                    "reason": "只处理了一个输入。",
                }
            ],
            "uncertain_pairs": [],
        }
    )

    with pytest.raises(ValueError, match="missing=\\['wg_0000000000000002'\\]"):
        profile_knowledge_map_module._validate_group_merge(
            payload,
            allowed_ids={
                "wg_0000000000000001",
                "wg_0000000000000002",
            },
        )


def test_group_merge_actions_only_union_input_topics() -> None:
    groups = [
        _candidate_group(
            group_id="wg_0000000000000001",
            label="Group A",
            child_topic_ids=["pt_0000000000000001", "pt_0000000000000002"],
            source_doc_ids=["doc_a"],
        ),
        _candidate_group(
            group_id="wg_0000000000000002",
            label="Group B",
            child_topic_ids=["pt_0000000000000002", "pt_0000000000000003"],
            source_doc_ids=["doc_b"],
        ),
    ]
    action = profile_knowledge_map_module._GroupMergeAction.model_validate(
        {
            "group_ids": [group.group_id for group in groups],
            "decision": "merge",
            "label": "Merged Group",
            "summary": "合并后的检索区域。",
            "retrieval_questions": ["合并区域包含什么？"],
            "reason": "测试同一区域。",
        }
    )

    merged, stats = ProfileKnowledgeMapService._merge_groups_by_actions(
        [action],
        groups,
    )

    assert len(merged) == 1
    assert merged[0].child_topic_ids == [
        "pt_0000000000000001",
        "pt_0000000000000002",
        "pt_0000000000000003",
    ]
    assert merged[0].source_doc_ids == ["doc_a", "doc_b"]
    assert stats["merge_action_count"] == 1
    assert stats["merged_cluster_count"] == 1


def test_high_confidence_group_merge_requires_vector_and_topic_overlap() -> None:
    groups = [
        _candidate_group(
            group_id="wg_0000000000000001",
            label="Group A",
            child_topic_ids=[
                "pt_0000000000000001",
                "pt_0000000000000002",
            ],
            source_doc_ids=["doc_a"],
        ),
        _candidate_group(
            group_id="wg_0000000000000002",
            label="Group B",
            child_topic_ids=[
                "pt_0000000000000001",
                "pt_0000000000000002",
            ],
            source_doc_ids=["doc_b"],
        ),
        _candidate_group(
            group_id="wg_0000000000000003",
            label="Group C",
            child_topic_ids=["pt_0000000000000003"],
            source_doc_ids=["doc_c"],
        ),
    ]

    merged, stats = ProfileKnowledgeMapService._auto_merge_high_overlap_groups(
        groups=groups,
        vectors={
            groups[0].group_id: [1.0, 0.0],
            groups[1].group_id: [1.0, 0.0],
            groups[2].group_id: [0.0, 1.0],
        },
        edges=[
            (groups[0].group_id, groups[1].group_id, 1.0),
            (groups[0].group_id, groups[2].group_id, 0.0),
        ],
    )

    assert len(merged) == 2
    assert stats["groups_removed"] == 1
    merged_group = next(group for group in merged if len(group.child_topic_ids) == 2)
    assert merged_group.source_doc_ids == ["doc_a", "doc_b"]


def test_high_confidence_group_merge_keeps_similar_vectors_without_topic_overlap() -> None:
    groups = [
        _candidate_group(
            group_id="wg_0000000000000001",
            label="Group A",
            child_topic_ids=["pt_0000000000000001"],
            source_doc_ids=["doc_a"],
        ),
        _candidate_group(
            group_id="wg_0000000000000002",
            label="Group B",
            child_topic_ids=["pt_0000000000000002"],
            source_doc_ids=["doc_b"],
        ),
    ]

    merged, stats = ProfileKnowledgeMapService._auto_merge_high_overlap_groups(
        groups=groups,
        vectors={
            groups[0].group_id: [1.0, 0.0],
            groups[1].group_id: [1.0, 0.0],
        },
        edges=[(groups[0].group_id, groups[1].group_id, 1.0)],
    )

    assert merged == groups
    assert stats["groups_removed"] == 0


def test_selected_group_requires_supported_topics() -> None:
    payload = profile_knowledge_map_module._GroupSelectionPayload.model_validate(
        {
            "groups": [
                {
                    "group_id": "wg_0000000000000001",
                    "relevance": "partial",
                    "supported_topic_ids": [],
                    "reason": "只写了相关。",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="supporting Topic"):
        profile_knowledge_map_module._validate_group_selection(
            payload,
            allowed_ids={"wg_0000000000000001"},
            allowed_topic_ids={"wg_0000000000000001": {"pt_0000000000000001"}},
        )


def test_selected_group_rejects_supporting_topics_outside_group() -> None:
    payload = profile_knowledge_map_module._GroupSelectionPayload.model_validate(
        {
            "groups": [
                {
                    "group_id": "wg_0000000000000001",
                    "relevance": "partial",
                    "supported_topic_ids": ["pt_0000000000000002"],
                    "reason": "引用了其它 Group 的 Topic。",
                }
            ]
        }
    )

    with pytest.raises(ValueError, match="outside its Group"):
        profile_knowledge_map_module._validate_group_selection(
            payload,
            allowed_ids={"wg_0000000000000001"},
            allowed_topic_ids={"wg_0000000000000001": {"pt_0000000000000001"}},
        )


def _preview_candidate_row(
    *,
    run_id: str,
    candidate_id: str,
    doc_id: str,
    block_id: str,
    title: str,
    summaries: list[str],
    aliases: list[str] | None = None,
) -> PreviewCandidateRow:
    return PreviewCandidateRow(
        candidate_id=candidate_id,
        run_id=run_id,
        doc_id=doc_id,
        doc_version=1,
        ordinal=1,
        name=title,
        normalized_name=title.casefold(),
        aliases_json=json_dumps(aliases or []),
        summaries_json=json_dumps(summaries),
        source_block_ids_json=json_dumps([block_id]),
        evidence_blocks_json="[]",
        channel_ids_json=json_dumps(["ec_1111111111111111"]),
        content_hash=hashlib.sha256(json_dumps(summaries).encode()).hexdigest(),
    )


def _candidate_topic(
    *,
    topic_id: str,
    title: str,
    aliases: list[str] | None = None,
    source_block_ids: list[str] | None = None,
) -> ProfileMapTopic:
    block_ids = source_block_ids or [f"{topic_id}_block"]
    candidate_suffix = topic_id.removeprefix("pt_")
    return ProfileMapTopic(
        topic_id=topic_id,
        revision_id="a" * 64,
        title=title,
        aliases=aliases or [],
        summaries=[f"{title} 的定义与机制。"],
        sources=[
            ProfileMapTopicSource(
                extraction_run_id="pr_test",
                candidate_id=f"pc_{candidate_suffix}",
                doc_id=f"doc_{candidate_suffix}",
                doc_version=1,
                source_block_ids=block_ids,
                channel_ids=["ec_1111111111111111"],
            )
        ],
        source_doc_ids=[f"doc_{candidate_suffix}"],
        source_block_ids=block_ids,
        channel_ids=["ec_1111111111111111"],
    )


def _candidate_group(
    *,
    group_id: str,
    label: str,
    child_topic_ids: list[str],
    source_doc_ids: list[str],
) -> ProfileMapGroup:
    return ProfileMapGroup(
        group_id=group_id,
        label=label,
        aliases=[],
        summary=f"{label} 的检索区域。",
        retrieval_questions=[f"{label} 包括什么？"],
        child_topic_ids=child_topic_ids,
        source_doc_ids=source_doc_ids,
    )


def test_group_coverage_audit_supplements_an_unselected_candidate(
    tmp_path: Path,
) -> None:
    selected_topic = _candidate_topic(topic_id="pt_1111111111111111", title="已覆盖边界")
    gap_topic = _candidate_topic(topic_id="pt_2222222222222222", title="遗漏边界")
    selected_group = _candidate_group(
        group_id="wg_aaaaaaaaaaaaaaaa",
        label="已选检索区域",
        child_topic_ids=[selected_topic.topic_id],
        source_doc_ids=selected_topic.source_doc_ids,
    )
    gap_group = _candidate_group(
        group_id="wg_bbbbbbbbbbbbbbbb",
        label="遗漏检索区域",
        child_topic_ids=[gap_topic.topic_id],
        source_doc_ids=gap_topic.source_doc_ids,
    )
    selection = profile_knowledge_map_module._GroupSelectionPayload.model_validate(
        {
            "groups": [
                {
                    "group_id": selected_group.group_id,
                    "relevance": "direct",
                    "supported_topic_ids": [selected_topic.topic_id],
                    "reason": "基础路由已选。",
                }
            ]
        }
    )

    supplements, usage = asyncio.run(
        SearchWikiTopicV2Service(tmp_path)._audit_group_coverage(
            topic="边界测试",
            profile_context="测试领域",
            representative_questions=["有哪些边界？"],
            candidate_groups=[selected_group, gap_group],
            selected_groups=selection,
            group_scores={selected_group.group_id: 0.8, gap_group.group_id: 0.6},
            topic_scores={selected_topic.topic_id: 0.7, gap_topic.topic_id: 0.6},
            topic_by_id={
                selected_topic.topic_id: selected_topic,
                gap_topic.topic_id: gap_topic,
            },
            provider=_GroupingProvider(supplement_first_unselected_group=True),
        )
    )

    assert [item.group_id for item in supplements] == [gap_group.group_id]
    assert supplements[0].supported_topic_ids == [gap_topic.topic_id]
    assert supplements[0].reason.startswith("覆盖补选｜")
    assert usage["attempts"] == 1


def test_search_wiki_topic_v2_reports_not_ready_without_a_map(tmp_path: Path) -> None:
    adapter = WikiToolAdapter(
        tmp_path,
        snapshot_id="s_missing",
        embedding_provider=FakeEmbeddingProvider(),
        chat_provider=_GroupingProvider(),
    )

    assert adapter.descriptors == []
    with pytest.raises(ToolUnavailableError):
        asyncio.run(
            adapter.execute(
                "search_wiki_topic_v2",
                {"topic": "AI Agent 隔离与控制边界"},
            )
        )


def test_incremental_map_plan_is_noop_across_preview_runs(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_base",
        candidate_prefix=1,
    )
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_target",
        candidate_prefix=100,
    )
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )
    map_path = next(tmp_path.glob(f"wiki-knowledge-maps/*/{built.manifest.map_id}/map.json"))
    vector_path = map_path.with_name("vectors.npz")
    artifact_bytes = (map_path.read_bytes(), vector_path.read_bytes())

    plan = asyncio.run(
        service.plan_incremental_map_update(
            base_map_id=built.manifest.map_id,
            preview_run_ids=[target_run_id],
            embedding_provider=embedding_provider,
        )
    )

    assert plan.status == "noop"
    assert plan.candidates.added_source_keys == []
    assert plan.candidates.removed_source_keys == []
    assert plan.candidates.updated_sources == []
    assert plan.topics.revised_topic_lineage_ids == []
    assert plan.topics.retired_topic_lineage_ids == []
    assert plan.affected_current_group_lineage_ids == []
    assert plan.pending_source_group_candidates == []
    assert len(plan.topics.unchanged_topic_lineage_ids) == len(built.manifest.topics)
    assert (map_path.read_bytes(), vector_path.read_bytes()) == artifact_bytes


def test_incremental_map_plan_marks_added_candidate_without_touching_groups(
    tmp_path: Path,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_added_base",
        candidate_prefix=1,
    )
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_added_target",
        candidate_prefix=100,
    )
    reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    document = reader.list_documents().documents[0]
    extra_block = reader.list_blocks(document.doc_id).blocks[-1]
    with StorageDatabase(tmp_path).session() as session, session.begin():
        session.add(
            PreviewCandidateRow(
                candidate_id=f"pc_{104:016x}",
                run_id=target_run_id,
                doc_id=document.doc_id,
                doc_version=1,
                ordinal=4,
                name="并行任务状态协调",
                normalized_name="并行任务状态协调",
                provisional_name="并行任务状态协调",
                title_kind=None,
                title_standalone_clear=None,
                missing_anchor_types_json="[]",
                name_changed=False,
                aliases_json="[]",
                summaries_json=json_dumps(["说明并行任务之间的状态协调边界。"]),
                selection_reasons_json="[]",
                name_anchor_source_block_ids_json="[]",
                name_anchor_reason=None,
                source_block_ids_json=json_dumps([extra_block.block_id]),
                evidence_blocks_json=json_dumps(
                    [
                        {
                            "block_id": extra_block.block_id,
                            "section_path": extra_block.section_path,
                            "text": extra_block.text,
                        }
                    ]
                ),
                channel_ids_json=json_dumps(["ec_1111111111111111"]),
                content_hash=hashlib.sha256("并行任务状态协调".encode()).hexdigest(),
            )
        )
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )

    plan = asyncio.run(
        service.plan_incremental_map_update(
            base_map_id=built.manifest.map_id,
            preview_run_ids=[target_run_id],
            embedding_provider=embedding_provider,
        )
    )

    assert plan.status == "incremental"
    assert len(plan.candidates.added_source_keys) == 1
    assert plan.candidates.removed_source_keys == []
    assert plan.candidates.updated_sources == []
    assert plan.topics.revised_topic_lineage_ids == []
    assert plan.topics.retired_topic_lineage_ids == []
    assert plan.topics.new_source_keys == plan.candidates.added_source_keys
    assert plan.affected_current_group_lineage_ids == []
    assert len(plan.pending_source_group_candidates) == 1
    assert 0 < len(plan.pending_source_group_candidates[0].candidate_groups) <= 16


def test_incremental_map_plan_marks_one_updated_source_and_topic(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_updated_base",
        candidate_prefix=1,
    )
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_updated_target",
        candidate_prefix=100,
    )
    target_candidate_id = f"pc_{100:016x}"
    with StorageDatabase(tmp_path).session() as session, session.begin():
        candidate = session.get(PreviewCandidateRow, target_candidate_id)
        assert candidate is not None
        candidate.summaries_json = json_dumps(["文件挂载模式的更新说明。"])
        candidate.content_hash = hashlib.sha256(
            candidate.summaries_json.encode("utf-8")
        ).hexdigest()
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )

    plan = asyncio.run(
        service.plan_incremental_map_update(
            base_map_id=built.manifest.map_id,
            preview_run_ids=[target_run_id],
            embedding_provider=embedding_provider,
        )
    )

    assert plan.status == "incremental"
    assert len(plan.candidates.updated_sources) == 1
    assert len(plan.candidates.added_source_keys) == 1
    assert len(plan.candidates.removed_source_keys) == 1
    assert len(plan.topics.revised_topic_lineage_ids) == 1
    assert plan.topics.retired_topic_lineage_ids == []
    assert plan.topics.new_source_keys == plan.candidates.added_source_keys
    assert plan.affected_current_group_lineage_ids
    assert len(plan.pending_source_group_candidates) == 1
    assert len(plan.pending_source_group_candidates[0].candidate_groups) > 0


def test_incremental_map_plan_retires_topic_after_source_delete(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_delete_base",
        candidate_prefix=1,
    )
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_delete_target",
        candidate_prefix=100,
    )
    target_candidate_id = f"pc_{100:016x}"
    with StorageDatabase(tmp_path).session() as session, session.begin():
        candidate = session.get(PreviewCandidateRow, target_candidate_id)
        assert candidate is not None
        session.delete(candidate)
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )

    plan = asyncio.run(
        service.plan_incremental_map_update(
            base_map_id=built.manifest.map_id,
            preview_run_ids=[target_run_id],
            embedding_provider=embedding_provider,
        )
    )

    assert plan.status == "incremental"
    assert len(plan.candidates.removed_source_keys) == 1
    assert plan.candidates.added_source_keys == []
    assert len(plan.topics.retired_topic_lineage_ids) == 1
    assert plan.topics.revised_topic_lineage_ids == []
    assert plan.affected_current_group_lineage_ids


def test_incremental_map_plan_without_base_is_full(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_full",
        candidate_prefix=100,
    )

    plan = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).plan_incremental_map_update(
            preview_run_ids=[target_run_id],
            embedding_provider=FakeEmbeddingProvider(),
            base_map_id=None,
        )
    )

    assert plan.status == "full"
    assert plan.base_map_id is None
    assert plan.candidates.removed_source_keys == []
    assert plan.candidates.unchanged_source_keys == []
    assert len(plan.candidates.added_source_keys) == 4
    assert plan.topics.new_source_keys == plan.candidates.added_source_keys


def test_incremental_map_build_noop_reuses_base_map(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_build_noop_base",
        candidate_prefix=1,
    )
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )

    updated = asyncio.run(
        service.build_incremental_map_update(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
            base_map_id=built.manifest.map_id,
        )
    )

    assert updated.reused
    assert updated.manifest.map_id == built.manifest.map_id
    assert updated.manifest.manifest_hash == built.manifest.manifest_hash


def test_incremental_map_build_adds_searchable_new_topic(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_build_add_base",
        candidate_prefix=1,
    )
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_build_add_target",
        candidate_prefix=100,
    )
    reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    document = reader.list_documents().documents[0]
    extra_block = next(
        block
        for block in reader.list_blocks(document.doc_id).blocks
        if "工具输出检查" in block.text
    )
    with StorageDatabase(tmp_path).session() as session, session.begin():
        session.add(
            PreviewCandidateRow(
                candidate_id=f"pc_{104:016x}",
                run_id=target_run_id,
                doc_id=document.doc_id,
                doc_version=1,
                ordinal=4,
                name="工具输出风险协调",
                normalized_name="工具输出风险协调",
                provisional_name="工具输出风险协调",
                title_kind=None,
                title_standalone_clear=None,
                missing_anchor_types_json="[]",
                name_changed=False,
                aliases_json="[]",
                summaries_json=json_dumps(["协调工具输出进入模型上下文前的风险边界。"]),
                selection_reasons_json="[]",
                name_anchor_source_block_ids_json="[]",
                name_anchor_reason=None,
                source_block_ids_json=json_dumps([extra_block.block_id]),
                evidence_blocks_json=json_dumps(
                    [
                        {
                            "block_id": extra_block.block_id,
                            "section_path": extra_block.section_path,
                            "text": extra_block.text,
                        }
                    ]
                ),
                channel_ids_json=json_dumps(["ec_1111111111111111"]),
                content_hash=hashlib.sha256("工具输出风险协调".encode()).hexdigest(),
            )
        )
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )

    updated = asyncio.run(
        service.build_incremental_map_update(
            preview_run_ids=[target_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
            base_map_id=built.manifest.map_id,
        )
    )

    assert not updated.reused
    assert updated.manifest.schema_version == "profile-knowledge-map-v6"
    assert updated.manifest.base_map_id == built.manifest.map_id
    assert updated.manifest.compilation_mode == "incremental"
    ledger_usage = next(
        item for item in updated.manifest.usage if item.get("stage") == "incremental_ledger_update"
    )
    assert ledger_usage["new_topic_count"] == 5
    assert ledger_usage["reused_topic_count"] == 0
    assert any(topic.title == "工具输出风险协调" for topic in updated.manifest.topics)
    assert not {topic.topic_id for topic in built.manifest.topics}.intersection(
        {topic.topic_id for topic in updated.manifest.topics}
    )

    execution = asyncio.run(
        SearchWikiTopicV2Service(tmp_path).search(
            snapshot_id=snapshot_id,
            map_id=updated.manifest.map_id,
            topic="工具输出风险协调",
            embedding_provider=embedding_provider,
            provider=_GroupingProvider(),
        )
    )

    assert extra_block.block_id in {block.block_id for block in execution.result.blocks}


def test_incremental_map_build_reuses_topic_id_for_updated_source(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_build_update_base",
        candidate_prefix=1,
    )
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_build_update_target",
        candidate_prefix=100,
    )
    target_candidate_id = f"pc_{100:016x}"
    with StorageDatabase(tmp_path).session() as session, session.begin():
        candidate = session.get(PreviewCandidateRow, target_candidate_id)
        assert candidate is not None
        candidate.summaries_json = json_dumps(["文件挂载模式现在覆盖更明确的宿主机边界。"])
        candidate.content_hash = hashlib.sha256(
            candidate.summaries_json.encode("utf-8")
        ).hexdigest()
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )
    base_topic = next(topic for topic in built.manifest.topics if topic.title == "文件挂载模式")

    updated = asyncio.run(
        service.build_incremental_map_update(
            preview_run_ids=[target_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
            base_map_id=built.manifest.map_id,
        )
    )

    updated_topic = next(
        topic for topic in updated.manifest.topics if topic.title == "文件挂载模式"
    )
    ledger_usage = next(
        item for item in updated.manifest.usage if item.get("stage") == "incremental_ledger_update"
    )
    assert ledger_usage["revised_topic_count"] == 0
    assert ledger_usage["new_topic_count"] == 4
    assert ledger_usage["reused_topic_count"] == 0
    assert updated_topic.topic_id != base_topic.topic_id
    assert updated_topic.summaries == ["文件挂载模式现在覆盖更明确的宿主机边界。"]
    assert any(source.candidate_id == target_candidate_id for source in updated_topic.sources)
    execution = asyncio.run(
        SearchWikiTopicV2Service(tmp_path).search(
            snapshot_id=snapshot_id,
            map_id=updated.manifest.map_id,
            topic="文件挂载模式",
            embedding_provider=embedding_provider,
            provider=_GroupingProvider(),
        )
    )
    assert execution.result.blocks


def test_incremental_map_build_retires_deleted_last_source(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    base_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_build_delete_base",
        candidate_prefix=1,
    )
    target_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_incremental_build_delete_target",
        candidate_prefix=100,
    )
    target_candidate_id = f"pc_{100:016x}"
    with StorageDatabase(tmp_path).session() as session, session.begin():
        candidate = session.get(PreviewCandidateRow, target_candidate_id)
        assert candidate is not None
        session.delete(candidate)
    embedding_provider = FakeEmbeddingProvider()
    service = ProfileKnowledgeMapService(tmp_path)
    built = asyncio.run(
        service.build(
            preview_run_ids=[base_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
        )
    )
    retired_topic = next(topic for topic in built.manifest.topics if topic.title == "文件挂载模式")

    updated = asyncio.run(
        service.build_incremental_map_update(
            preview_run_ids=[target_run_id],
            provider=_GroupingProvider(),
            embedding_provider=embedding_provider,
            base_map_id=built.manifest.map_id,
        )
    )

    ledger_usage = next(
        item for item in updated.manifest.usage if item.get("stage") == "incremental_ledger_update"
    )
    assert ledger_usage["retired_topic_count"] == len(built.manifest.topics)
    assert ledger_usage["reused_topic_count"] == 0
    assert retired_topic.topic_id not in {topic.topic_id for topic in updated.manifest.topics}
    assert retired_topic.topic_id not in {
        topic_id for group in updated.manifest.groups for topic_id in group.child_topic_ids
    }
    assert all(group.child_topic_ids for group in updated.manifest.groups)


def test_knowledge_map_rejects_overlapping_preview_run_scopes(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    first_run_id = _seed_preview_run(tmp_path, snapshot_id)
    second_run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_knowledge_map_test_second",
        candidate_prefix=100,
    )

    with pytest.raises(ProfileKnowledgeMapError, match="overlapping document scopes"):
        asyncio.run(
            ProfileKnowledgeMapService(tmp_path).build(
                preview_run_ids=[first_run_id, second_run_id],
                provider=_GroupingProvider(),
                embedding_provider=FakeEmbeddingProvider(),
            )
        )


def test_knowledge_map_rejects_legacy_candidate_pipeline(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(
        tmp_path,
        snapshot_id,
        run_id="pr_legacy_pipeline",
        candidate_pipeline_version="profile-preview-pipeline-v8",
    )

    with pytest.raises(ProfileKnowledgeMapError) as failure:
        asyncio.run(
            ProfileKnowledgeMapService(tmp_path).build(
                preview_run_ids=[run_id],
                provider=_GroupingProvider(),
                embedding_provider=FakeEmbeddingProvider(),
            )
        )

    assert failure.value.code == "knowledge_map_source_run_schema_incompatible"


def test_identity_parameter_change_creates_new_compile_contract_and_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    service = ProfileKnowledgeMapService(tmp_path)
    first = asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    assert first.manifest.compile_contract is not None

    monkeypatch.setattr(
        "app.wiki.profile_knowledge_map.TOPIC_IDENTITY_SIMILARITY_THRESHOLD",
        0.81,
    )
    second = asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
            force_new=True,
        )
    )
    assert second.manifest.compile_contract is not None

    assert (
        second.manifest.compile_contract.contract_hash
        != first.manifest.compile_contract.contract_hash
    )
    assert second.manifest.map_id != first.manifest.map_id
    assert second.manifest.compile_contract.topic_identity_parameters.similarity_threshold == 0.81


def test_same_compile_input_gets_distinct_attempt_identity(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    service = ProfileKnowledgeMapService(tmp_path)
    first = asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    second = asyncio.run(
        service.build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
            force_new=True,
        )
    )

    assert first.manifest.compile_input_key == second.manifest.compile_input_key
    assert first.manifest.compile_attempt_id != second.manifest.compile_attempt_id
    assert first.manifest.map_id != second.manifest.map_id
    vector_usage = next(
        item
        for item in second.manifest.usage
        if item.get("stage") == "topic_candidate_vector_cache"
    )
    assert vector_usage["cache_hit_count"] == 4
    assert vector_usage["embedding_api_call_count"] == 0


def test_compile_contract_rejects_tampered_parameters(tmp_path: Path) -> None:
    snapshot_id = _publish_snapshot(tmp_path)
    run_id = _seed_preview_run(tmp_path, snapshot_id)
    built = asyncio.run(
        ProfileKnowledgeMapService(tmp_path).build(
            preview_run_ids=[run_id],
            provider=_GroupingProvider(),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    contract = built.manifest.compile_contract
    assert contract is not None
    tampered = contract.model_dump(mode="json")
    tampered["topic_identity_parameters"]["similarity_threshold"] = 0.1

    with pytest.raises(ValidationError, match="contract hash"):
        type(contract).model_validate(tampered)


def test_identity_judge_contract_hash_binds_profile_identity() -> None:
    provider = _TopicIdentityProvider()
    first = profile_knowledge_map_module._topic_identity_judge_contract_hash(
        provider=provider,
        profile_context="AI Agent 安全与工程",
        profile_id="profile_a",
        profile_hash="a" * 64,
    )
    second = profile_knowledge_map_module._topic_identity_judge_contract_hash(
        provider=provider,
        profile_context="AI Agent 安全与工程",
        profile_id="profile_b",
        profile_hash="b" * 64,
    )

    assert first != second


def test_general_knowledge_map_prompts_do_not_embed_case_targets() -> None:
    source_paths = [
        Path(__file__).parents[1] / "app" / "wiki" / "profile_knowledge_map.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)

    for case_specific_token in (
        "c13-003",
        "cr_c13_003_fff9fbc82a83",
        "How we contain Claude",
        "doc_fd4dec9976e041308e2b39ed797c75d1",
        "filesystem_and_workspace_scope",
    ):
        assert case_specific_token not in source
    assert "你不回答问题" in source
    assert "Topic 身份判定器" in source
