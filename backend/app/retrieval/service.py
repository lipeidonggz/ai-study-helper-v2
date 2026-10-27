from __future__ import annotations

import asyncio
import hashlib
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.collections.models import (
    CandidateSnapshotManifest,
    SnapshotDocument,
)
from app.collections.service import CollectionSnapshotService
from app.ingestion.errors import IngestFailure
from app.ingestion.models import (
    DocumentLanguage,
    IngestDocumentResponse,
    SectionRecord,
    SectionStats,
)
from app.providers.embeddings import (
    EmbeddingProvider,
    EmbeddingProviderError,
    EmbeddingQueryResult,
)
from app.retrieval.errors import RetrievalFailure
from app.retrieval.hybrid import (
    DEFAULT_HYBRID_RETRIEVAL_PROFILE,
    FusedCandidate,
    HybridRetrievalProfile,
    search_snapshot_index,
)
from app.retrieval.models import (
    GetBlocksResult,
    GetOutlineResult,
    ListBlocksResult,
    ListDocumentsResult,
    RetrievalBlock,
    RetrievalDocumentOutline,
    RetrievalDocumentSummary,
    RetrievalErrorPayload,
    RetrievalOutlineItem,
    RetrievalOutlineSection,
    RetrievalSectionContext,
    ScanMatch,
    ScanTermResult,
    SearchBlocksResult,
)

SCAN_SNIPPET_CONTEXT_CHARS = 80
_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ASCII_FOLD_TABLE = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)
_SENTENCE_TERMINATORS = frozenset("。！？!?")
_SENTENCE_CLOSERS = frozenset("\"'”’」』）)]}")


