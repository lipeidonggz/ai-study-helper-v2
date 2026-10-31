from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.providers.embeddings import EmbeddingProvider, EmbeddingProviderError
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import RetrievalBlock
from app.retrieval.service import RetrievalReadService, RetrievalSnapshotReader
from app.storage.database import StorageDatabase
from app.storage.models import utc_iso
from app.storage.wiki_models import (
    ActiveWikiPointerRow,
    WikiBuildDocumentMapRow,
    WikiBuildRow,
    WikiCoverageSlotRow,
    WikiSlotEvidenceRow,
    WikiTopicAliasRow,
    WikiTopicEvidenceRow,
    WikiTopicRow,
    WikiTopicVectorRow,
)
from app.wiki.build_models import (
    WikiBuildCounts,
    WikiBuildResult,
    WikiContentLevel,
    WikiCoverageSlotView,
    WikiMatchSource,
    WikiTopicDetail,
    WikiTopicEvidenceGroup,
    WikiTopicEvidenceResult,
    WikiTopicSearchItem,
    WikiTopicSearchResult,
)
from app.wiki.errors import WikiCompilationError
from app.wiki.topic_identity import TopicIdentityCompiler
from app.wiki.topic_map import TopicMapCompiler, normalize_topic_name
from app.wiki.topic_models import WikiTopicLedgerEntry, WikiTopicLedgerResult

WIKI_BUILD_COMPILER_VERSION = "wiki-topic-build-v1"
WIKI_BUILD_ID_PATTERN = re.compile(r"^wb_[0-9a-f]{16}$")
DENSE_MIN_SIMILARITY = 0.15


