from __future__ import annotations

from pathlib import Path
from typing import Literal

from sqlalchemy import select

from app.providers.chat import ChatProvider
from app.providers.embeddings import EmbeddingProvider
from app.retrieval.errors import RetrievalFailure
from app.retrieval.service import RetrievalReadService, RetrievalSnapshotReader
from app.storage.database import StorageDatabase
from app.storage.wiki_models import WikiBuildDocumentMapRow, WikiTopicRow
from app.wiki.build_models import WikiCompilationResult
from app.wiki.build_service import WikiBuildService
from app.wiki.errors import WikiCompilationError
from app.wiki.narrative_service import WikiNarrativeService
from app.wiki.topic_identity import TopicIdentityCompiler
from app.wiki.topic_map import TopicMapCompiler
from app.wiki.topic_models import WikiTopicLedgerEntry


class WikiCompilationService:
    """串联 topic map、身份账本、topic build 与 narrative pages。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._database = StorageDatabase(self._data_root)
        self._retrieval = RetrievalReadService(self._data_root)
        self._maps = TopicMapCompiler(self._data_root)
        self._identities = TopicIdentityCompiler(self._data_root)
        self._builds = WikiBuildService(self._data_root)
        self._narratives = WikiNarrativeService(self._data_root)

    async def compile_full(
        self,
        snapshot_id: str,
        *,
        chat_provider: ChatProvider,
        embedding_provider: EmbeddingProvider,
        granularity: Literal["focused", "standard", "exhaustive"] = "standard",
        materialize_pages: bool = True,
        map_parallel: int = 1,
    ) -> WikiCompilationResult:
        """对一个 snapshot 执行全量编译，不依赖旧 Wiki build。"""

        topic_map = await self._maps.compile(
            snapshot_id,
            provider=chat_provider,
            granularity=granularity,
            map_parallel=map_parallel,
        )
        ledger = await self._identities.compile(
            snapshot_id,
            topic_map_ids=[topic_map.manifest.map_id],
            provider=chat_provider,
        )
        build = await self._builds.publish(
            snapshot_id,
            ledger_id=ledger.manifest.ledger_id,
            embedding_provider=embedding_provider,
        )
        pages = (
            await self._narratives.materialize(
                snapshot_id,
                provider=chat_provider,
            )
            if materialize_pages
            else None
        )
        return WikiCompilationResult(
            snapshot_id=snapshot_id,
            build=build,
            ledger_id=ledger.manifest.ledger_id,
            topic_map_ids=[topic_map.manifest.map_id],
            changed_document_ids=[document.doc_id for document in topic_map.documents],
            removed_document_ids=[],
            affected_topic_ids=sorted(topic.topic_id for topic in ledger.topics),
            materialized_pages=len(pages.pages) if pages is not None else 0,
            copied_pages=0,
        )

    async def compile_incremental(
        self,
        snapshot_id: str,
        *,
        chat_provider: ChatProvider,
        embedding_provider: EmbeddingProvider,
        base_build_id: str,
        granularity: Literal["focused", "standard", "exhaustive"] = "standard",
        materialize_pages: bool = True,
        map_parallel: int = 1,
    ) -> WikiCompilationResult:
        """基于上一 build 只重算变更文档及受影响主题。"""

        base_build = self._builds.build_by_id(base_build_id)
        reader = self._bind_snapshot(snapshot_id)
        current_versions = {
            document.doc_id: document.doc_version for document in reader.manifest.documents
        }
        with self._database.session() as session:
            base_map_rows = list(
                session.execute(
                    select(WikiBuildDocumentMapRow).where(
                        WikiBuildDocumentMapRow.build_id == base_build_id
                    )
                ).scalars()
            )
        base_versions = {row.doc_id: row.doc_version for row in base_map_rows}
        changed_doc_ids = sorted(
            doc_id
            for doc_id, version in current_versions.items()
            if base_versions.get(doc_id) != version
        )
        removed_doc_ids = sorted(set(base_versions) - set(current_versions))
        unchanged_doc_ids = sorted(set(current_versions) - set(changed_doc_ids))
        maps_by_id: dict[str, list[str]] = {}
        for row in base_map_rows:
            if row.doc_id in unchanged_doc_ids:
                maps_by_id.setdefault(row.topic_map_id, []).append(row.doc_id)
        topic_map_ids: list[str] = []
        for source_map_id, document_ids in maps_by_id.items():
            adopted = self._maps.adopt_documents(
                snapshot_id,
                source_snapshot_id=base_build.snapshot_id,
                source_map_id=source_map_id,
                document_ids=document_ids,
            )
            if adopted is not None:
                topic_map_ids.append(adopted.manifest.map_id)
        if changed_doc_ids:
            changed_map = await self._maps.compile(
                snapshot_id,
                provider=chat_provider,
                granularity=granularity,
                map_parallel=map_parallel,
                document_ids=changed_doc_ids,
            )
            topic_map_ids.append(changed_map.manifest.map_id)
        if not topic_map_ids:
            raise WikiCompilationError(
                "wiki_no_documents",
                "incremental Wiki build has no documents",
            )
        ledger = await self._identities.compile(
            snapshot_id,
            topic_map_ids=topic_map_ids,
            provider=chat_provider,
            base_ledger_id=base_build.ledger_id,
        )
        base_ledger = self._identities.load(base_build.ledger_id)
        affected_topic_ids = _affected_topic_ids(
            previous=base_ledger.topics,
            current=ledger.topics,
            changed_doc_ids=set(changed_doc_ids) | set(removed_doc_ids),
        )
        build = await self._builds.publish(
            snapshot_id,
            ledger_id=ledger.manifest.ledger_id,
            embedding_provider=embedding_provider,
            base_build_id=base_build_id,
        )
        if not materialize_pages:
            return WikiCompilationResult(
                snapshot_id=snapshot_id,
                build=build,
                ledger_id=ledger.manifest.ledger_id,
                topic_map_ids=topic_map_ids,
                changed_document_ids=changed_doc_ids,
                removed_document_ids=removed_doc_ids,
                affected_topic_ids=sorted(affected_topic_ids),
                materialized_pages=0,
                copied_pages=0,
            )
        current_levels = self._topic_levels(build.build_id)
        current_narrative_ids = {
            topic_id for topic_id, level in current_levels.items() if level == "narrative"
        }
        stable_narrative_ids = sorted(current_narrative_ids - affected_topic_ids)
        existing_base_pages = self._narratives.page_topic_ids(base_build_id)
        copied_topic_ids = sorted(set(stable_narrative_ids) & existing_base_pages)
        self._narratives.copy_pages_from_base(
            base_build_id=base_build_id,
            target_build_id=build.build_id,
            topic_ids=copied_topic_ids,
        )
        generated_topic_ids = sorted(current_narrative_ids - set(copied_topic_ids))
        materialized = await self._narratives.materialize(
            snapshot_id,
            provider=chat_provider,
            topic_ids=generated_topic_ids,
        )
        return WikiCompilationResult(
            snapshot_id=snapshot_id,
            build=build,
            ledger_id=ledger.manifest.ledger_id,
            topic_map_ids=topic_map_ids,
            changed_document_ids=changed_doc_ids,
            removed_document_ids=removed_doc_ids,
            affected_topic_ids=sorted(affected_topic_ids),
            materialized_pages=len(materialized.pages),
            copied_pages=len(copied_topic_ids),
        )

    def _topic_levels(self, build_id: str) -> dict[str, str]:
        with self._database.session() as session:
            return {
                row.topic_id: row.content_level
                for row in session.execute(
                    select(WikiTopicRow).where(WikiTopicRow.build_id == build_id)
                ).scalars()
            }

    def _bind_snapshot(self, snapshot_id: str) -> RetrievalSnapshotReader:
        try:
            return self._retrieval.bind_snapshot(snapshot_id)
        except RetrievalFailure as error:
            raise WikiCompilationError(
                "wiki_snapshot_unavailable",
                error.message,
                retryable=error.retryable,
            ) from error


def _affected_topic_ids(
    *,
    previous: list[WikiTopicLedgerEntry],
    current: list[WikiTopicLedgerEntry],
    changed_doc_ids: set[str],
) -> set[str]:
    previous_by_id = {topic.topic_id: topic for topic in previous}
    current_by_id = {topic.topic_id: topic for topic in current}
    seeds: set[str] = set()
    for topic_id in set(previous_by_id) | set(current_by_id):
        old = previous_by_id.get(topic_id)
        new = current_by_id.get(topic_id)
        if (
            old is None
            or new is None
            or _topic_fingerprint(old) != _topic_fingerprint(new)
            or (old.source_doc_ids and changed_doc_ids.intersection(old.source_doc_ids))
        ):
            seeds.add(topic_id)
    evidence_to_topics: dict[str, set[str]] = {}
    for topic in [*previous, *current]:
        for block_id in topic.evidence_block_ids:
            evidence_to_topics.setdefault(block_id, set()).add(topic.topic_id)
    affected = set(seeds)
    for topic_id in seeds:
        topic = current_by_id.get(topic_id) or previous_by_id.get(topic_id)
        if topic is None:
            continue
        for block_id in topic.evidence_block_ids:
            affected.update(evidence_to_topics.get(block_id, set()))
    return affected


def _topic_fingerprint(topic: WikiTopicLedgerEntry) -> tuple[object, ...]:
    return (
        topic.title,
        topic.canonical_name,
        tuple(topic.aliases),
        topic.summary,
        tuple(topic.semantic_tags),
        tuple(topic.source_doc_ids),
        tuple(topic.evidence_block_ids),
        tuple(
            (
                slot.label,
                slot.question,
                tuple(slot.source_block_ids),
            )
            for slot in topic.coverage_slots
        ),
    )
