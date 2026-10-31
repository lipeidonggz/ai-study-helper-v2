from __future__ import annotations

import asyncio
import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.indexing.keywords import analyze_keyword_terms
from app.ingestion.blocks import estimate_context_tokens
from app.providers.embeddings import (
    EmbeddingProvider,
    EmbeddingProviderError,
)
from app.retrieval.errors import RetrievalFailure
from app.retrieval.models import (
    RetrievalBlock,
    RetrievalDocumentOutline,
    RetrievalOutlineSection,
)

if TYPE_CHECKING:
    from app.retrieval.service import RetrievalSnapshotReader

SectionKind = Literal["facet", "generic", "boilerplate", "code_example", "document_root"]
SectionSearchMode = Literal["lexical", "dense", "hybrid"]

_ZERO_WIDTH_PATTERN = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
_TRAILING_ANCHOR_PATTERN = re.compile(r"#+\s*$")
_BOILERPLATE_HEADINGS = frozenset(
    {
        "acknowledgements",
        "acknowledgments",
        "acknowlegements",
        "author",
        "authors",
        "citation",
        "footnotes",
        "get the developer newsletter",
        "keep reading",
        "references",
        "related resources",
    }
)
_GENERIC_HEADINGS = frozenset(
    {
        "appendix",
        "conclusion",
        "future work",
        "looking ahead",
        "overview",
        "summary",
    }
)
_CODE_HEADINGS = frozenset({"javascript", "python", "typescript"})


@dataclass(frozen=True, slots=True)
class SectionIndexEntry:
    """一个可检索章节及其稳定来源。"""

    section_id: str
    snapshot_id: str
    doc_id: str
    doc_title: str
    source_label: str
    path: tuple[str, ...]
    parent_path: tuple[str, ...]
    title: str
    depth: int
    own_blocks: int
    subtree_blocks: int
    subtree_context_tokens: int
    first_line: str
    kind: SectionKind
    routing_eligible: bool
    search_text: str

    @property
    def path_text(self) -> str:
        """返回便于展示和日志记录的章节路径。"""

        return " > ".join(self.path)


@dataclass(frozen=True, slots=True)
class SectionIndexStats:
    """描述一次章节索引构建的确定性统计。"""

    documents: int
    sections: int
    routing_eligible: int
    excluded_no_blocks: int
    document_roots: int
    boilerplate: int
    code_examples: int
    generic: int


@dataclass(frozen=True, slots=True)
class SectionSearchCandidate:
    """章节检索命中的来源和分路排名。"""

    entry: SectionIndexEntry
    score: float
    lexical_score: float | None = None
    dense_score: float | None = None
    lexical_rank: int | None = None
    dense_rank: int | None = None
    query_support_count: int = 1


@dataclass(frozen=True, slots=True)
class SectionVectorIndex:
    """绑定章节索引和固定 embedding profile 的向量索引。"""

    section_index: SectionIndex
    provider: str
    model_id: str
    dimension: int
    vectors: dict[str, tuple[float, ...]]
    request_ids: tuple[str, ...]
    input_tokens: int