class WikiBuildService:
    """原子发布并读取绑定 collection snapshot 的 Wiki topic build。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._database = StorageDatabase(self._data_root)
        self._retrieval = RetrievalReadService(self._data_root)
        self._topic_maps = TopicMapCompiler(self._data_root)
        self._identities = TopicIdentityCompiler(self._data_root)

    async def publish(
        self,
        snapshot_id: str,
        *,
        ledger_id: str,
        embedding_provider: EmbeddingProvider,
        base_build_id: str | None = None,
    ) -> WikiBuildResult:
        """从 topic ledger 构建并原子发布一个完整 Wiki build。"""

        reader = self._bind_snapshot(snapshot_id)
        ledger = self._identities.load(ledger_id)
        if (
            ledger.manifest.snapshot_id != snapshot_id
            or ledger.manifest.snapshot_manifest_hash != reader.manifest.manifest_hash
        ):
            raise WikiCompilationError(
                "wiki_snapshot_mismatch",
                "topic ledger is not bound to the requested snapshot",
            )
        maps = [
            self._topic_maps.load(snapshot_id, map_id) for map_id in ledger.manifest.topic_map_ids
        ]
        document_maps = [
            WikiBuildDocumentMapRow(
                build_id="",
                doc_id=document.doc_id,
                doc_version=document.doc_version,
                topic_map_id=topic_map.manifest.map_id,
            )
            for topic_map in maps
            for document in topic_map.documents
        ]
        blocks = self._load_evidence_blocks(reader, ledger)
        topics_with_levels = [(topic, _content_level(topic)) for topic in ledger.topics]
        vectors, _ = await _embed_topics(
            topics_with_levels,
            permission_provider=embedding_provider,
        )
        counts = WikiBuildCounts(
            topics=len(topics_with_levels),
            catalog_only=sum(level == "catalog" for _, level in topics_with_levels),
            narrative=sum(level == "narrative" for _, level in topics_with_levels),
            evidence_refs=sum(len(topic.evidence_block_ids) for topic in ledger.topics),
            coverage_slots=sum(len(topic.coverage_slots) for topic in ledger.topics),
        )
        pipeline_versions = {
            "compiler_version": WIKI_BUILD_COMPILER_VERSION,
            "ledger_id": ledger.manifest.ledger_id,
            "topic_map_version": ledger.manifest.topic_map_ids,
            "topic_identity_version": ledger.manifest.resolver_version,
            "topic_identity_prompt_hash": ledger.manifest.prompt_template_hash,
            "embedding_profile": embedding_provider.profile.model_dump(mode="json"),
        }
        manifest_hash = _build_manifest_hash(
            snapshot_id=snapshot_id,
            snapshot_manifest_hash=reader.manifest.manifest_hash,
            base_build_id=base_build_id,
            ledger=ledger,
            counts=counts,
            pipeline_versions=pipeline_versions,
        )
        build_id = f"wb_{os.urandom(8).hex()}"
        created_at = datetime.now(UTC)
        for mapping in document_maps:
            mapping.build_id = build_id
        try:
            with self._database.session() as session:
                session.add(
                    WikiBuildRow(
                        build_id=build_id,
                        snapshot_id=snapshot_id,
                        snapshot_manifest_hash=reader.manifest.manifest_hash,
                        base_build_id=base_build_id,
                        status="ready",
                        compiler_version=WIKI_BUILD_COMPILER_VERSION,
                        pipeline_versions_json=json.dumps(
                            pipeline_versions,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        counts_json=counts.model_dump_json(),
                        manifest_hash=manifest_hash,
                        created_at=created_at,
                        published_at=created_at,
                    )
                )
                session.flush()
                session.add_all(document_maps)
                self._insert_topic_rows(
                    session,
                    build_id=build_id,
                    topics_with_levels=topics_with_levels,
                    blocks=blocks,
                )
                self._insert_vectors(
                    session,
                    build_id=build_id,
                    vectors=vectors,
                    provider=embedding_provider,
                )
                self._insert_fts(
                    session,
                    build_id=build_id,
                    topics_with_levels=topics_with_levels,
                )
                self._publish_pointer(
                    session,
                    build_id=build_id,
                    snapshot_id=snapshot_id,
                )
                session.commit()
        except SQLAlchemyError as error:
            raise WikiCompilationError(
                "wiki_build_write_failed",
                f"could not publish Wiki build: {type(error).__name__}",
            ) from error
        return _build_result(
            build_id=build_id,
            reader=reader,
            base_build_id=base_build_id,
            manifest_hash=manifest_hash,
            counts=counts,
            created_at=created_at,
            ledger_id=ledger.manifest.ledger_id,
            topic_map_ids=ledger.manifest.topic_map_ids,
        )

    def active_for_snapshot(self, snapshot_id: str) -> WikiBuildResult:
        """读取指定 snapshot 的 active Wiki build。"""

        with self._database.session() as session:
            pointer = session.get(ActiveWikiPointerRow, 1)
            if pointer is None:
                raise WikiCompilationError(
                    "wiki_build_not_ready_for_snapshot",
                    f"no Wiki build is active for snapshot {snapshot_id}",
                )
            if pointer.snapshot_id != snapshot_id:
                raise WikiCompilationError(
                    "wiki_build_not_ready_for_snapshot",
                    f"active Wiki build targets snapshot {pointer.snapshot_id}, not {snapshot_id}",
                )
            build = session.get(WikiBuildRow, pointer.build_id)
            if build is None or build.status != "ready":
                raise WikiCompilationError(
                    "wiki_build_not_ready_for_snapshot",
                    f"active Wiki build is unavailable: {pointer.build_id}",
                )
            return _row_to_build_result(build)

    def build_by_id(self, build_id: str) -> WikiBuildResult:
        """按 build ID 读取记录，不要求它仍是 active。"""

        if not WIKI_BUILD_ID_PATTERN.fullmatch(build_id):
            raise WikiCompilationError(
                "wiki_build_not_found",
                f"invalid Wiki build id: {build_id}",
            )
        with self._database.session() as session:
            build = session.get(WikiBuildRow, build_id)
            if build is None:
                raise WikiCompilationError(
                    "wiki_build_not_found",
                    f"Wiki build does not exist: {build_id}",
                )
            return _row_to_build_result(build)

    async def search_topics(
        self,
        snapshot_id: str,
        query: str,
        *,
        semantic_tag: str | None,
        embedding_provider: EmbeddingProvider,
    ) -> WikiTopicSearchResult:
        """在匹配 snapshot 的 active build 中搜索全部直接匹配和合格语义候选。"""

        normalized_query = query.strip()
        if not normalized_query:
            raise WikiCompilationError("wiki_request_invalid", "query must not be empty")
        build = self.active_for_snapshot(snapshot_id)
        reader = self._bind_snapshot(snapshot_id)
        document_titles = {
            document.doc_id: document.title for document in reader.manifest.documents
        }
        dense_profile = embedding_provider.profile.model_dump(mode="json")
        try:
            query_result = await embedding_provider.embed_query(normalized_query)
        except EmbeddingProviderError as error:
            raise WikiCompilationError(
                "wiki_search_embedding_failed",
                error.message,
                retryable=error.retryable,
            ) from error
        query_vector = query_result.vector
        query_norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
        with self._database.session() as session:
            topics = list(
                session.execute(
                    select(WikiTopicRow).where(WikiTopicRow.build_id == build.build_id)
                ).scalars()
            )
            aliases = list(
                session.execute(
                    select(WikiTopicAliasRow).where(WikiTopicAliasRow.build_id == build.build_id)
                ).scalars()
            )
            evidence = list(
                session.execute(
                    select(WikiTopicEvidenceRow).where(
                        WikiTopicEvidenceRow.build_id == build.build_id
                    )
                ).scalars()
            )
            slots = list(
                session.execute(
                    select(WikiCoverageSlotRow).where(
                        WikiCoverageSlotRow.build_id == build.build_id
                    )
                ).scalars()
            )
            vector_rows = list(
                session.execute(
                    select(WikiTopicVectorRow).where(WikiTopicVectorRow.build_id == build.build_id)
                ).scalars()
            )
            fts_ids = self._fts_search(session, build.build_id, normalized_query)

        aliases_by_topic: dict[str, list[WikiTopicAliasRow]] = {}
        for alias in aliases:
            aliases_by_topic.setdefault(alias.topic_id, []).append(alias)
        evidence_by_topic: dict[str, list[str]] = {}
        docs_by_topic: dict[str, set[str]] = {}
        for ref in evidence:
            evidence_by_topic.setdefault(ref.topic_id, []).append(ref.block_id)
            docs_by_topic.setdefault(ref.topic_id, set()).add(ref.doc_id)
        coverage_labels_by_topic: dict[str, list[str]] = {}
        coverage_slot_counts: dict[str, int] = {}
        for slot in slots:
            labels = coverage_labels_by_topic.setdefault(slot.topic_id, [])
            if slot.label not in labels:
                labels.append(slot.label)
            coverage_slot_counts[slot.topic_id] = coverage_slot_counts.get(slot.topic_id, 0) + 1
        direct_ids: set[str] = set(fts_ids)
        matched_aliases: dict[str, list[str]] = {}
        normalized_query = normalize_topic_name(normalized_query)
        for topic in topics:
            searchable = normalize_topic_name(
                f"{topic.title} {topic.canonical_name} {topic.summary}"
            )
            if normalized_query in searchable:
                direct_ids.add(topic.topic_id)
            for alias in aliases_by_topic.get(topic.topic_id, []):
                if normalized_query in alias.normalized_alias:
                    direct_ids.add(topic.topic_id)
                    matched_aliases.setdefault(topic.topic_id, []).append(alias.alias)

        vectors_by_id = {row.topic_id: row for row in vector_rows}
        scores: dict[str, float] = {}
        for topic_id, row in vectors_by_id.items():
            if (
                row.provider != embedding_provider.profile.provider
                or row.model_id != embedding_provider.profile.model_id
                or row.dimension != embedding_provider.profile.dimension
            ):
                raise WikiCompilationError(
                    "wiki_search_profile_mismatch",
                    "query embedding profile does not match the Wiki build",
                )
            vector = struct.unpack(f"<{row.dimension}f", row.vector)
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True)) / (
                query_norm * row.norm
            )
            if score >= DENSE_MIN_SIMILARITY:
                scores[topic_id] = score
        selected_ids = direct_ids | set(scores)
        topic_by_id = {topic.topic_id: topic for topic in topics}
        items: list[WikiTopicSearchItem] = []
        normalized_tag = normalize_topic_name(semantic_tag) if semantic_tag else None
        for topic_id in selected_ids:
            topic = topic_by_id.get(topic_id)
            if topic is None:
                continue
            tags = json.loads(topic.semantic_tags_json)
            if normalized_tag and normalized_tag not in {
                normalize_topic_name(value) for value in tags
            }:
                continue
            sources: list[WikiMatchSource] = []
            if topic_id in fts_ids:
                sources.append("fts")
            if topic_id in matched_aliases:
                sources.append("alias")
            if normalized_query in normalize_topic_name(
                f"{topic.title} {topic.canonical_name} {topic.summary}"
            ):
                sources.append("title")
            if topic_id in scores:
                sources.append("dense")
            items.append(
                WikiTopicSearchItem(
                    topic_id=topic.topic_id,
                    title=topic.title,
                    slug=topic.slug,
                    summary=topic.summary,
                    matched_aliases=sorted(set(matched_aliases.get(topic_id, []))),
                    semantic_tags=tags,
                    content_level=cast(WikiContentLevel, topic.content_level),
                    source_doc_count=len(docs_by_topic.get(topic_id, set())),
                    source_doc_titles=sorted(
                        {document_titles[doc_id] for doc_id in docs_by_topic.get(topic_id, set())}
                    ),
                    evidence_count=len(evidence_by_topic.get(topic_id, [])),
                    coverage_slot_labels=coverage_labels_by_topic.get(topic_id, []),
                    coverage_slot_count=coverage_slot_counts.get(topic_id, 0),
                    match_sources=list(dict.fromkeys(sources)),
                    dense_score=scores.get(topic_id),
                )
            )
        items = _source_diverse_order(
            items,
            source_doc_ids_by_topic={
                topic_id: sorted(doc_ids) for topic_id, doc_ids in docs_by_topic.items()
            },
        )
        return WikiTopicSearchResult(
            snapshot_id=snapshot_id,
            build_id=build.build_id,
            topics=items,
            total=len(items),
            dense_profile=dense_profile,
        )

    def get_topic_evidence(
        self,
        snapshot_id: str,
        topic_ids: list[str],
    ) -> WikiTopicEvidenceResult:
        """返回指定 topic 的完整当前 snapshot block 证据。"""

        if not topic_ids:
            raise WikiCompilationError("wiki_request_invalid", "topic_ids must not be empty")
        build = self.active_for_snapshot(snapshot_id)
        unique_ids = list(dict.fromkeys(value.strip() for value in topic_ids if value.strip()))
        if len(unique_ids) != len(topic_ids):
            raise WikiCompilationError(
                "wiki_request_invalid",
                "topic_ids must be unique and non-empty",
            )
        with self._database.session() as session:
            topics = {
                row.topic_id: row
                for row in session.execute(
                    select(WikiTopicRow).where(
                        WikiTopicRow.build_id == build.build_id,
                        WikiTopicRow.topic_id.in_(unique_ids),
                    )
                ).scalars()
            }
            evidence_rows = list(
                session.execute(
                    select(WikiTopicEvidenceRow).where(
                        WikiTopicEvidenceRow.build_id == build.build_id,
                        WikiTopicEvidenceRow.topic_id.in_(unique_ids),
                    )
                ).scalars()
            )
        missing_topics = [topic_id for topic_id in unique_ids if topic_id not in topics]
        if missing_topics:
            raise WikiCompilationError(
                "wiki_topic_not_found",
                f"topics do not exist in the active build: {', '.join(missing_topics)}",
            )
        evidence_by_topic: dict[str, list[str]] = {}
        for row in evidence_rows:
            evidence_by_topic.setdefault(row.topic_id, []).append(row.block_id)
        reader = self._bind_snapshot(snapshot_id)
        groups: list[WikiTopicEvidenceGroup] = []
        for topic_id in unique_ids:
            block_ids = sorted(evidence_by_topic.get(topic_id, []))
            try:
                block_result = reader.get_blocks(block_ids)
            except RetrievalFailure as error:
                raise WikiCompilationError(
                    "wiki_evidence_unavailable",
                    error.message,
                ) from error
            groups.append(
                WikiTopicEvidenceGroup(
                    topic_id=topic_id,
                    title=topics[topic_id].title,
                    blocks=block_result.blocks,
                )
            )
        return WikiTopicEvidenceResult(
            snapshot_id=snapshot_id,
            build_id=build.build_id,
            topics=groups,
        )

    def get_topic(self, snapshot_id: str, topic_id: str) -> WikiTopicDetail:
        """读取单个 topic 元数据，不读取页面正文。"""

        build = self.active_for_snapshot(snapshot_id)
        with self._database.session() as session:
            topic = session.get(WikiTopicRow, (build.build_id, topic_id))
            if topic is None:
                raise WikiCompilationError(
                    "wiki_topic_not_found",
                    f"topic does not exist in the active build: {topic_id}",
                )
            evidence = list(
                session.execute(
                    select(WikiTopicEvidenceRow).where(
                        WikiTopicEvidenceRow.build_id == build.build_id,
                        WikiTopicEvidenceRow.topic_id == topic_id,
                    )
                ).scalars()
            )
            slots = list(
                session.execute(
                    select(WikiCoverageSlotRow).where(
                        WikiCoverageSlotRow.build_id == build.build_id,
                        WikiCoverageSlotRow.topic_id == topic_id,
                    )
                ).scalars()
            )
            slot_ids = [slot.slot_id for slot in slots]
            slot_evidence = (
                list(
                    session.execute(
                        select(WikiSlotEvidenceRow).where(
                            WikiSlotEvidenceRow.build_id == build.build_id,
                            WikiSlotEvidenceRow.slot_id.in_(slot_ids),
                        )
                    ).scalars()
                )
                if slot_ids
                else []
            )
        slot_evidence_by_id: dict[str, list[str]] = {}
        for row in slot_evidence:
            slot_evidence_by_id.setdefault(row.slot_id, []).append(row.block_id)
        return WikiTopicDetail(
            topic_id=topic.topic_id,
            title=topic.title,
            slug=topic.slug,
            summary=topic.summary,
            semantic_tags=json.loads(topic.semantic_tags_json),
            content_level=cast(WikiContentLevel, topic.content_level),
            source_doc_ids=sorted({row.doc_id for row in evidence}),
            evidence_block_ids=sorted(row.block_id for row in evidence),
            coverage_slots=[
                WikiCoverageSlotView(
                    slot_id=slot.slot_id,
                    label=slot.label,
                    question=slot.question,
                    source_block_ids=sorted(slot_evidence_by_id.get(slot.slot_id, [])),
                )
                for slot in slots
            ],
        )

    def _insert_topic_rows(
        self,
        session: Session,
        *,
        build_id: str,
        topics_with_levels: list[tuple[WikiTopicLedgerEntry, str]],
        blocks: dict[str, RetrievalBlock],
    ) -> None:
        for topic, level in topics_with_levels:
            session.add(
                WikiTopicRow(
                    build_id=build_id,
                    topic_id=topic.topic_id,
                    canonical_name=topic.canonical_name,
                    normalized_name=topic.normalized_name,
                    slug=topic.slug,
                    title=topic.title,
                    summary=topic.summary,
                    semantic_tags_json=json.dumps(topic.semantic_tags, ensure_ascii=False),
                    content_level=level,
                )
            )
            aliases = [topic.title, *topic.aliases]
            seen_aliases: set[str] = set()
            for alias in aliases:
                normalized = normalize_topic_name(alias)
                if not normalized or normalized in seen_aliases:
                    continue
                seen_aliases.add(normalized)
                session.add(
                    WikiTopicAliasRow(
                        build_id=build_id,
                        normalized_alias=normalized,
                        topic_id=topic.topic_id,
                        alias=alias,
                    )
                )
            for block_id in topic.evidence_block_ids:
                block = blocks[block_id]
                session.add(
                    WikiTopicEvidenceRow(
                        build_id=build_id,
                        topic_id=topic.topic_id,
                        block_id=block_id,
                        doc_id=block.doc_id,
                        doc_version=int(block_id.split("#v", 1)[1].split("#", 1)[0]),
                        content_hash=_block_content_hash(block.text),
                        relation="direct",
                    )
                )
            for slot in topic.coverage_slots:
                session.add(
                    WikiCoverageSlotRow(
                        build_id=build_id,
                        slot_id=slot.slot_id,
                        topic_id=topic.topic_id,
                        label=slot.label,
                        question=slot.question,
                        status="observed",
                    )
                )
                for block_id in slot.source_block_ids:
                    session.add(
                        WikiSlotEvidenceRow(
                            build_id=build_id,
                            slot_id=slot.slot_id,
                            block_id=block_id,
                        )
                    )

    @staticmethod
    def _insert_vectors(
        session: Session,
        *,
        build_id: str,
        vectors: dict[str, tuple[list[float], float]],
        provider: EmbeddingProvider,
    ) -> None:
        for topic_id, (vector, norm) in vectors.items():
            session.add(
                WikiTopicVectorRow(
                    build_id=build_id,
                    topic_id=topic_id,
                    provider=provider.profile.provider,
                    model_id=provider.profile.model_id,
                    dimension=provider.profile.dimension,
                    vector=struct.pack(f"<{provider.profile.dimension}f", *vector),
                    norm=norm,
                )
            )

    @staticmethod
    def _insert_fts(
        session: Session,
        *,
        build_id: str,
        topics_with_levels: list[tuple[WikiTopicLedgerEntry, str]],
    ) -> None:
        for topic, level in topics_with_levels:
            body = topic.summary if level == "catalog" else topic.summary
            session.execute(
                text(
                    "INSERT INTO wiki_topic_fts "
                    "(build_id, topic_id, title, aliases, summary, body) "
                    "VALUES (:build_id, :topic_id, :title, :aliases, :summary, :body)"
                ),
                {
                    "build_id": build_id,
                    "topic_id": topic.topic_id,
                    "title": topic.title,
                    "aliases": " ".join(topic.aliases),
                    "summary": topic.summary,
                    "body": body,
                },
            )

    @staticmethod
    def _publish_pointer(session: Session, *, build_id: str, snapshot_id: str) -> None:
        pointer = session.get(ActiveWikiPointerRow, 1)
        if pointer is None:
            session.add(
                ActiveWikiPointerRow(
                    pointer_id=1,
                    build_id=build_id,
                    snapshot_id=snapshot_id,
                    generation=1,
                    published_at=datetime.now(UTC),
                )
            )
            return
        pointer.previous_build_id = pointer.build_id
        pointer.build_id = build_id
        pointer.snapshot_id = snapshot_id
        pointer.generation += 1
        pointer.published_at = datetime.now(UTC)

    @staticmethod
    def _fts_search(session: Session, build_id: str, query: str) -> set[str]:
        escaped = query.replace('"', '""')
        try:
            rows = session.execute(
                text(
                    "SELECT topic_id FROM wiki_topic_fts "
                    "WHERE build_id = :build_id AND wiki_topic_fts MATCH :query"
                ),
                {"build_id": build_id, "query": f'"{escaped}"'},
            ).all()
        except SQLAlchemyError:
            return set()
        return {str(row[0]) for row in rows}

    def _load_evidence_blocks(
        self,
        reader: RetrievalSnapshotReader,
        ledger: WikiTopicLedgerResult,
    ) -> dict[str, RetrievalBlock]:
        block_ids = sorted(
            {block_id for topic in ledger.topics for block_id in topic.evidence_block_ids}
        )
        try:
            blocks = reader.get_blocks(block_ids).blocks
        except RetrievalFailure as error:
            raise WikiCompilationError(
                "wiki_evidence_out_of_snapshot",
                error.message,
            ) from error
        return {block.block_id: block for block in blocks}

    def _bind_snapshot(self, snapshot_id: str) -> RetrievalSnapshotReader:
        try:
            return self._retrieval.bind_snapshot(snapshot_id)
        except RetrievalFailure as error:
            raise WikiCompilationError(
                "wiki_snapshot_unavailable",
                error.message,
                retryable=error.retryable,
            ) from error


def _source_diverse_order(
    items: list[WikiTopicSearchItem],
    *,
    source_doc_ids_by_topic: dict[str, list[str]],
) -> list[WikiTopicSearchItem]:
    """保留全部主题，并按来源分组轮转，避免单一来源垄断目录前部。"""

    def topic_key(item: WikiTopicSearchItem) -> tuple[object, ...]:
        return (
            "dense" not in item.match_sources,
            -(item.dense_score or 0.0),
            item.title,
            item.topic_id,
        )

    grouped: dict[str, list[WikiTopicSearchItem]] = {}
    for item in items:
        doc_ids = source_doc_ids_by_topic.get(item.topic_id, [])
        group_id = doc_ids[0] if doc_ids else ""
        grouped.setdefault(group_id, []).append(item)
    for group in grouped.values():
        group.sort(key=topic_key)
    ordered_groups = sorted(
        grouped.values(),
        key=lambda group: (topic_key(group[0]), group[0].topic_id),
    )
    result: list[WikiTopicSearchItem] = []
    offset = 0
    while len(result) < len(items):
        added = False
        for group in ordered_groups:
            if offset < len(group):
                result.append(group[offset])
                added = True
        if not added:
            break
        offset += 1
    return result


async def _embed_topics(
    topics_with_levels: list[tuple[WikiTopicLedgerEntry, str]],
    *,
    permission_provider: EmbeddingProvider,
) -> tuple[dict[str, tuple[list[float], float]], int]:
    if not topics_with_levels:
        return {}, 0
    texts = [_topic_search_text(topic) for topic, _ in topics_with_levels]
    vectors: list[list[float]] = []
    request_count = 0
    for offset in range(0, len(texts), permission_provider.batch_size):
        batch = texts[offset : offset + permission_provider.batch_size]
        try:
            result = await permission_provider.embed_documents(batch)
        except EmbeddingProviderError as error:
            raise WikiCompilationError(
                "wiki_build_embedding_failed",
                error.message,
                retryable=error.retryable,
            ) from error
        vectors.extend(result.vectors)
        request_count += 1
    if len(vectors) != len(topics_with_levels):
        raise WikiCompilationError(
            "wiki_build_embedding_invalid",
            "embedding provider returned the wrong vector count",
        )
    result_map: dict[str, tuple[list[float], float]] = {}
    for (topic, _), vector in zip(topics_with_levels, vectors, strict=True):
        if (
            len(vector) != permission_provider.profile.dimension
            or not all(math.isfinite(value) for value in vector)
            or not any(value != 0 for value in vector)
        ):
            raise WikiCompilationError(
                "wiki_build_embedding_invalid",
                f"invalid dense vector for topic {topic.topic_id}",
            )
        norm = math.sqrt(sum(value * value for value in vector))
        result_map[topic.topic_id] = (vector, norm)
    return result_map, request_count


def _topic_search_text(topic: WikiTopicLedgerEntry) -> str:
    return "\n".join([topic.title, *topic.aliases, topic.summary])


def _content_level(topic: WikiTopicLedgerEntry) -> Literal["catalog", "narrative"]:
    if len(topic.evidence_block_ids) >= 2 or len(topic.source_doc_ids) >= 2:
        return "narrative"
    return "catalog"


def _block_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _build_manifest_hash(
    *,
    snapshot_id: str,
    snapshot_manifest_hash: str,
    base_build_id: str | None,
    ledger: WikiTopicLedgerResult,
    counts: WikiBuildCounts,
    pipeline_versions: dict[str, object],
) -> str:
    payload = {
        "snapshot_id": snapshot_id,
        "snapshot_manifest_hash": snapshot_manifest_hash,
        "base_build_id": base_build_id,
        "ledger_id": ledger.manifest.ledger_id,
        "ledger_hash": ledger.manifest.manifest_hash,
        "counts": counts.model_dump(mode="json"),
        "pipeline_versions": pipeline_versions,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _row_to_build_result(row: WikiBuildRow) -> WikiBuildResult:
    counts = WikiBuildCounts.model_validate_json(row.counts_json)
    pipeline_versions = json.loads(row.pipeline_versions_json)
    return WikiBuildResult(
        build_id=row.build_id,
        snapshot_id=row.snapshot_id,
        snapshot_manifest_hash=row.snapshot_manifest_hash,
        base_build_id=row.base_build_id,
        status="ready",
        manifest_hash=row.manifest_hash,
        ledger_id=str(pipeline_versions["ledger_id"]),
        topic_map_ids=[str(value) for value in pipeline_versions["topic_map_version"]],
        counts=counts,
        created_at=utc_iso(row.created_at),
        published_at=utc_iso(row.published_at or row.created_at),
    )


def _build_result(
    *,
    build_id: str,
    reader: RetrievalSnapshotReader,
    base_build_id: str | None,
    manifest_hash: str,
    counts: WikiBuildCounts,
    created_at: datetime,
    ledger_id: str,
    topic_map_ids: list[str],
) -> WikiBuildResult:
    return WikiBuildResult(
        build_id=build_id,
        snapshot_id=reader.snapshot_id,
        snapshot_manifest_hash=reader.manifest.manifest_hash,
        base_build_id=base_build_id,
        status="ready",
        manifest_hash=manifest_hash,
        ledger_id=ledger_id,
        topic_map_ids=topic_map_ids,
        counts=counts,
        created_at=created_at.isoformat(),
        published_at=created_at.isoformat(),
    )
