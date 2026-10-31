from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.agent.runtime import AgentRuntime
from app.collections.models import SnapshotTotals
from app.collections.service import (
    CollectionSnapshotService,
    _expected_blocks_for_document,
    _with_manifest_hash,
)
from app.indexing.lancedb_index import (
    read_candidate_lancedb_rows,
    write_candidate_lancedb,
)
from app.main import create_app
from app.providers.chat import FakeChatProvider
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.service import RetrievalReadService
from app.wiki.build_models import WikiTopicSearchItem
from app.wiki.build_service import WikiBuildService, _source_diverse_order
from app.wiki.compilation_service import WikiCompilationService
from app.wiki.errors import WikiCompilationError
from app.wiki.narrative_service import WikiNarrativeService
from app.wiki.topic_identity import TopicIdentityCompiler
from app.wiki.topic_map import TopicMapCompiler, _split_blocks
from app.wiki.topic_prompt import build_topic_identity_messages

TOKEN = "wiki-topic-test-token"


class _MemoryCredentials:
    """测试 API 时提供独立 chat／embedding 凭证。"""

    def __init__(self) -> None:
        self.values = {
            "chat-api-key": "test-chat-key",
            "embedding-api-key": "test-embedding-key",
        }

    def get(self, username: str) -> str | None:
        return self.values.get(username)

    def set(self, username: str, value: str) -> None:
        self.values[username] = value

    def delete(self, username: str) -> None:
        self.values.pop(username, None)


@dataclass(frozen=True, slots=True)
class _PublishedCollection:
    snapshot_id: str
    first_block_id: str
    second_block_id: str


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        f"<p>{body}</p><p>{body} 的补充说明。</p>"
        "<p>补充正文用于确保提取、章节、切块和候选索引流程具有足够内容。</p>"
        "</article></body></html>"
    ).encode()


def test_topic_identity_prompt_declares_json_response_mode() -> None:
    messages = build_topic_identity_messages([])

    assert "JSON" in messages[0].content
    assert "json" in messages[0].content.lower()


def test_topic_catalog_round_robins_across_source_documents() -> None:
    items = [
        WikiTopicSearchItem(
            topic_id=f"wt_{index:016x}",
            title=f"topic-{index}",
            slug=f"topic-{index}",
            summary="summary",
            content_level="catalog",
            source_doc_count=1,
            source_doc_titles=[f"doc-{document_index}"],
            evidence_count=1,
            coverage_slot_labels=[],
            coverage_slot_count=0,
            match_sources=["dense"],
            dense_score=score,
        )
        for index, (document_index, score) in enumerate([(0, 0.9), (0, 0.8), (1, 0.7), (1, 0.6)])
    ]

    ordered = _source_diverse_order(
        items,
        source_doc_ids_by_topic={
            item.topic_id: [f"doc-{index}"] for item, index in zip(items, [0, 0, 1, 1], strict=True)
        },
    )

    assert [item.source_doc_titles[0] for item in ordered] == [
        "doc-0",
        "doc-1",
        "doc-0",
        "doc-1",
    ]