@dataclass(frozen=True, slots=True)
class ExpandedSectionBlock:
    """从章节命中展开出的 block 及其章节来源。"""

    block: RetrievalBlock
    section_id: str
    section_path: tuple[str, ...]
    section_rank: int
    section_score: float
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class SectionIndex:
    """一个固定 collection snapshot 的只读章节检索索引。"""

    snapshot_id: str
    entries: tuple[SectionIndexEntry, ...]

    @classmethod
    def from_reader(cls, reader: RetrievalSnapshotReader) -> SectionIndex:
        """从固定快照 reader 构建确定性的章节索引。"""

        documents = reader.list_documents().documents
        outlines = reader.get_outline([document.doc_id for document in documents]).documents
        resolved: list[RetrievalDocumentOutline] = []
        for item in outlines:
            if item.document is None:
                message = (
                    item.error.message if item.error is not None else "section outline missing"
                )
                raise RetrievalFailure("snapshot_integrity_error", message)
            resolved.append(item.document)
        return cls.from_documents(reader.snapshot_id, resolved)

    @classmethod
    def from_documents(
        cls,
        snapshot_id: str,
        documents: Iterable[RetrievalDocumentOutline],
    ) -> SectionIndex:
        """从已校验的文档大纲构建章节索引。"""

        entries: list[SectionIndexEntry] = []
        for document in sorted(documents, key=lambda item: item.doc_id):
            for position, section in enumerate(document.sections):
                entry = _section_entry(
                    snapshot_id=snapshot_id,
                    document=document,
                    section=section,
                    is_first=position == 0,
                )
                entries.append(entry)
        section_ids = [entry.section_id for entry in entries]
        if len(section_ids) != len(set(section_ids)):
            raise RetrievalFailure(
                "snapshot_integrity_error",
                "section index contains duplicate section IDs",
            )
        entries.sort(key=lambda entry: (entry.doc_id, entry.path, entry.section_id))
        return cls(snapshot_id=snapshot_id, entries=tuple(entries))

    @property
    def stats(self) -> SectionIndexStats:
        """返回构建阶段可审计的章节分类统计。"""

        kinds = Counter(entry.kind for entry in self.entries)
        return SectionIndexStats(
            documents=len({entry.doc_id for entry in self.entries}),
            sections=len(self.entries),
            routing_eligible=sum(entry.routing_eligible for entry in self.entries),
            excluded_no_blocks=sum(
                entry.kind not in {"document_root", "boilerplate", "code_example"}
                and entry.subtree_blocks == 0
                for entry in self.entries
            ),
            document_roots=kinds["document_root"],
            boilerplate=kinds["boilerplate"],
            code_examples=kinds["code_example"],
            generic=kinds["generic"],
        )

    def search_lexical(self, query: str, *, top_k: int = 20) -> tuple[SectionSearchCandidate, ...]:
        """使用固定 analyzer 和 BM25 对可路由章节进行词面检索。"""

        normalized_query = _required_query(query)
        _validate_top_k(top_k)
        entries = tuple(entry for entry in self.entries if entry.routing_eligible)
        if not entries:
            raise RetrievalFailure(
                "no_semantic_sections",
                "snapshot has no semantic sections with descendant blocks",
            )

        query_terms = Counter(analyze_keyword_terms(normalized_query))
        if not query_terms:
            return ()
        documents = [Counter(analyze_keyword_terms(entry.search_text)) for entry in entries]
        lengths = [sum(document.values()) for document in documents]
        average_length = max(1.0, sum(lengths) / len(lengths))
        document_frequency = Counter(
            term for document in documents for term in document if term in query_terms
        )

        scored: list[SectionSearchCandidate] = []
        for entry, term_counts, length in zip(entries, documents, lengths, strict=True):
            score = 0.0
            for term, query_frequency in query_terms.items():
                term_frequency = term_counts.get(term, 0)
                if term_frequency == 0:
                    continue
                frequency = document_frequency[term]
                inverse_frequency = math.log(
                    1.0 + (len(entries) - frequency + 0.5) / (frequency + 0.5)
                )
                denominator = term_frequency + 1.2 * (1.0 - 0.75 + 0.75 * length / average_length)
                score += inverse_frequency * term_frequency * 2.2 / denominator * query_frequency
            if score <= 0:
                continue
            if entry.kind == "generic":
                score *= 0.75
            scored.append(SectionSearchCandidate(entry=entry, score=score))

        scored.sort(key=_candidate_sort_key)
        return tuple(
            SectionSearchCandidate(
                entry=candidate.entry,
                score=candidate.score,
                lexical_score=candidate.score,
                lexical_rank=rank,
                query_support_count=1,
            )
            for rank, candidate in enumerate(scored[:top_k], start=1)
        )

    async def search_dense(
        self,
        query: str,
        *,
        provider: EmbeddingProvider,
        vector_index: SectionVectorIndex,
        top_k: int = 20,
    ) -> tuple[SectionSearchCandidate, ...]:
        """使用预先构建的章节向量索引执行 dense 检索。"""

        normalized_query = _required_query(query)
        _validate_top_k(top_k)
        if not any(entry.routing_eligible for entry in self.entries):
            raise RetrievalFailure(
                "no_semantic_sections",
                "snapshot has no semantic sections with descendant blocks",
            )
        if vector_index.section_index.snapshot_id != self.snapshot_id:
            raise RetrievalFailure(
                "snapshot_integrity_error",
                "section vector index belongs to a different snapshot",
            )
        if (
            provider.profile.provider != vector_index.provider
            or provider.profile.model_id != vector_index.model_id
            or provider.profile.dimension != vector_index.dimension
        ):
            raise RetrievalFailure(
                "collection_unavailable",
                "embedding provider does not match section vector index",
            )
        try:
            query_embedding = await provider.embed_query(normalized_query)
        except EmbeddingProviderError as error:
            raise RetrievalFailure(
                "section_embedding_failed",
                error.message,
                retryable=error.retryable,
            ) from error
        query_vector = tuple(query_embedding.vector)
        if (
            len(query_vector) != vector_index.dimension
            or not all(math.isfinite(value) for value in query_vector)
            or not any(value != 0 for value in query_vector)
        ):
            raise RetrievalFailure(
                "section_embedding_failed",
                "query embedding returned an invalid dense vector",
            )
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        scored: list[SectionSearchCandidate] = []
        for entry in self.entries:
            if not entry.routing_eligible:
                continue
            vector = vector_index.vectors.get(entry.section_id)
            if vector is None:
                raise RetrievalFailure(
                    "snapshot_integrity_error",
                    f"section vector is missing: {entry.section_id}",
                )
            vector_norm = math.sqrt(sum(value * value for value in vector))
            if vector_norm == 0:
                continue
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            score /= query_norm * vector_norm
            scored.append(SectionSearchCandidate(entry=entry, score=score))
        scored.sort(key=_candidate_sort_key)
        return tuple(
            SectionSearchCandidate(
                entry=candidate.entry,
                score=candidate.score,
                dense_score=candidate.score,
                dense_rank=rank,
                query_support_count=1,
            )
            for rank, candidate in enumerate(scored[:top_k], start=1)
        )

    async def search_dense_many(
        self,
        queries: Iterable[str],
        *,
        provider: EmbeddingProvider,
        vector_index: SectionVectorIndex,
        per_query_top_k: int = 10,
        top_k: int = 20,
        rrf_k: int = 60,
        max_concurrency: int = 5,
    ) -> tuple[SectionSearchCandidate, ...]:
        """并行查询多个语义输入，并按章节累计 RRF 覆盖证据。"""

        normalized_queries = tuple(dict.fromkeys(_required_query(query) for query in queries))
        if not normalized_queries:
            raise RetrievalFailure("invalid_argument", "queries must not be empty")
        _validate_top_k(per_query_top_k)
        _validate_top_k(top_k)
        if max_concurrency < 1:
            raise RetrievalFailure("invalid_argument", "max_concurrency must be positive")
        semaphore = asyncio.Semaphore(max_concurrency)

        async def query_one(query: str) -> tuple[SectionSearchCandidate, ...]:
            async with semaphore:
                return await self.search_dense(
                    query,
                    provider=provider,
                    vector_index=vector_index,
                    top_k=per_query_top_k,
                )

        results = await asyncio.gather(*(query_one(query) for query in normalized_queries))
        scores: dict[str, float] = {}
        support: Counter[str] = Counter()
        entries: dict[str, SectionIndexEntry] = {}
        best_rank: dict[str, int] = {}
        best_score: dict[str, float] = {}
        for candidates in results:
            for rank, candidate in enumerate(candidates, start=1):
                section_id = candidate.entry.section_id
                scores[section_id] = scores.get(section_id, 0.0) + 1.0 / (rrf_k + rank)
                support[section_id] += 1
                entries[section_id] = candidate.entry
                best_rank[section_id] = min(best_rank.get(section_id, rank), rank)
                best_score[section_id] = max(
                    best_score.get(section_id, candidate.score),
                    candidate.score,
                )
        fused = [
            SectionSearchCandidate(
                entry=entries[section_id],
                score=score,
                dense_score=best_score[section_id],
                dense_rank=best_rank[section_id],
                query_support_count=support[section_id],
            )
            for section_id, score in scores.items()
        ]
        fused.sort(key=_candidate_sort_key)
        return tuple(fused[:top_k])

    async def search_hybrid(
        self,
        query: str,
        *,
        provider: EmbeddingProvider,
        vector_index: SectionVectorIndex,
        top_k: int = 20,
        rrf_k: int = 60,
    ) -> tuple[SectionSearchCandidate, ...]:
        """使用 RRF 融合章节词面与 dense 排名。"""

        _validate_top_k(top_k)
        lexical = self.search_lexical(query, top_k=max(top_k * 2, top_k))
        dense = await self.search_dense(
            query,
            provider=provider,
            vector_index=vector_index,
            top_k=max(top_k * 2, top_k),
        )
        return _fuse_section_candidates(lexical, dense, top_k=top_k, rrf_k=rrf_k)


