from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import lancedb

from app.indexing.keywords import build_keyword_terms
from app.indexing.lancedb_index import BLOCKS_TABLE_NAME, FTS_FIELD_NAME
from app.retrieval.errors import RetrievalFailure


@dataclass(frozen=True, slots=True)
class HybridRetrievalProfile:
    """一次混合检索固定的查询时参数组。"""

    version: str
    candidate_depth: int
    rrf_k: int
    top_k: int


DEFAULT_HYBRID_RETRIEVAL_PROFILE = HybridRetrievalProfile(
    version="hybrid-rrf-v1",
    candidate_depth=20,
    rrf_k=60,
    top_k=5,
)

RetrievalSearchMode = Literal["hybrid", "dense", "keyword"]


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """两路候选经 RRF 合并后的内部排序记录。"""

    block_id: str
    doc_id: str
    block_index: int
    score: float
    dense_rank: int | None
    keyword_rank: int | None
    dense_score: float | None
    keyword_score: float | None


@dataclass(frozen=True, slots=True)
class HybridIndexCandidates:
    """LanceDB 两路召回和融合后的内部候选。"""

    dense_rows: tuple[dict[str, Any], ...]
    keyword_rows: tuple[dict[str, Any], ...]
    fused: tuple[FusedCandidate, ...]


def search_snapshot_index(
    snapshot_root: Path,
    *,
    query_vector: list[float] | None,
    query: str,
    doc_ids: tuple[str, ...],
    profile: HybridRetrievalProfile = DEFAULT_HYBRID_RETRIEVAL_PROFILE,
    mode: RetrievalSearchMode = "hybrid",
) -> HybridIndexCandidates:
    """在同一 LanceDB 快照上执行指定模式的候选召回。"""

    index_root = snapshot_root / "index" / "lance"
    if not index_root.is_dir():
        raise RetrievalFailure(
            "tool_internal_error",
            "collection index directory is missing",
        )
    where_clause = _doc_scope_filter(doc_ids)
    try:
        table = lancedb.connect(str(index_root)).open_table(BLOCKS_TABLE_NAME)
        if mode in {"hybrid", "dense"}:
            if query_vector is None:
                raise RetrievalFailure(
                    "tool_internal_error",
                    "dense retrieval requires a query vector",
                )
            dense_query = (
                table.search(
                    query_vector,
                    vector_column_name="vector",
                    query_type="vector",
                )
                .select(["block_id", "doc_id", "block_index", "_distance"])
                .limit(profile.candidate_depth)
            )
            if where_clause is not None:
                dense_query = dense_query.where(where_clause, prefilter=True)
            dense_rows = tuple(dense_query.to_list())
        else:
            dense_rows = ()

        if mode in {"hybrid", "keyword"}:
            keyword_terms = build_keyword_terms(query)
            if keyword_terms:
                keyword_query = (
                    table.search(
                        keyword_terms,
                        query_type="fts",
                        fts_columns=FTS_FIELD_NAME,
                    )
                    .select(["block_id", "doc_id", "block_index", "_score"])
                    .limit(profile.candidate_depth)
                )
                if where_clause is not None:
                    keyword_query = keyword_query.where(where_clause, prefilter=True)
                keyword_rows = tuple(keyword_query.to_list())
            else:
                keyword_rows = ()
        else:
            keyword_rows = ()
    except RetrievalFailure:
        raise
    except Exception as error:
        raise RetrievalFailure(
            "tool_internal_error",
            f"collection index query failed: {type(error).__name__}",
        ) from error

    fused = reciprocal_rank_fusion(dense_rows, keyword_rows, rrf_k=profile.rrf_k)
    return HybridIndexCandidates(
        dense_rows=dense_rows,
        keyword_rows=keyword_rows,
        fused=fused,
    )


def reciprocal_rank_fusion(
    dense_rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    keyword_rows: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    *,
    rrf_k: int,
) -> tuple[FusedCandidate, ...]:
    """按固定 RRF 公式融合两路排名，并应用稳定平局规则。"""

    scores: dict[str, float] = {}
    metadata: dict[str, tuple[str, int]] = {}
    dense_ranks: dict[str, int] = {}
    keyword_ranks: dict[str, int] = {}
    dense_scores: dict[str, float] = {}
    keyword_scores: dict[str, float] = {}
    for ranks, score_name, scores_by_id, rows in (
        (dense_ranks, "_distance", dense_scores, dense_rows),
        (keyword_ranks, "_score", keyword_scores, keyword_rows),
    ):
        seen: set[str] = set()
        for rank, row in enumerate(rows, start=1):
            block_id, doc_id, block_index = _candidate_identity(row)
            if block_id in seen:
                continue
            seen.add(block_id)
            ranks[block_id] = rank
            metadata[block_id] = (doc_id, block_index)
            scores[block_id] = scores.get(block_id, 0.0) + 1.0 / (rrf_k + rank)
            raw_score = row.get(score_name)
            if isinstance(raw_score, int | float):
                scores_by_id[block_id] = float(raw_score)

    candidates = [
        FusedCandidate(
            block_id=block_id,
            doc_id=metadata[block_id][0],
            block_index=metadata[block_id][1],
            score=score,
            dense_rank=dense_ranks.get(block_id),
            keyword_rank=keyword_ranks.get(block_id),
            dense_score=dense_scores.get(block_id),
            keyword_score=keyword_scores.get(block_id),
        )
        for block_id, score in scores.items()
    ]
    candidates.sort(
        key=lambda candidate: (
            -candidate.score,
            candidate.doc_id,
            candidate.block_index,
            candidate.block_id,
        )
    )
    return tuple(candidates)


def _candidate_identity(row: dict[str, Any]) -> tuple[str, str, int]:
    block_id = row.get("block_id")
    doc_id = row.get("doc_id")
    block_index = row.get("block_index")
    if (
        not isinstance(block_id, str)
        or not block_id
        or not isinstance(doc_id, str)
        or not doc_id
        or not isinstance(block_index, int)
    ):
        raise RetrievalFailure(
            "tool_internal_error",
            "collection index returned an invalid candidate identity",
        )
    return block_id, doc_id, block_index


def _doc_scope_filter(doc_ids: tuple[str, ...]) -> str | None:
    if not doc_ids:
        return None
    values = ", ".join(f"'{doc_id.replace("'", "''")}'" for doc_id in doc_ids)
    return f"doc_id IN ({values})"