def _publish_collection(tmp_path: Path) -> _PublishedCollection:
    async def run() -> _PublishedCollection:
        first = tmp_path / "first.html"
        second = tmp_path / "second.html"
        first.write_bytes(_html("工作区隔离", "并行任务使用独立工作区。"))
        second.write_bytes(_html("恢复边界", "任务从检查点恢复并验证终态。"))
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        headers = {"x-launch-token": TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            job_ids: list[str] = []
            first_block_id = ""
            second_block_id = ""
            for index, source in enumerate((first, second)):
                preview = await client.post(
                    "/api/ingest/preview",
                    headers=headers,
                    json={"source_kind": "file", "source_value": str(source)},
                )
                preview.raise_for_status()
                job_id = preview.json()["job_id"]
                job_ids.append(job_id)
                persisted = await client.post(
                    "/api/ingest/persist",
                    headers=headers,
                    json={"job_id": job_id},
                )
                persisted.raise_for_status()
                selected_block = persisted.json()["block_set"]["blocks"][0]["block_id"]
                if index == 0:
                    first_block_id = selected_block
                else:
                    second_block_id = selected_block
                indexed = await client.post(
                    "/api/ingest/index",
                    headers=headers,
                    json={"job_id": job_id},
                )
                indexed.raise_for_status()
            published = await client.post(
                "/api/collections/publish",
                headers=headers,
                json={"job_ids": job_ids},
            )
            published.raise_for_status()
            return _PublishedCollection(
                snapshot_id=published.json()["manifest"]["snapshot_id"],
                first_block_id=first_block_id,
                second_block_id=second_block_id,
            )

    return asyncio.run(run())


def _publish_additional_document(
    tmp_path: Path,
    *,
    title: str,
    body: str,
) -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        source = tmp_path / f"{title}.html"
        source.write_bytes(_html(title, body))
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        headers = {"x-launch-token": TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            preview = await client.post(
                "/api/ingest/preview",
                headers=headers,
                json={"source_kind": "file", "source_value": str(source)},
            )
            preview.raise_for_status()
            job_id = preview.json()["job_id"]
            persisted = await client.post(
                "/api/ingest/persist",
                headers=headers,
                json={"job_id": job_id},
            )
            persisted.raise_for_status()
            indexed = await client.post(
                "/api/ingest/index",
                headers=headers,
                json={"job_id": job_id},
            )
            indexed.raise_for_status()
            published = await client.post(
                "/api/collections/publish",
                headers=headers,
                json={"job_ids": [job_id]},
            )
            published.raise_for_status()
            return (
                published.json()["manifest"]["snapshot_id"],
                persisted.json()["document"]["doc_id"],
            )

    return asyncio.run(run())


def _snapshot_without_document(
    tmp_path: Path,
    *,
    source_snapshot_id: str,
    removed_doc_id: str,
    target_snapshot_id: str,
) -> str:
    service = CollectionSnapshotService(tmp_path)
    source_manifest = service.snapshot(source_snapshot_id)
    source_root = tmp_path / "collections" / source_snapshot_id
    target_root = tmp_path / "collections" / target_snapshot_id
    shutil.copytree(source_root, target_root)
    removed_document = next(
        document for document in source_manifest.documents if document.doc_id == removed_doc_id
    )
    shutil.rmtree(target_root / "documents" / removed_document.doc_id)
    expected_blocks = {
        block_id
        for document in source_manifest.documents
        for block_id in _expected_blocks_for_document(source_root, document)
    }
    dimension = (
        source_manifest.versions.embedding_profile.dimension
        if source_manifest.versions is not None
        else 0
    )
    rows = read_candidate_lancedb_rows(
        source_root / "index" / "lance",
        expected_block_ids=expected_blocks,
        dimension=dimension,
    )
    retained_rows = [row for row in rows if str(row["doc_id"]) != removed_doc_id]
    shutil.rmtree(target_root / "index" / "lance")
    write_candidate_lancedb(
        target_root / "index" / "lance",
        retained_rows,
        dimension=dimension,
    )
    documents = [
        document for document in source_manifest.documents if document.doc_id != removed_doc_id
    ]
    manifest = source_manifest.model_copy(
        update={
            "snapshot_id": target_snapshot_id,
            "parent_snapshot_id": source_snapshot_id,
            "documents": documents,
            "failed_documents": [],
            "totals": SnapshotTotals(
                documents=len(documents),
                sections=sum(document.section_count for document in documents),
                blocks=sum(document.block_count for document in documents),
                table_blocks=sum(document.table_block_count for document in documents),
                embedding_requests=source_manifest.totals.embedding_requests,
                embedding_input_tokens=source_manifest.totals.embedding_input_tokens,
            ),
            "index_ref": f"collections/{target_snapshot_id}/index/lance",
            "keyword_index_ref": (
                f"collections/{target_snapshot_id}/index/lance#blocks.search_terms"
            ),
        }
    )
    manifest = _with_manifest_hash(manifest)
    (target_root / "manifest.json").write_text(
        manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return target_snapshot_id


def _candidate_response(*, block_id: str, name: str, aliases: list[str]) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "name": name,
                    "aliases": aliases,
                    "summary": f"{name} 的语料内摘要。",
                    "semantic_tags": ["concept"],
                    "source_block_ids": [block_id],
                    "coverage_slots": [
                        {
                            "label": "边界",
                            "question": f"{name} 限制什么？",
                            "source_block_ids": [block_id],
                        }
                    ],
                }
            ]
        },
        ensure_ascii=False,
    )