async def build_section_vector_index(
    section_index: SectionIndex,
    *,
    provider: EmbeddingProvider,
) -> SectionVectorIndex:
    """为可路由章节批量生成固定 embedding。"""

    entries = tuple(entry for entry in section_index.entries if entry.routing_eligible)
    vectors: dict[str, tuple[float, ...]] = {}
    request_ids: list[str] = []
    input_tokens = 0
    batch_size = max(1, provider.batch_size)
    try:
        for start in range(0, len(entries), batch_size):
            batch = entries[start : start + batch_size]
            result = await provider.embed_documents([entry.search_text for entry in batch])
            if len(result.vectors) != len(batch):
                raise RetrievalFailure(
                    "section_embedding_failed",
                    "section embedding count does not match the batch",
                )
            for entry, vector in zip(batch, result.vectors, strict=True):
                if (
                    len(vector) != provider.profile.dimension
                    or not all(math.isfinite(value) for value in vector)
                    or not any(value != 0 for value in vector)
                ):
                    raise RetrievalFailure(
                        "section_embedding_failed",
                        f"section embedding is invalid: {entry.section_id}",
                    )
                vectors[entry.section_id] = tuple(vector)
            request_ids.append(result.request_id)
            input_tokens += result.input_tokens
    except EmbeddingProviderError as error:
        raise RetrievalFailure(
            "section_embedding_failed",
            error.message,
            retryable=error.retryable,
        ) from error
    return SectionVectorIndex(
        section_index=section_index,
        provider=provider.profile.provider,
        model_id=provider.profile.model_id,
        dimension=provider.profile.dimension,
        vectors=vectors,
        request_ids=tuple(request_ids),
        input_tokens=input_tokens,
    )


