from __future__ import annotations

import hashlib
import unicodedata
from pathlib import Path

from pydantic import ValidationError

from app.collections.models import CurrentCollectionResponse, SnapshotDocument
from app.collections.service import CollectionSnapshotService
from app.ingestion.errors import IngestFailure
from app.ingestion.models import (
    DocumentLanguage,
    IngestDocumentResponse,
    SectionRecord,
    SectionStats,
)
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import (
    GetOutlineResult,
    ListBlocksResult,
    ListDocumentsResult,
    RetrievalBlock,
    RetrievalDocumentOutline,
    RetrievalDocumentSummary,
    RetrievalErrorPayload,
    RetrievalOutlineItem,
    RetrievalOutlineSection,
    ScanMatch,
    ScanTermResult,
)

SCAN_SNIPPET_CONTEXT_CHARS = 80
_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_ASCII_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ASCII_FOLD_TABLE = str.maketrans(_ASCII_UPPER, _ASCII_LOWER)


class RetrievalReadService:
    """为一次读取操作绑定当前活动 collection 快照。"""

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
            current=current,
            snapshot_root=self._collections_root / current.manifest.snapshot_id,
        )


class RetrievalSnapshotReader:
    """只读一个不可变 collection 快照的确定性检索读取器。"""

    def __init__(
        self,
        *,
        current: CurrentCollectionResponse,
        snapshot_root: Path,
    ) -> None:
        self._current = current
        self._snapshot_root = snapshot_root.resolve()
        self._documents_by_id = {
            document.doc_id: document for document in current.manifest.documents
        }
        if len(self._documents_by_id) != len(current.manifest.documents):
            raise RetrievalFailure(
                "snapshot_integrity_error",
                "snapshot manifest contains duplicate document IDs",
            )
        self._document_cache: dict[str, IngestDocumentResponse] = {}

    @property
    def snapshot_id(self) -> str:
        """返回 reader 生命周期内固定的快照 ID。"""

        return self._current.manifest.snapshot_id

    @property
    def completeness(self) -> str:
        """返回固定快照的完整性。"""

        return self._current.manifest.completeness

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
        for summary in self._current.manifest.documents:
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
    ) -> ListBlocksResult:
        """返回单篇文档指定章节及其全部后代的完整块集合。"""

        normalized_doc_id = _required_non_empty(doc_id, "doc_id")
        persisted = self._load_document(normalized_doc_id)
        existing_paths = {tuple(section.path) for section in persisted.section_tree.sections}
        selected_paths = [
            tuple(path) for path in (section_paths if section_paths is not None else [])
        ]
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
            or any(tuple(block.section_path[: len(path)]) == path for path in selected_paths)
        ]
        selected_blocks.sort(key=lambda block: block.block_index)
        blocks = [
            RetrievalBlock(
                block_id=block.block_id,
                doc_id=normalized_doc_id,
                doc_title=persisted.document.title,
                source_label=persisted.document.source_label,
                section_path=block.section_path,
                text=block.text,
            )
            for block in selected_blocks
        ]
        return ListBlocksResult(blocks=blocks, total=len(blocks))

    def scan_term(
        self,
        term: str,
        doc_ids: list[str] | None = None,
    ) -> ScanTermResult:
        """在原文上执行 NFC＋英文大小写折叠的确定性子串扫描。"""

        normalized_term = _required_non_empty(term, "term")
        requested_ids = (
            [document.doc_id for document in self._current.manifest.documents]
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


def _snippet(text: str, start: int, end: int) -> str:
    snippet_start = max(0, start - SCAN_SNIPPET_CONTEXT_CHARS)
    snippet_end = min(len(text), end + SCAN_SNIPPET_CONTEXT_CHARS)
    prefix = "…" if snippet_start > 0 else ""
    suffix = "…" if snippet_end < len(text) else ""
    return f"{prefix}{text[snippet_start:snippet_end]}{suffix}"