def _tool_call(tool: str, arguments: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "tool_call",
            "decision_summary": "验证 Wiki 工具路由。",
            "tool": tool,
            "arguments": arguments,
        },
        ensure_ascii=False,
    )


def _finish(answer: str) -> str:
    return json.dumps(
        {
            "type": "finish",
            "answer": answer,
            "decision_summary": "工具结果已足够。",
        },
        ensure_ascii=False,
    )


def test_topic_map_covers_all_blocks_and_binds_evidence(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    provider = FakeChatProvider(
        [
            _candidate_response(
                block_id=collection.first_block_id,
                name="工作区隔离",
                aliases=["独立工作区"],
            ),
            _candidate_response(
                block_id=collection.second_block_id,
                name="检查点恢复",
                aliases=["断点续跑"],
            ),
        ]
    )

    result = asyncio.run(
        TopicMapCompiler(tmp_path).compile(
            collection.snapshot_id,
            provider=provider,
        )
    )

    assert result.manifest.candidate_count == 2
    assert result.manifest.block_count == 2
    assert all(len(document.processed_block_ids) > 0 for document in result.documents)
    assert result.manifest.block_count == sum(
        len(document.processed_block_ids) for document in result.documents
    )
    loaded = TopicMapCompiler(tmp_path).load(collection.snapshot_id, result.manifest.map_id)
    assert loaded.documents == result.documents


def test_topic_identity_merges_exact_aliases_without_type_key(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    map_provider = FakeChatProvider(
        [
            _candidate_response(
                block_id=collection.first_block_id,
                name="检索增强生成",
                aliases=["RAG", "Retrieval-Augmented Generation"],
            ),
            _candidate_response(
                block_id=collection.second_block_id,
                name="RAG",
                aliases=["检索增强生成"],
            ),
        ]
    )
    topic_map = asyncio.run(
        TopicMapCompiler(tmp_path).compile(
            collection.snapshot_id,
            provider=map_provider,
        )
    )
    ledger = asyncio.run(
        TopicIdentityCompiler(tmp_path).compile(
            collection.snapshot_id,
            topic_map_ids=[topic_map.manifest.map_id],
            provider=FakeChatProvider(["unused"]),
        )
    )

    assert len(ledger.topics) == 1
    topic = ledger.topics[0]
    assert set(topic.evidence_block_ids) == {
        collection.first_block_id,
        collection.second_block_id,
    }
    assert "RAG" in topic.aliases or "Retrieval-Augmented Generation" in topic.aliases
    assert topic.semantic_tags == ["concept"]


def test_topic_identity_reuses_base_topic_id(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    response = [
        _candidate_response(
            block_id=collection.first_block_id,
            name="工作区隔离",
            aliases=["独立工作区"],
        ),
        _candidate_response(
            block_id=collection.second_block_id,
            name="检查点恢复",
            aliases=["断点续跑"],
        ),
    ]
    first_map = asyncio.run(
        TopicMapCompiler(tmp_path).compile(
            collection.snapshot_id,
            provider=FakeChatProvider(response),
        )
    )
    first_ledger = asyncio.run(
        TopicIdentityCompiler(tmp_path).compile(
            collection.snapshot_id,
            topic_map_ids=[first_map.manifest.map_id],
            provider=FakeChatProvider(["unused"]),
        )
    )
    second_map = asyncio.run(
        TopicMapCompiler(tmp_path).compile(
            collection.snapshot_id,
            provider=FakeChatProvider(response),
        )
    )
    second_ledger = asyncio.run(
        TopicIdentityCompiler(tmp_path).compile(
            collection.snapshot_id,
            topic_map_ids=[second_map.manifest.map_id],
            provider=FakeChatProvider(["unused"]),
            base_ledger_id=first_ledger.manifest.ledger_id,
        )
    )

    first_ids = {(topic.title, topic.topic_id) for topic in first_ledger.topics}
    second_ids = {(topic.title, topic.topic_id) for topic in second_ledger.topics}
    assert first_ids == second_ids


def test_topic_map_rejects_cross_window_evidence(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    provider = FakeChatProvider(
        [
            _candidate_response(
                block_id=collection.first_block_id,
                name="工作区隔离",
                aliases=[],
            ),
            _candidate_response(
                block_id=collection.first_block_id,
                name="恢复边界",
                aliases=[],
            ),
        ]
    )

    with pytest.raises(WikiCompilationError) as failure:
        asyncio.run(
            TopicMapCompiler(tmp_path).compile(
                collection.snapshot_id,
                provider=provider,
                block_window_size=1,
            )
        )

    assert failure.value.code in {
        "topic_map_source_out_of_scope",
        "topic_map_candidate_unsupported",
        "topic_map_provider_failed",
    }


def test_wiki_build_search_and_evidence_are_snapshot_bound(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    topic_map = asyncio.run(
        TopicMapCompiler(tmp_path).compile(
            collection.snapshot_id,
            provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=collection.first_block_id,
                        name="工作区隔离",
                        aliases=["独立工作区"],
                    ),
                    _candidate_response(
                        block_id=collection.second_block_id,
                        name="检查点恢复",
                        aliases=["断点续跑", "checkpoint recovery"],
                    ),
                ]
            ),
        )
    )
    ledger = asyncio.run(
        TopicIdentityCompiler(tmp_path).compile(
            collection.snapshot_id,
            topic_map_ids=[topic_map.manifest.map_id],
            provider=FakeChatProvider(["unused"]),
        )
    )
    build_service = WikiBuildService(tmp_path)
    build = asyncio.run(
        build_service.publish(
            collection.snapshot_id,
            ledger_id=ledger.manifest.ledger_id,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    search = asyncio.run(
        build_service.search_topics(
            collection.snapshot_id,
            "断点续跑",
            semantic_tag=None,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    assert search.build_id == build.build_id
    assert search.topics[0].title == "检查点恢复"
    assert "alias" in search.topics[0].match_sources
    assert search.topics[0].evidence_count == 1
    assert search.topics[0].source_doc_count == 1
    assert search.topics[0].source_doc_titles == ["恢复边界"]
    assert search.topics[0].coverage_slot_labels == ["边界"]
    evidence = build_service.get_topic_evidence(
        collection.snapshot_id,
        [search.topics[0].topic_id],
    )
    assert [block.block_id for block in evidence.topics[0].blocks] == [collection.second_block_id]

    with pytest.raises(WikiCompilationError) as failure:
        build_service.active_for_snapshot("s_0000000000000000")
    assert failure.value.code == "wiki_build_not_ready_for_snapshot"


def test_narrative_page_materializes_with_paragraph_evidence(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    topic_map = asyncio.run(
        TopicMapCompiler(tmp_path).compile(
            collection.snapshot_id,
            provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=collection.first_block_id,
                        name="检索增强生成",
                        aliases=["RAG"],
                    ),
                    _candidate_response(
                        block_id=collection.second_block_id,
                        name="RAG",
                        aliases=["检索增强生成"],
                    ),
                ]
            ),
        )
    )
    ledger = asyncio.run(
        TopicIdentityCompiler(tmp_path).compile(
            collection.snapshot_id,
            topic_map_ids=[topic_map.manifest.map_id],
            provider=FakeChatProvider(["unused"]),
        )
    )
    build_service = WikiBuildService(tmp_path)
    build = asyncio.run(
        build_service.publish(
            collection.snapshot_id,
            ledger_id=ledger.manifest.ledger_id,
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    assert build.counts.narrative == 1
    narrative_provider = FakeChatProvider(
        [
            json.dumps(
                {
                    "summary": "RAG 把检索结果与生成结合起来。",
                    "sections": [
                        {
                            "heading": "机制",
                            "paragraphs": [
                                {
                                    "text": "两个来源共同描述了检索增强生成。",
                                    "source_block_ids": [
                                        collection.first_block_id,
                                        collection.second_block_id,
                                    ],
                                }
                            ],
                        }
                    ],
                    "related_topic_ids": [],
                },
                ensure_ascii=False,
            )
        ]
    )
    narrative = WikiNarrativeService(tmp_path)
    materialized = asyncio.run(
        narrative.materialize(
            collection.snapshot_id,
            provider=narrative_provider,
        )
    )
    assert len(materialized.pages) == 1
    page = narrative.read_page(
        collection.snapshot_id,
        materialized.pages[0].topic_id,
    ).page
    assert page.content_level == "narrative"
    assert page.sections[0].paragraphs[0].source_block_ids == [
        collection.first_block_id,
        collection.second_block_id,
    ]

    artifact_path = tmp_path / page.artifact_ref
    artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact_payload["sections"][0]["paragraphs"][0]["text"] += "（篡改）"
    artifact_path.write_text(json.dumps(artifact_payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(WikiCompilationError) as failure:
        narrative.read_page(collection.snapshot_id, page.topic_id)
    assert failure.value.code == "wiki_page_invalid"


def test_incremental_compile_reuses_unchanged_documents(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    compiler = WikiCompilationService(tmp_path)
    full = asyncio.run(
        compiler.compile_full(
            collection.snapshot_id,
            chat_provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=collection.first_block_id,
                        name="工作区隔离",
                        aliases=["独立工作区"],
                    ),
                    _candidate_response(
                        block_id=collection.second_block_id,
                        name="检查点恢复",
                        aliases=["断点续跑"],
                    ),
                ]
            ),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )

    snapshot_id, changed_doc_id = _publish_additional_document(
        tmp_path,
        title="审计日志",
        body="系统记录敏感操作并支持审计。",
    )
    # The new snapshot contains three documents; reuse the old maps for the first two.
    new_reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    changed_document = next(
        document for document in new_reader.manifest.documents if document.doc_id == changed_doc_id
    )
    changed_block_id = new_reader.list_blocks(changed_document.doc_id).blocks[0].block_id
    incremental = asyncio.run(
        compiler.compile_incremental(
            snapshot_id,
            chat_provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=changed_block_id,
                        name="审计日志",
                        aliases=["操作审计"],
                    )
                ]
            ),
            embedding_provider=FakeEmbeddingProvider(),
            base_build_id=full.build.build_id,
        )
    )

    assert incremental.changed_document_ids == [changed_doc_id]
    assert incremental.removed_document_ids == []
    assert incremental.build.base_build_id == full.build.build_id
    assert len(incremental.topic_map_ids) == 2
    assert "审计日志" in _topic_titles(tmp_path, incremental.build.build_id)


def test_agent_runtime_can_use_wiki_topic_search_when_build_matches(
    tmp_path: Path,
) -> None:
    collection = _publish_collection(tmp_path)
    compiler = WikiCompilationService(tmp_path)
    compiled = asyncio.run(
        compiler.compile_full(
            collection.snapshot_id,
            chat_provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=collection.first_block_id,
                        name="工作区隔离",
                        aliases=["独立工作区"],
                    ),
                    _candidate_response(
                        block_id=collection.second_block_id,
                        name="检查点恢复",
                        aliases=["断点续跑"],
                    ),
                ]
            ),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    agent_provider = FakeChatProvider(
        [
            _tool_call("search_wiki_topics", {"query": "断点续跑", "semantic_tag": None}),
            _finish("Wiki 中存在检查点恢复主题。"),
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=agent_provider,
        embedding_provider=FakeEmbeddingProvider(),
        enable_wiki_tools=True,
    )

    result = asyncio.run(
        runtime.run(
            "知识库中恢复边界有哪些？",
            snapshot_id=collection.snapshot_id,
        )
    )

    assert result.status == "succeeded"
    assert result.tool_calls == 1
    tool_event = next(event for event in result.events if event.event_type == "tool_call")
    assert tool_event.payload["tool"] == "search_wiki_topics"
    assert tool_event.payload["diagnostics"]["build_id"] == compiled.build.build_id
    assert tool_event.payload["diagnostics"]["topic_ids"]


def test_incremental_compile_retracts_removed_document(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    compiler = WikiCompilationService(tmp_path)
    full = asyncio.run(
        compiler.compile_full(
            collection.snapshot_id,
            chat_provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=collection.first_block_id,
                        name="工作区隔离",
                        aliases=["独立工作区"],
                    ),
                    _candidate_response(
                        block_id=collection.second_block_id,
                        name="检查点恢复",
                        aliases=["断点续跑"],
                    ),
                ]
            ),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    reader = RetrievalReadService(tmp_path).bind_snapshot(collection.snapshot_id)
    removed_doc_id = next(
        document.doc_id for document in reader.manifest.documents if document.title == "工作区隔离"
    )
    deleted_snapshot_id = "s_1234567890abcdef"
    _snapshot_without_document(
        tmp_path,
        source_snapshot_id=collection.snapshot_id,
        removed_doc_id=removed_doc_id,
        target_snapshot_id=deleted_snapshot_id,
    )

    incremental = asyncio.run(
        compiler.compile_incremental(
            deleted_snapshot_id,
            chat_provider=FakeChatProvider(["unused"]),
            embedding_provider=FakeEmbeddingProvider(),
            base_build_id=full.build.build_id,
        )
    )

    assert incremental.removed_document_ids == [removed_doc_id]
    assert _topic_titles(tmp_path, incremental.build.build_id) == {"检查点恢复"}
    assert "工作区隔离" in _topic_titles(tmp_path, full.build.build_id)


def test_incremental_compile_copies_unaffected_narrative_page(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)
    compiler = WikiCompilationService(tmp_path)
    full = asyncio.run(
        compiler.compile_full(
            collection.snapshot_id,
            chat_provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=collection.first_block_id,
                        name="检索增强生成",
                        aliases=["RAG"],
                    ),
                    _candidate_response(
                        block_id=collection.second_block_id,
                        name="RAG",
                        aliases=["检索增强生成"],
                    ),
                    json.dumps(
                        {
                            "summary": "RAG 的跨源摘要。",
                            "sections": [
                                {
                                    "heading": "概述",
                                    "paragraphs": [
                                        {
                                            "text": "两个来源共同支撑该主题。",
                                            "source_block_ids": [
                                                collection.first_block_id,
                                                collection.second_block_id,
                                            ],
                                        }
                                    ],
                                }
                            ],
                            "related_topic_ids": [],
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            embedding_provider=FakeEmbeddingProvider(),
        )
    )
    assert full.materialized_pages == 1
    snapshot_id, changed_doc_id = _publish_additional_document(
        tmp_path,
        title="审计附件",
        body="审计附件记录操作过程。",
    )
    new_reader = RetrievalReadService(tmp_path).bind_snapshot(snapshot_id)
    changed_block_id = new_reader.list_blocks(changed_doc_id).blocks[0].block_id
    incremental = asyncio.run(
        compiler.compile_incremental(
            snapshot_id,
            chat_provider=FakeChatProvider(
                [
                    _candidate_response(
                        block_id=changed_block_id,
                        name="审计附件",
                        aliases=["操作记录附件"],
                    )
                ]
            ),
            embedding_provider=FakeEmbeddingProvider(),
            base_build_id=full.build.build_id,
        )
    )

    assert incremental.materialized_pages == 0
    assert incremental.copied_pages == 1
    narrative = WikiNarrativeService(tmp_path)
    page_topics = narrative.page_topic_ids(incremental.build.build_id)
    assert len(page_topics) == 1
    page = narrative.read_page(snapshot_id, next(iter(page_topics))).page
    assert page.sections[0].paragraphs[0].source_block_ids == [
        collection.first_block_id,
        collection.second_block_id,
    ]


def test_wiki_compile_api_publishes_snapshot_bound_build(tmp_path: Path) -> None:
    collection = _publish_collection(tmp_path)

    async def run() -> tuple[int, dict[str, object], int]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            credential_store=_MemoryCredentials(),
            embedding_provider_factory=FakeEmbeddingProvider,
            chat_provider_factory=lambda _key: FakeChatProvider(
                [
                    _candidate_response(
                        block_id=collection.first_block_id,
                        name="工作区隔离",
                        aliases=["独立工作区"],
                    ),
                    _candidate_response(
                        block_id=collection.second_block_id,
                        name="检查点恢复",
                        aliases=["断点续跑"],
                    ),
                ]
            ),
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            compiled = await client.post(
                "/api/wiki/compile",
                headers={"x-launch-token": TOKEN},
                json={"snapshot_id": collection.snapshot_id},
            )
            current = await client.get(
                f"/api/wiki/current/{collection.snapshot_id}",
                headers={"x-launch-token": TOKEN},
            )
            return compiled.status_code, compiled.json(), current.status_code

    compile_status, payload, current_status = asyncio.run(run())
    assert compile_status == 200
    assert payload["snapshot_id"] == collection.snapshot_id
    assert payload["build"]["snapshot_id"] == collection.snapshot_id
    assert current_status == 200


def test_agent_without_matching_wiki_build_keeps_block_tools_and_rejects_wiki_tool(
    tmp_path: Path,
) -> None:
    collection = _publish_collection(tmp_path)
    provider = FakeChatProvider(
        [
            _tool_call("search_wiki_topics", {"query": "任意主题", "semantic_tag": None}),
            _finish("当前工具不可用。"),
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        chat_provider=provider,
        embedding_provider=FakeEmbeddingProvider(),
        enable_wiki_tools=True,
    )

    result = asyncio.run(
        runtime.run(
            "知识库里有什么主题？",
            snapshot_id=collection.snapshot_id,
        )
    )

    assert result.status == "succeeded"
    assert any(event.event_type == "tool_unavailable" for event in result.events)
    wiki_event = next(event for event in result.events if event.event_type == "wiki_status")
    assert wiki_event.payload == {
        "status": "not_ready_for_snapshot",
        "build_id": None,
    }
    assert '"wiki_status":"not_ready_for_snapshot"' in provider.requests[0][0].content


def test_wiki_prompt_modules_do_not_contain_case_specific_probe_terms() -> None:
    prompt_paths = [
        Path(__file__).parents[1] / "app" / "wiki" / "topic_prompt.py",
        Path(__file__).parents[1] / "app" / "wiki" / "narrative_prompt.py",
    ]
    forbidden = (
        "c13-003",
        "worktree",
        "checkpoint_resume",
        "checkpoint_evaluation",
        "doc_983ba4e2cbff4185873d525a69195fdf",
    )
    for path in prompt_paths:
        text = path.read_text(encoding="utf-8").casefold()
        assert not any(value.casefold() in text for value in forbidden)


def test_topic_window_partition_covers_one_hundred_thousand_blocks() -> None:
    class _TextOnlyBlock:
        def __init__(self, index: int) -> None:
            self.text = f"block-{index}"

    blocks = [_TextOnlyBlock(index) for index in range(100_000)]
    windows = _split_blocks(blocks, max_blocks=32, max_chars=1_000)

    flattened = [block for window in windows for block in window]
    assert flattened == blocks
    assert all(1 <= len(window) <= 32 for window in windows)


def _topic_titles(tmp_path: Path, build_id: str) -> set[str]:
    from app.storage.database import StorageDatabase
    from app.storage.wiki_models import WikiTopicRow

    database = StorageDatabase(tmp_path)
    with database.session() as session:
        return {
            row.title
            for row in session.execute(
                select(WikiTopicRow).where(WikiTopicRow.build_id == build_id)
            ).scalars()
        }