def expand_section_blocks(
    reader: RetrievalSnapshotReader,
    candidates: Iterable[SectionSearchCandidate],
    *,
    max_blocks: int = 120,
    max_tokens: int = 40_000,
) -> tuple[ExpandedSectionBlock, ...]:
    """按章节排名展开子树 block，并应用固定去重预算。"""

    if max_blocks < 1 or max_tokens < 1:
        raise RetrievalFailure("invalid_argument", "section expansion budget must be positive")
    expanded: list[ExpandedSectionBlock] = []
    seen_block_ids: set[str] = set()
    used_tokens = 0
    for section_rank, candidate in enumerate(candidates, start=1):
        result = reader.list_blocks(
            candidate.entry.doc_id,
            [list(candidate.entry.path)],
            include_descendants=True,
        )
        for block in result.blocks:
            if block.block_id in seen_block_ids:
                continue
            estimated_tokens = estimate_context_tokens(block.text)
            if len(expanded) >= max_blocks or used_tokens + estimated_tokens > max_tokens:
                return tuple(expanded)
            seen_block_ids.add(block.block_id)
            used_tokens += estimated_tokens
            expanded.append(
                ExpandedSectionBlock(
                    block=block,
                    section_id=candidate.entry.section_id,
                    section_path=candidate.entry.path,
                    section_rank=section_rank,
                    section_score=candidate.score,
                    estimated_tokens=estimated_tokens,
                )
            )
    return tuple(expanded)