class RetrievalReadService:
    """为一次读取操作绑定当前或显式指定的 collection 快照。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._collections = CollectionSnapshotService(self._data_root)
        self._collections_root = self._data_root / "collections"

    def bind_current(self) -> RetrievalSnapshotReader:
        """解析一次活动指针并返回固定该快照的 reader。

        后续调用不能重新读取活动指针；新的入库发布只影响下一次
        ``bind_current`` 创建的新 reader。
        """

        try:
            current = self._collections.current()
        except IngestFailure as error:
            raise RetrievalFailure(
                error.code,
                error.message,
                retryable=error.retryable,
            ) from error
        if current is None:
            raise RetrievalFailure(
                "collection_unavailable",
                "no active collection snapshot is available",
            )
        if current.manifest.status not in {"ready", "ready_partial"}:
            raise RetrievalFailure(
                "collection_unavailable",
                f"active collection status is not readable: {current.manifest.status}",
            )
        return RetrievalSnapshotReader(
            manifest=current.manifest,
            snapshot_root=self._collections_root / current.manifest.snapshot_id,
        )

    def bind_snapshot(self, snapshot_id: str) -> RetrievalSnapshotReader:
        """按 ID 返回只读 reader，不跟随之后的活动 collection 指针变化。"""

        try:
            manifest = self._collections.snapshot(snapshot_id)
        except IngestFailure as error:
            raise RetrievalFailure(
                error.code,
                error.message,
                retryable=error.retryable,
            ) from error
        if manifest.status not in {"ready", "ready_partial"}:
            raise RetrievalFailure(
                "collection_unavailable",
                f"collection status is not readable: {manifest.status}",
            )
        return RetrievalSnapshotReader(
            manifest=manifest,
            snapshot_root=self._collections_root / manifest.snapshot_id,
        )


@dataclass(frozen=True, slots=True)
class HybridSearchExecution:
    """一次混合检索的 Agent payload 与内部诊断快照。"""

    result: SearchBlocksResult
    profile: HybridRetrievalProfile
    query_request_id: str
    query_input_tokens: int
    dense_block_ids: tuple[str, ...]
    keyword_block_ids: tuple[str, ...]
    fused_candidates: tuple[FusedCandidate, ...]


class RetrievalSnapshotReader:
    """只读一个不可变 collection 快照的确定性检索读取器。"""

    def __init__(
        self,
        *,
        manifest: CandidateSnapshotManifest,
        snapshot_root: Path,
    ) -> None:
        self._manifest = manifest
        self._snapshot_root = snapshot_root.resolve()
        self._documents_by_id = {document.doc_id: document for document in manifest.documents}
        if len(self._documents_by_id) != len(manifest.documents):
            raise RetrievalFailure(
                "snapshot_integrity_error",
                "snapshot manifest contains duplicate document IDs",
            )
        self._document_cache: dict[str, IngestDocumentResponse] = {}

    @property
    def snapshot_id(self) -> str:
        """返回 reader 生命周期内固定的快照 ID。"""

        return self._manifest.snapshot_id

    @property
    def completeness(self) -> str:
        """返回固定快照的完整性。"""

        return self._manifest.completeness

    @property
    def manifest(self) -> CandidateSnapshotManifest:
        """返回 reader 固定的快照 manifest。"""

        return self._manifest

    def list_documents(
        self,
        *,
        source: str | None = None,
        lang: DocumentLanguage | None = None,
        title_contains: str | None = None,
    ) -> ListDocumentsResult:
        """按来源、语言和标题片段列出固定快照中的文档。"""

        source_filter = _optional_non_empty(source, "source")
        title_filter = _optional_non_empty(title_contains, "title_contains")
        documents: list[RetrievalDocumentSummary] = []
        for summary in self._manifest.documents:
            persisted = self._load_document(summary.doc_id)
            metadata = persisted.document
            if source_filter is not None:
                candidates = [metadata.source_label, *metadata.aliases]
                if not any(value.casefold() == source_filter.casefold() for value in candidates):
                    continue
            if lang is not None and metadata.language != lang:
                continue
            if (
                title_filter is not None
                and title_filter.casefold() not in metadata.title.casefold()
            ):
                continue
            documents.append(
                RetrievalDocumentSummary(
                    doc_id=summary.doc_id,
                    title=metadata.title,
                    source_label=metadata.source_label,
                    aliases=metadata.aliases,
                    lang=metadata.language,
                    sections=len(persisted.section_tree.sections),
                    blocks=len(persisted.block_set.blocks),
                )
            )
        documents.sort(
            key=lambda document: (
                document.source_label.casefold(),
                document.title.casefold(),
                document.doc_id,
            )
        )
        return ListDocumentsResult(documents=documents, total=len(documents))

    def get_outline(self, doc_ids: list[str]) -> GetOutlineResult:
        """返回输入文档的章节大纲，并按输入顺序保留首次出现。"""

        unique_ids = _deduplicate_non_empty(doc_ids, "doc_ids")
        items: list[RetrievalOutlineItem] = []
        for doc_id in unique_ids:
            try:
                persisted = self._load_document(doc_id)
                items.append(
                    RetrievalOutlineItem(
                        doc_id=doc_id,
                        document=self._document_outline(persisted),
                    )
                )
            except RetrievalFailure as error:
                items.append(
                    RetrievalOutlineItem(
                        doc_id=doc_id,
                        error=RetrievalErrorPayload(
                            code=error.code,
                            message=error.message,
                            retryable=error.retryable,
                        ),
                    )
                )
        return GetOutlineResult(documents=items)

    def list_blocks(
        self,
        doc_id: str,
        section_paths: list[list[str]] | None = None,
        *,
        include_descendants: bool | None = None,
    ) -> ListBlocksResult:
        """按整篇、章节直属或章节子树范围返回单篇文档的完整块集合。"""

        normalized_doc_id = _required_non_empty(doc_id, "doc_id")
        persisted = self._load_document(normalized_doc_id)
        existing_paths = {tuple(section.path) for section in persisted.section_tree.sections}
        selected_paths = [
            tuple(path) for path in (section_paths if section_paths is not None else [])
        ]
        if selected_paths and include_descendants is None:
            raise RetrievalFailure(
                "invalid_argument",
                "include_descendants is required when section_paths is not empty",
            )
        if not selected_paths and include_descendants is not None:
            raise RetrievalFailure(
                "invalid_argument",
                "include_descendants is only valid with non-empty section_paths",
            )
        missing_paths = [path for path in selected_paths if path not in existing_paths]
        if missing_paths:
            raise RetrievalFailure(
                "scope_not_found",
                f"section path does not exist in document {normalized_doc_id}",
            )

        selected_blocks = [
            block
            for block in persisted.block_set.blocks
            if not selected_paths
            or any(
                (
                    tuple(block.section_path[: len(path)]) == path
                    if include_descendants
                    else tuple(block.section_path) == path
                )
                for path in selected_paths
            )
        ]
        selected_blocks.sort(key=lambda block: block.block_index)
        section_context_by_path = _section_context_map(persisted)
        blocks = [
            RetrievalBlock(
                block_id=block.block_id,
                doc_id=normalized_doc_id,
                doc_title=persisted.document.title,
                source_label=persisted.document.source_label,
                section_path=block.section_path,
                section_context=_required_section_context(
                    section_context_by_path,
                    normalized_doc_id,
                    block.section_path,
                ),
                text=block.text,
            )
            for block in selected_blocks
        ]
        scope = (
            "document" if not selected_paths else ("subtree" if include_descendants else "section")
        )
        return ListBlocksResult(blocks=blocks, total=len(blocks), scope=scope)

    def get_blocks(self, block_ids: list[str]) -> GetBlocksResult:
        """按请求顺序返回 block_id 对应的完整块；任一 ID 缺失时整次失败。"""

        unique_ids = _deduplicate_non_empty(block_ids, "block_ids")
        blocks_by_document: dict[str, dict[str, RetrievalBlock]] = {}
        missing_ids: list[str] = []
        for block_id in unique_ids:
            doc_id, separator, _ = block_id.partition("#")
            if not separator or doc_id not in self._documents_by_id:
                missing_ids.append(block_id)
                continue
            document_blocks = blocks_by_document.get(doc_id)
            if document_blocks is None:
                persisted = self._load_document(doc_id)
                section_context_by_path = _section_context_map(persisted)
                document_blocks = {
                    block.block_id: RetrievalBlock(
                        block_id=block.block_id,
                        doc_id=doc_id,
                        doc_title=persisted.document.title,
                        source_label=persisted.document.source_label,
                        section_path=block.section_path,
                        section_context=_required_section_context(
                            section_context_by_path,
                            doc_id,
                            block.section_path,
                        ),
                        text=block.text,
                    )
                    for block in persisted.block_set.blocks
                }
                blocks_by_document[doc_id] = document_blocks
            if block_id not in document_blocks:
                missing_ids.append(block_id)
        if missing_ids:
            raise RetrievalFailure(
                "scope_not_found",
                f"blocks do not exist in the fixed snapshot: {', '.join(missing_ids)}",
            )
        blocks = [
            blocks_by_document[block_id.partition("#")[0]][block_id] for block_id in unique_ids
        ]
        return GetBlocksResult(blocks=blocks, total=len(blocks))

    def scan_term(
        self,
        term: str,
        doc_ids: list[str] | None = None,
    ) -> ScanTermResult:
        """在原文上执行 NFC＋英文大小写折叠的确定性子串扫描。"""

        normalized_term = _required_non_empty(term, "term")
        requested_ids = (
            [document.doc_id for document in self._manifest.documents]
            if doc_ids is None
            else _deduplicate_non_empty(doc_ids, "doc_ids")
        )
        missing_ids = [doc_id for doc_id in requested_ids if doc_id not in self._documents_by_id]
        if missing_ids:
            raise RetrievalFailure(
                "scope_not_found",
                f"documents do not exist in the fixed snapshot: {', '.join(missing_ids)}",
            )

        folded_term = _fold_for_scan(normalized_term)
        matches_with_index: list[tuple[int, ScanMatch]] = []
        for doc_id in requested_ids:
            persisted = self._load_document(doc_id)
            section_context_by_path = _section_context_map(persisted)
            for block in persisted.block_set.blocks:
                normalized_text = unicodedata.normalize("NFC", block.text)
                folded_text = normalized_text.translate(_ASCII_FOLD_TABLE)
                start = folded_text.find(folded_term)
                if start < 0:
                    continue
                matches_with_index.append(
                    (
                        block.block_index,
                        ScanMatch(
                            block_id=block.block_id,
                            doc_id=doc_id,
                            doc_title=persisted.document.title,
                            source_label=persisted.document.source_label,
                            section_path=block.section_path,
                            section_context=_required_section_context(
                                section_context_by_path,
                                doc_id,
                                block.section_path,
                            ),
                            snippet=_snippet(
                                normalized_text,
                                start,
                                start + len(folded_term),
                            ),
                        ),
                    )
                )
        matches_with_index.sort(key=lambda item: (item[1].doc_id, item[0]))
        matches = [match for _, match in matches_with_index]
        return ScanTermResult(term=normalized_term, matches=matches, count=len(matches))

    async def search_blocks(
        self,
        query: str,
        *,
        provider: EmbeddingProvider,
        doc_ids: list[str] | None = None,
    ) -> SearchBlocksResult:
        """执行固定快照上的 dense＋FTS 混合检索并返回 Top-5 块全文。"""

        execution = await self.execute_search_blocks(
            query,
            provider=provider,
            doc_ids=doc_ids,
        )
        return execution.result

    async def execute_search_blocks(
        self,
        query: str,
        *,
        provider: EmbeddingProvider,
        doc_ids: list[str] | None = None,
        profile: HybridRetrievalProfile = DEFAULT_HYBRID_RETRIEVAL_PROFILE,
    ) -> HybridSearchExecution:
        """执行混合检索，并返回仅供后续 trace 使用的内部排名信息。"""

        normalized_query = _required_non_empty(query, "query")
        versions = self._manifest.versions
        if versions is None:
            raise RetrievalFailure(
                "collection_unavailable",
                "active collection has no embedding profile",
            )
        expected_profile = versions.embedding_profile
        if (
            provider.profile.provider != expected_profile.provider
            or provider.profile.model_id != expected_profile.model_id
            or provider.profile.dimension != expected_profile.dimension
        ):
            raise RetrievalFailure(
                "collection_unavailable",
                "query embedding provider does not match the active collection",
            )

        selected_ids = self._search_scope(doc_ids)
        if not self._manifest.documents:
            empty = SearchBlocksResult(blocks=[], empty_reason="collection_empty")
            return HybridSearchExecution(
                result=empty,
                profile=profile,
                query_request_id="",
                query_input_tokens=0,
                dense_block_ids=(),
                keyword_block_ids=(),
                fused_candidates=(),
            )
        if not any(self._documents_by_id[doc_id].block_count > 0 for doc_id in selected_ids):
            empty = SearchBlocksResult(blocks=[], empty_reason="scope_empty")
            return HybridSearchExecution(
                result=empty,
                profile=profile,
                query_request_id="",
                query_input_tokens=0,
                dense_block_ids=(),
                keyword_block_ids=(),
                fused_candidates=(),
            )

        query_result = await _embed_query_with_retry(provider, normalized_query)
        _validate_query_vector(query_result.vector, expected_profile.dimension)
        expected_index_ref = f"collections/{self.snapshot_id}/index/lance"
        if self._manifest.index_ref != expected_index_ref:
            raise RetrievalFailure(
                "snapshot_integrity_error",
                "active collection index reference does not match snapshot",
            )
        candidates = search_snapshot_index(
            self._snapshot_root,
            query_vector=query_result.vector,
            query=normalized_query,
            doc_ids=selected_ids,
            profile=profile,
        )
        if not candidates.fused:
            return HybridSearchExecution(
                result=SearchBlocksResult(blocks=[], empty_reason="no_match"),
                profile=profile,
                query_request_id=query_result.request_id,
                query_input_tokens=query_result.input_tokens,
                dense_block_ids=tuple(str(row["block_id"]) for row in candidates.dense_rows),
                keyword_block_ids=tuple(str(row["block_id"]) for row in candidates.keyword_rows),
                fused_candidates=(),
            )

        blocks: list[RetrievalBlock] = []
        for candidate in candidates.fused[: profile.top_k]:
            persisted = self._load_document(candidate.doc_id)
            block = next(
                (
                    item
                    for item in persisted.block_set.blocks
                    if item.block_id == candidate.block_id
                ),
                None,
            )
            if block is None:
                raise RetrievalFailure(
                    "snapshot_integrity_error",
                    f"index candidate is missing from document: {candidate.block_id}",
                )
            section_context_by_path = _section_context_map(persisted)
            blocks.append(
                RetrievalBlock(
                    block_id=block.block_id,
                    doc_id=candidate.doc_id,
                    doc_title=persisted.document.title,
                    source_label=persisted.document.source_label,
                    section_path=block.section_path,
                    section_context=_required_section_context(
                        section_context_by_path,
                        candidate.doc_id,
                        block.section_path,
                    ),
                    text=block.text,
                )
            )
        return HybridSearchExecution(
            result=SearchBlocksResult(blocks=blocks),
            profile=profile,
            query_request_id=query_result.request_id,
            query_input_tokens=query_result.input_tokens,
            dense_block_ids=tuple(str(row["block_id"]) for row in candidates.dense_rows),
            keyword_block_ids=tuple(str(row["block_id"]) for row in candidates.keyword_rows),
            fused_candidates=candidates.fused,
        )

    def _search_scope(self, doc_ids: list[str] | None) -> tuple[str, ...]:
        if doc_ids is None:
            return tuple(document.doc_id for document in self._manifest.documents)
        unique_ids = _deduplicate_non_empty(doc_ids, "doc_ids")
        missing_ids = [doc_id for doc_id in unique_ids if doc_id not in self._documents_by_id]
        if missing_ids:
            raise RetrievalFailure(
                "scope_not_found",
                f"documents do not exist in the fixed snapshot: {', '.join(missing_ids)}",
            )
        return tuple(unique_ids)

    def _load_document(self, doc_id: str) -> IngestDocumentResponse:
        cached = self._document_cache.get(doc_id)
        if cached is not None:
            return cached
        summary = self._documents_by_id.get(doc_id)
        if summary is None:
            raise RetrievalFailure(
                "scope_not_found",
                f"document does not exist in the fixed snapshot: {doc_id}",
            )
        artifact = next(
            (item for item in summary.artifact_refs if item.kind == "document"),
            None,
        )
        if artifact is None:
            raise RetrievalFailure(
                "snapshot_integrity_error",
                f"document artifact is missing from snapshot: {doc_id}",
            )
        artifact_path = (self._snapshot_root / artifact.snapshot_ref).resolve()
        if not artifact_path.is_relative_to(self._snapshot_root):
            raise RetrievalFailure(
                "snapshot_integrity_error",
                f"document artifact path escapes snapshot: {doc_id}",
            )
        try:
            artifact_bytes = artifact_path.read_bytes()
        except OSError as error:
            raise RetrievalFailure(
                "snapshot_integrity_error",
                f"document artifact is unreadable: {doc_id}",
            ) from error
        if hashlib.sha256(artifact_bytes).hexdigest() != artifact.sha256:
            raise RetrievalFailure(
                "snapshot_integrity_error",
                f"document artifact checksum does not match snapshot: {doc_id}",
            )
        try:
            persisted = IngestDocumentResponse.model_validate_json(artifact_bytes)
        except ValidationError as error:
            raise RetrievalFailure(
                "snapshot_integrity_error",
                f"document artifact is unreadable: {doc_id}",
            ) from error
        _validate_document_identity(persisted, summary)
        self._document_cache[doc_id] = persisted
        return persisted

    @staticmethod
    def _document_outline(persisted: IngestDocumentResponse) -> RetrievalDocumentOutline:
        stats_by_path = {
            tuple(stats.section_path): stats for stats in persisted.block_set.section_stats
        }
        sections: list[RetrievalOutlineSection] = []
        for section in persisted.section_tree.sections:
            stats = stats_by_path.get(tuple(section.path))
            if stats is None:
                raise RetrievalFailure(
                    "snapshot_integrity_error",
                    f"section stats are missing for path: {section.path}",
                )
            sections.append(_outline_section(section.path, section, stats))
        return RetrievalDocumentOutline(
            doc_id=persisted.document.doc_id,
            doc_title=persisted.document.title,
            source_label=persisted.document.source_label,
            sections=sections,
        )


def _section_context_map(
    persisted: IngestDocumentResponse,
) -> dict[tuple[str, ...], RetrievalSectionContext]:
    """按稳定 section_path 生成模型需要的局部结构上下文。"""

    parent_paths = {
        tuple(section.parent_path)
        for section in persisted.section_tree.sections
        if tuple(section.parent_path) != tuple(section.path)
    }
    section_count = len(persisted.section_tree.sections)
    return {
        tuple(section.path): RetrievalSectionContext(
            section_depth=section.depth,
            has_child_sections=tuple(section.path) in parent_paths,
            document_section_count=section_count,
        )
        for section in persisted.section_tree.sections
    }


def _required_section_context(
    contexts: dict[tuple[str, ...], RetrievalSectionContext],
    doc_id: str,
    section_path: list[str],
) -> RetrievalSectionContext:
    """解析 block 所属结构上下文；缺失时拒绝返回不完整证据。"""

    context = contexts.get(tuple(section_path))
    if context is None:
        raise RetrievalFailure(
            "snapshot_integrity_error",
            f"block references an unknown section path in document {doc_id}: {section_path}",
        )
    return context


def _outline_section(
    path: list[str],
    section: SectionRecord,
    stats: SectionStats,
) -> RetrievalOutlineSection:
    return RetrievalOutlineSection(
        path=path,
        ordinal=section.ordinal,
        title=section.title,
        depth=section.depth,
        own_blocks=stats.own_blocks,
        own_context_tokens=stats.own_context_tokens,
        subtree_blocks=stats.subtree_blocks,
        subtree_context_tokens=stats.subtree_context_tokens,
        first_line=section.first_line,
    )


def _validate_document_identity(
    persisted: IngestDocumentResponse,
    summary: SnapshotDocument,
) -> None:
    if (
        persisted.document.doc_id != summary.doc_id
        or persisted.version.doc_version != summary.doc_version
        or persisted.section_tree.doc_id != summary.doc_id
        or persisted.section_tree.doc_version != summary.doc_version
        or persisted.block_set.doc_id != summary.doc_id
        or persisted.block_set.doc_version != summary.doc_version
        or len(persisted.section_tree.sections) != summary.section_count
        or len(persisted.block_set.blocks) != summary.block_count
    ):
        raise RetrievalFailure(
            "snapshot_integrity_error",
            f"document artifact identity does not match snapshot manifest: {summary.doc_id}",
        )
    block_ids = [block.block_id for block in persisted.block_set.blocks]
    block_indexes = [block.block_index for block in persisted.block_set.blocks]
    if len(set(block_ids)) != len(block_ids) or len(set(block_indexes)) != len(block_indexes):
        raise RetrievalFailure(
            "snapshot_integrity_error",
            f"document block identities are not unique: {summary.doc_id}",
        )


def _required_non_empty(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RetrievalFailure(
            "invalid_argument",
            f"{field_name} must not be empty",
        )
    return normalized


def _optional_non_empty(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_non_empty(value, field_name)


def _deduplicate_non_empty(values: list[str], field_name: str) -> list[str]:
    if not values:
        raise RetrievalFailure(
            "invalid_argument",
            f"{field_name} must contain at least one value",
        )
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _required_non_empty(value, field_name)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _fold_for_scan(value: str) -> str:
    return unicodedata.normalize("NFC", value).translate(_ASCII_FOLD_TABLE)


async def _embed_query_with_retry(
    provider: EmbeddingProvider,
    query: str,
) -> EmbeddingQueryResult:
    for attempt in range(2):
        try:
            return await provider.embed_query(query)
        except EmbeddingProviderError as error:
            if not error.retryable or attempt == 1:
                raise RetrievalFailure(
                    "upstream_unavailable",
                    error.message,
                    retryable=error.retryable,
                ) from error
            delay = error.retry_after_seconds
            await asyncio.sleep(0.25 if delay is None else min(delay, 5.0))
    raise AssertionError("query embedding retry loop must return or raise")


def _validate_query_vector(vector: list[float], dimension: int) -> None:
    if (
        len(vector) != dimension
        or not all(math.isfinite(value) for value in vector)
        or not any(value != 0 for value in vector)
    ):
        raise RetrievalFailure(
            "upstream_unavailable",
            "query embedding returned an invalid dense vector",
        )


def _snippet(text: str, start: int, end: int) -> str:
    target_start = max(0, start - SCAN_SNIPPET_CONTEXT_CHARS)
    target_end = min(len(text), end + SCAN_SNIPPET_CONTEXT_CHARS)
    boundaries = _sentence_boundaries(text)
    snippet_start = max(
        (boundary for boundary in boundaries if boundary <= target_start),
        default=0,
    )
    snippet_end = min(
        (boundary for boundary in boundaries if boundary >= target_end),
        default=len(text),
    )
    while snippet_start < start and text[snippet_start].isspace():
        snippet_start += 1
    while snippet_end > end and text[snippet_end - 1].isspace():
        snippet_end -= 1
    prefix = "…" if snippet_start > 0 else ""
    suffix = "…" if snippet_end < len(text) else ""
    return f"{prefix}{text[snippet_start:snippet_end]}{suffix}"


def _sentence_boundaries(text: str) -> list[int]:
    """返回可安全切开的句末位置，保留结尾引号和括号。"""

    boundaries: list[int] = []
    for index, char in enumerate(text):
        if char == "\n":
            boundaries.append(index + 1)
            continue
        if char not in _SENTENCE_TERMINATORS and char != ".":
            continue
        if char == ".":
            previous = text[index - 1] if index > 0 else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous.isdigit() and following.isdigit():
                continue
            if following and not following.isspace() and following not in _SENTENCE_CLOSERS:
                continue
        boundary = index + 1
        while boundary < len(text) and text[boundary] in _SENTENCE_CLOSERS:
            boundary += 1
        boundaries.append(boundary)
    return boundaries