def _section_entry(
    *,
    snapshot_id: str,
    document: RetrievalDocumentOutline,
    section: RetrievalOutlineSection,
    is_first: bool,
) -> SectionIndexEntry:
    title = _normalize_title(section.title)
    kind = _classify_section(
        section=section,
        document_title=document.doc_title,
        normalized_title=title,
        is_first=is_first,
    )
    routing_eligible = kind in {"facet", "generic"} and section.subtree_blocks > 0
    path = tuple(section.path)
    section_id = _section_id(document.doc_id, path)
    search_text = "\n".join(
        (
            _normalize_text(document.doc_title),
            " > ".join(_normalize_text(part) for part in path),
            _normalize_text(section.first_line),
        )
    )
    return SectionIndexEntry(
        section_id=section_id,
        snapshot_id=snapshot_id,
        doc_id=document.doc_id,
        doc_title=_normalize_text(document.doc_title),
        source_label=document.source_label,
        path=path,
        parent_path=path[:-1],
        title=title,
        depth=section.depth,
        own_blocks=section.own_blocks,
        subtree_blocks=section.subtree_blocks,
        subtree_context_tokens=section.subtree_context_tokens,
        first_line=_normalize_text(section.first_line),
        kind=kind,
        routing_eligible=routing_eligible,
        search_text=search_text,
    )


def _classify_section(
    *,
    section: RetrievalOutlineSection,
    document_title: str,
    normalized_title: str,
    is_first: bool,
) -> SectionKind:
    if (
        is_first
        and section.depth <= 1
        and len(section.path) <= 1
        and normalized_title == _normalize_title(document_title)
    ):
        return "document_root"
    folded = normalized_title.casefold()
    if folded in _BOILERPLATE_HEADINGS:
        return "boilerplate"
    if folded in _CODE_HEADINGS:
        return "code_example"
    if folded in _GENERIC_HEADINGS or folded.startswith(("what's next", "what’s next")):
        return "generic"
    return "facet"


def _normalize_title(value: str) -> str:
    normalized = _normalize_text(value)
    return _TRAILING_ANCHOR_PATTERN.sub("", normalized).strip()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    return _ZERO_WIDTH_PATTERN.sub("", normalized)


def _section_id(doc_id: str, path: tuple[str, ...]) -> str:
    digest = hashlib.sha256("\0".join(path).encode("utf-8")).hexdigest()[:16]
    return f"{doc_id}#section:{digest}"


def _required_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise RetrievalFailure("invalid_argument", "query must not be empty")
    return normalized


def _validate_top_k(top_k: int) -> None:
    if top_k < 1:
        raise RetrievalFailure("invalid_argument", "top_k must be positive")


def _candidate_sort_key(candidate: SectionSearchCandidate) -> tuple[float, str, tuple[str, ...]]:
    return (-candidate.score, candidate.entry.doc_id, candidate.entry.path)


def _fuse_section_candidates(
    lexical: tuple[SectionSearchCandidate, ...],
    dense: tuple[SectionSearchCandidate, ...],
    *,
    top_k: int,
    rrf_k: int,
) -> tuple[SectionSearchCandidate, ...]:
    by_id: dict[str, SectionSearchCandidate] = {}
    scores: dict[str, float] = {}
    for candidates in (lexical, dense):
        for rank, candidate in enumerate(candidates, start=1):
            section_id = candidate.entry.section_id
            scores[section_id] = scores.get(section_id, 0.0) + 1.0 / (rrf_k + rank)
            existing = by_id.get(candidate.entry.section_id)
            if existing is None:
                by_id[candidate.entry.section_id] = candidate
            else:
                by_id[candidate.entry.section_id] = SectionSearchCandidate(
                    entry=existing.entry,
                    score=0.0,
                    lexical_score=(
                        candidate.lexical_score
                        if candidate.lexical_score is not None
                        else existing.lexical_score
                    ),
                    dense_score=(
                        candidate.dense_score
                        if candidate.dense_score is not None
                        else existing.dense_score
                    ),
                    lexical_rank=(
                        candidate.lexical_rank
                        if candidate.lexical_rank is not None
                        else existing.lexical_rank
                    ),
                    dense_rank=(
                        candidate.dense_rank
                        if candidate.dense_rank is not None
                        else existing.dense_rank
                    ),
                    query_support_count=(
                        existing.query_support_count + candidate.query_support_count
                    ),
                )
    fused = [
        SectionSearchCandidate(
            entry=by_id[section_id].entry,
            score=score,
            lexical_score=by_id[section_id].lexical_score,
            dense_score=by_id[section_id].dense_score,
            lexical_rank=by_id[section_id].lexical_rank,
            dense_rank=by_id[section_id].dense_rank,
            query_support_count=by_id[section_id].query_support_count,
        )
        for section_id, score in scores.items()
    ]
    fused.sort(key=_candidate_sort_key)
    return tuple(fused[:top_k])
