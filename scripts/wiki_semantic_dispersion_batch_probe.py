"""按叶节点语义近邻图重排跨树批次，并回放证据挂接结果。"""

# 探针必须先设置 backend 与 scripts 搜索路径，之后才能导入依赖探针。
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import itertools
import json
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

SCRIPT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_ROOT.parent / "backend"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import (
    DEEPSEEK_BASE_URL,
    ChatProviderProfile,
    DeepSeekChatProvider,
)
from app.providers.embeddings import DashScopeEmbeddingProvider, EmbeddingProvider
from app.storage.credentials import (
    CHAT_API_KEY_USERNAME,
    EMBEDDING_API_KEY_USERNAME,
    WindowsCredentialStore,
)
from wiki_leaf_batch_support_probe import MODEL_ID, run_one
from wiki_leaf_support_probe import (
    FOREST_PATH,
    MAX_OUTPUT_TOKENS,
    load_block,
    load_json,
    load_leaves,
)
from wiki_root_tree_batch_probe import collect_leaf_ids, find_node
from wiki_routed_leaf_batch_probe import (
    BATCH_SYSTEM_PROMPT,
    DEFAULT_ROOT_RESULT_PATH,
    build_batch_plan,
    build_probes,
    expected_supported,
    load_retained_root_ids,
    summarize,
)

DEFAULT_TOP_FRACTION = 0.10
DEFAULT_RESTARTS = 48
DEFAULT_MAX_CONCURRENCY = 4
DEFAULT_RANDOM_SEED = 20261124
LEAF_IDENTITY_POLICY_VERSION = "leaf-identity-v1"
OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "semantic_dispersion_batch_probe"


@dataclass(frozen=True, slots=True)
class ConflictGraph:
    """保存叶节点两两余弦相似度及其 k 近邻冲突边。"""

    leaf_ids: tuple[str, ...]
    similarity: np.ndarray
    conflict_weights: dict[tuple[int, int], float]
    neighbors: dict[str, tuple[str, ...]]
    top_k: int


def leaf_identity_text(leaf: dict[str, Any]) -> str:
    """把叶节点知识身份序列化为稳定的 embedding 输入。"""

    exclusions = "；".join(leaf.get("exclusions") or []) or "无"
    return (
        f"叶节点：{leaf['title']}\n"
        f"完整路径：{' > '.join(leaf['path'])}\n"
        f"范围：{leaf['description']}\n"
        f"排除：{exclusions}"
    )


async def embed_leaf_identities(
    provider: EmbeddingProvider,
    leaf_ids: list[str],
    leaves: dict[str, dict[str, Any]],
) -> list[list[float]]:
    """按供应商批大小生成与输入顺序一致的叶节点向量。"""

    vectors: list[list[float]] = []
    for start in range(0, len(leaf_ids), provider.batch_size):
        batch_ids = leaf_ids[start : start + provider.batch_size]
        result = await provider.embed_documents(
            [leaf_identity_text(leaves[leaf_id]) for leaf_id in batch_ids]
        )
        if len(result.vectors) != len(batch_ids):
            raise RuntimeError("embedding provider returned the wrong vector count")
        vectors.extend(result.vectors)
    return vectors


def build_conflict_graph(
    leaf_ids: list[str],
    vectors: list[list[float]],
    *,
    top_fraction: float,
) -> ConflictGraph:
    """按每个叶节点最近的固定比例节点构造对称冲突图。"""

    if len(leaf_ids) != len(vectors):
        raise ValueError("leaf_ids and vectors must have the same length")
    if not 0 < top_fraction <= 1:
        raise ValueError("top_fraction must be in (0, 1]")

    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("leaf embedding vectors must be non-zero")
    normalized = matrix / norms
    similarity = normalized @ normalized.T
    np.fill_diagonal(similarity, -1.0)

    top_k = min(len(leaf_ids) - 1, max(5, math.ceil(len(leaf_ids) * top_fraction)))
    conflict_weights: dict[tuple[int, int], float] = {}
    neighbor_indices: dict[str, set[int]] = {leaf_id: set() for leaf_id in leaf_ids}
    for row_index, leaf_id in enumerate(leaf_ids):
        ranked = np.argsort(similarity[row_index])[::-1][:top_k]
        for raw_neighbor_index in ranked:
            neighbor_index = int(raw_neighbor_index)
            key = tuple(sorted((row_index, neighbor_index)))
            score = float(similarity[row_index, neighbor_index])
            conflict_weights[key] = max(conflict_weights.get(key, 0.0), score)
            neighbor_indices[leaf_id].add(neighbor_index)
            neighbor_indices[leaf_ids[neighbor_index]].add(row_index)

    neighbors = {
        leaf_id: tuple(sorted(leaf_ids[index] for index in indices))
        for leaf_id, indices in neighbor_indices.items()
    }
    return ConflictGraph(
        leaf_ids=tuple(leaf_ids),
        similarity=similarity,
        conflict_weights=conflict_weights,
        neighbors=neighbors,
        top_k=top_k,
    )


def _batch_metric(
    batch: list[str],
    *,
    leaf_index: dict[str, int],
    graph: ConflictGraph,
    leaves: dict[str, dict[str, Any]],
) -> tuple[float, int, int, int, float]:
    """计算一个批次的冲突权重、冲突数、结构重复和总相似度。"""

    conflict_weight = 0.0
    conflict_count = 0
    similarity_sum = 0.0
    for left_offset, left_id in enumerate(batch):
        left_index = leaf_index[left_id]
        for right_id in batch[left_offset + 1 :]:
            right_index = leaf_index[right_id]
            similarity_sum += float(graph.similarity[left_index, right_index])
            key = tuple(sorted((left_index, right_index)))
            if key in graph.conflict_weights:
                conflict_count += 1
                conflict_weight += graph.conflict_weights[key]
    root_counts = Counter(leaf_id.split(".", 1)[0] for leaf_id in batch)
    branch_counts = Counter(tuple(leaves[leaf_id]["path"][:-1]) for leaf_id in batch)
    root_duplicates = sum(count * (count - 1) // 2 for count in root_counts.values())
    branch_duplicates = sum(count * (count - 1) // 2 for count in branch_counts.values())
    return (
        conflict_weight,
        conflict_count,
        root_duplicates,
        branch_duplicates,
        similarity_sum,
    )


def evaluate_plan(
    batches: list[list[str]],
    *,
    graph: ConflictGraph,
    leaves: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """汇总批次计划的语义冲突与结构多样性指标。"""

    leaf_index = {leaf_id: index for index, leaf_id in enumerate(graph.leaf_ids)}
    metrics = [
        _batch_metric(
            batch,
            leaf_index=leaf_index,
            graph=graph,
            leaves=leaves,
        )
        for batch in batches
    ]
    return {
        "semantic_conflict_weight": sum(item[0] for item in metrics),
        "semantic_conflict_count": sum(item[1] for item in metrics),
        "root_duplicate_count": sum(item[2] for item in metrics),
        "branch_duplicate_count": sum(item[3] for item in metrics),
        "within_batch_similarity_sum": sum(item[4] for item in metrics),
        "batches_with_semantic_conflicts": sum(item[1] > 0 for item in metrics),
    }


def _plan_score(metrics: dict[str, Any]) -> tuple[float, int, int, int, float]:
    """把计划指标转为确定性的字典序优化目标。"""

    return (
        float(metrics["semantic_conflict_weight"]),
        int(metrics["semantic_conflict_count"]),
        int(metrics["branch_duplicate_count"]),
        int(metrics["root_duplicate_count"]),
        float(metrics["within_batch_similarity_sum"]),
    )


def _greedy_plan(
    ordered_leaf_ids: list[str],
    *,
    batch_sizes: list[int],
    graph: ConflictGraph,
    leaves: dict[str, dict[str, Any]],
    randomizer: random.Random,
) -> list[list[str]]:
    """按近邻冲突最小、结构重复最少和容量均衡依次放置叶节点。"""

    leaf_index = {leaf_id: index for index, leaf_id in enumerate(graph.leaf_ids)}
    batches: list[list[str]] = [[] for _ in batch_sizes]
    remaining = list(batch_sizes)

    for leaf_id in ordered_leaf_ids:
        leaf_index_value = leaf_index[leaf_id]
        candidates = [batch_index for batch_index, capacity in enumerate(remaining) if capacity > 0]

        def placement_score(
            batch_index: int,
            *,
            current_leaf_id: str = leaf_id,
            current_leaf_index: int = leaf_index_value,
        ) -> tuple[float, int, int, int, int, float]:
            members = batches[batch_index]
            conflict_weight = 0.0
            conflict_count = 0
            for member_id in members:
                key = tuple(sorted((current_leaf_index, leaf_index[member_id])))
                if key in graph.conflict_weights:
                    conflict_count += 1
                    conflict_weight += graph.conflict_weights[key]
            branch_duplicates = sum(
                leaves[current_leaf_id]["path"][:-1] == leaves[member_id]["path"][:-1]
                for member_id in members
            )
            root_duplicates = sum(
                current_leaf_id.split(".", 1)[0] == member_id.split(".", 1)[0]
                for member_id in members
            )
            return (
                conflict_weight,
                conflict_count,
                branch_duplicates,
                root_duplicates,
                -remaining[batch_index],
                randomizer.random(),
            )

        selected_batch = min(candidates, key=placement_score)
        batches[selected_batch].append(leaf_id)
        remaining[selected_batch] -= 1

    if any(remaining):
        raise RuntimeError(f"semantic allocator left capacity unused: {remaining}")
    return batches


def _improve_by_swaps(
    batches: list[list[str]],
    *,
    graph: ConflictGraph,
    leaves: dict[str, dict[str, Any]],
    max_rounds: int = 20,
) -> list[list[str]]:
    """通过跨批交换继续降低近邻共现，同时保护结构多样性。"""

    current = [list(batch) for batch in batches]
    leaf_index = {leaf_id: index for index, leaf_id in enumerate(graph.leaf_ids)}
    batch_metrics = [
        _batch_metric(
            batch,
            leaf_index=leaf_index,
            graph=graph,
            leaves=leaves,
        )
        for batch in current
    ]
    current_metrics = {
        "semantic_conflict_weight": sum(item[0] for item in batch_metrics),
        "semantic_conflict_count": sum(item[1] for item in batch_metrics),
        "root_duplicate_count": sum(item[2] for item in batch_metrics),
        "branch_duplicate_count": sum(item[3] for item in batch_metrics),
        "within_batch_similarity_sum": sum(item[4] for item in batch_metrics),
    }
    current_score = _plan_score(current_metrics)

    for _ in range(max_rounds):
        best_score = current_score
        best_swap: tuple[int, int, int, int] | None = None
        best_batch_metrics: (
            tuple[
                tuple[float, int, int, int, float],
                tuple[float, int, int, int, float],
            ]
            | None
        ) = None
        conflict_batches = {
            batch_index for batch_index, metric in enumerate(batch_metrics) if metric[1] > 0
        }
        for left_batch_index in sorted(conflict_batches):
            for right_batch_index in range(len(current)):
                if left_batch_index == right_batch_index:
                    continue
                left_batch = current[left_batch_index]
                right_batch = current[right_batch_index]
                for left_offset, left_id in enumerate(left_batch):
                    for right_offset, right_id in enumerate(right_batch):
                        candidate_left = list(left_batch)
                        candidate_right = list(right_batch)
                        candidate_left[left_offset] = right_id
                        candidate_right[right_offset] = left_id
                        left_metric = _batch_metric(
                            candidate_left,
                            leaf_index=leaf_index,
                            graph=graph,
                            leaves=leaves,
                        )
                        right_metric = _batch_metric(
                            candidate_right,
                            leaf_index=leaf_index,
                            graph=graph,
                            leaves=leaves,
                        )
                        old_left = batch_metrics[left_batch_index]
                        old_right = batch_metrics[right_batch_index]
                        metrics = {
                            "semantic_conflict_weight": current_metrics["semantic_conflict_weight"]
                            - old_left[0]
                            - old_right[0]
                            + left_metric[0]
                            + right_metric[0],
                            "semantic_conflict_count": current_metrics["semantic_conflict_count"]
                            - old_left[1]
                            - old_right[1]
                            + left_metric[1]
                            + right_metric[1],
                            "root_duplicate_count": current_metrics["root_duplicate_count"]
                            - old_left[2]
                            - old_right[2]
                            + left_metric[2]
                            + right_metric[2],
                            "branch_duplicate_count": current_metrics["branch_duplicate_count"]
                            - old_left[3]
                            - old_right[3]
                            + left_metric[3]
                            + right_metric[3],
                            "within_batch_similarity_sum": current_metrics[
                                "within_batch_similarity_sum"
                            ]
                            - old_left[4]
                            - old_right[4]
                            + left_metric[4]
                            + right_metric[4],
                        }
                        score = _plan_score(metrics)
                        if score < best_score:
                            best_score = score
                            best_swap = (
                                left_batch_index,
                                left_offset,
                                right_batch_index,
                                right_offset,
                            )
                            best_batch_metrics = (left_metric, right_metric)
        if best_swap is None or best_batch_metrics is None:
            break
        left_batch_index, left_offset, right_batch_index, right_offset = best_swap
        (
            current[left_batch_index][left_offset],
            current[right_batch_index][right_offset],
        ) = (
            current[right_batch_index][right_offset],
            current[left_batch_index][left_offset],
        )
        batch_metrics[left_batch_index] = best_batch_metrics[0]
        batch_metrics[right_batch_index] = best_batch_metrics[1]
        current_metrics = {
            "semantic_conflict_weight": sum(item[0] for item in batch_metrics),
            "semantic_conflict_count": sum(item[1] for item in batch_metrics),
            "root_duplicate_count": sum(item[2] for item in batch_metrics),
            "branch_duplicate_count": sum(item[3] for item in batch_metrics),
            "within_batch_similarity_sum": sum(item[4] for item in batch_metrics),
        }
        current_score = best_score

    # 保持局部变量参与索引完整性检查，避免叶节点集合在交换中被悄悄改写。
    if {leaf_id for batch in current for leaf_id in batch} != set(leaf_index):
        raise RuntimeError("swap optimizer changed the leaf set")
    return current


def _order_batch_by_dispersion(
    batch: list[str],
    *,
    graph: ConflictGraph,
) -> list[str]:
    """把批次内相似度较高的节点尽量放到输入顺序两端。"""

    leaf_index = {leaf_id: index for index, leaf_id in enumerate(graph.leaf_ids)}
    remaining = set(batch)
    first_id = max(
        remaining,
        key=lambda leaf_id: (
            len(graph.neighbors[leaf_id]),
            leaf_id,
        ),
    )
    ordered = [first_id]
    remaining.remove(first_id)
    while remaining:
        next_id = min(
            remaining,
            key=lambda leaf_id: (
                max(
                    float(graph.similarity[leaf_index[leaf_id], leaf_index[placed_id]])
                    for placed_id in ordered
                ),
                sum(
                    float(graph.similarity[leaf_index[leaf_id], leaf_index[placed_id]])
                    for placed_id in ordered
                ),
                leaf_id,
            ),
        )
        ordered.append(next_id)
        remaining.remove(next_id)
    return ordered


def _adjacent_conflict_metric(
    batch: list[str],
    *,
    graph: ConflictGraph,
) -> tuple[float, int]:
    """计算相邻叶节点的语义冲突权重和冲突数。"""

    leaf_index = {leaf_id: index for index, leaf_id in enumerate(graph.leaf_ids)}
    conflict_weight = 0.0
    conflict_count = 0
    for left_id, right_id in itertools.pairwise(batch):
        key = tuple(sorted((leaf_index[left_id], leaf_index[right_id])))
        if key in graph.conflict_weights:
            conflict_weight += graph.conflict_weights[key]
            conflict_count += 1
    return conflict_weight, conflict_count


def build_semantic_dispersion_plan(
    structural_batches: list[list[str]],
    *,
    graph: ConflictGraph,
    leaves: dict[str, dict[str, Any]],
    restarts: int,
    random_seed: int,
    organizer: str,
    max_swap_rounds: int,
) -> tuple[list[list[str]], dict[str, Any]]:
    """多起点生成低近邻竞争批次，并返回与结构基线相同的容量计划。"""

    if organizer == "minimal-swap":
        structural_membership = {
            leaf_id: batch_index
            for batch_index, batch in enumerate(structural_batches)
            for leaf_id in batch
        }
        improved = _improve_by_swaps(
            structural_batches,
            graph=graph,
            leaves=leaves,
            max_rounds=max_swap_rounds,
        )
        changed_batch_indices = {
            batch_index
            for batch_index, (before, after) in enumerate(
                zip(structural_batches, improved, strict=True)
            )
            if set(before) != set(after)
        }
        ordered_batches = [
            _order_batch_by_dispersion(batch, graph=graph)
            if batch_index in changed_batch_indices
            else list(batch)
            for batch_index, batch in enumerate(improved)
        ]
        changed_leaf_count = sum(
            structural_membership[leaf_id] != batch_index
            for batch_index, batch in enumerate(improved)
            for leaf_id in batch
        )
        metrics = evaluate_plan(ordered_batches, graph=graph, leaves=leaves)
        return ordered_batches, {
            "optimizer": "minimal-conflict-swap",
            "max_swap_rounds": max_swap_rounds,
            "changed_leaf_count": changed_leaf_count,
            "changed_batch_count": len(changed_batch_indices),
            "metrics": metrics,
        }
    if organizer == "ordering-only":
        ordered_batches = [
            _order_batch_by_dispersion(batch, graph=graph) for batch in structural_batches
        ]
        reordered_batch_count = sum(
            before != after
            for before, after in zip(structural_batches, ordered_batches, strict=True)
        )
        metrics = evaluate_plan(ordered_batches, graph=graph, leaves=leaves)
        return ordered_batches, {
            "optimizer": "structural-membership-maximin-order",
            "changed_leaf_count": 0,
            "reordered_batch_count": reordered_batch_count,
            "metrics": metrics,
        }
    if organizer == "adjacent-order":
        ordered_batches: list[list[str]] = []
        reordered_batch_count = 0
        for batch in structural_batches:
            candidate = _order_batch_by_dispersion(batch, graph=graph)
            original_score = _adjacent_conflict_metric(batch, graph=graph)
            candidate_score = _adjacent_conflict_metric(candidate, graph=graph)
            if candidate_score < original_score:
                ordered_batches.append(candidate)
                reordered_batch_count += 1
            else:
                ordered_batches.append(list(batch))
        metrics = evaluate_plan(ordered_batches, graph=graph, leaves=leaves)
        return ordered_batches, {
            "optimizer": "conditional-adjacent-maximin-order",
            "changed_leaf_count": 0,
            "reordered_batch_count": reordered_batch_count,
            "metrics": metrics,
        }
    if organizer != "global-dispersion":
        raise ValueError(f"unknown organizer: {organizer}")

    leaf_ids = list(graph.leaf_ids)
    degree = {leaf_id: len(graph.neighbors[leaf_id]) for leaf_id in leaf_ids}
    batch_sizes = [len(batch) for batch in structural_batches]
    randomizer = random.Random(random_seed)
    best_batches: list[list[str]] | None = None
    best_metrics: dict[str, Any] | None = None

    for restart_index in range(max(1, restarts)):
        if restart_index == 0:
            ordered = sorted(leaf_ids, key=lambda leaf_id: (-degree[leaf_id], leaf_id))
        else:
            ordered = sorted(
                leaf_ids,
                key=lambda leaf_id: (
                    -(degree[leaf_id] + randomizer.random() * 2.0),
                    leaf_id,
                ),
            )
        candidate = _greedy_plan(
            ordered,
            batch_sizes=batch_sizes,
            graph=graph,
            leaves=leaves,
            randomizer=randomizer,
        )
        metrics = evaluate_plan(candidate, graph=graph, leaves=leaves)
        if best_metrics is None or _plan_score(metrics) < _plan_score(best_metrics):
            best_batches = candidate
            best_metrics = metrics

    if best_batches is None or best_metrics is None:
        raise RuntimeError("semantic allocator did not produce a plan")
    improved = _improve_by_swaps(best_batches, graph=graph, leaves=leaves)
    improved_metrics = evaluate_plan(improved, graph=graph, leaves=leaves)
    ordered_batches = [_order_batch_by_dispersion(batch, graph=graph) for batch in improved]
    return ordered_batches, {
        "optimizer": "greedy-multistart-plus-swap",
        "restarts": restarts,
        "random_seed": random_seed,
        "metrics": improved_metrics,
    }


def build_batch_artifacts(
    batches: list[list[str]],
    graph: ConflictGraph,
    leaves: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """构造与现有回放器兼容的批次元数据。"""

    leaf_index = {leaf_id: index for index, leaf_id in enumerate(graph.leaf_ids)}
    artifacts: list[dict[str, Any]] = []
    for batch_index, leaf_ids in enumerate(batches, start=1):
        metric = _batch_metric(
            leaf_ids,
            leaf_index=leaf_index,
            graph=graph,
            leaves=leaves,
        )
        artifacts.append(
            {
                "batch_id": f"S01-B{batch_index:03d}",
                "coverage_pass": 1,
                "batch_index": batch_index,
                "leaf_ids": leaf_ids,
                "root_ids": sorted({leaf_id.split(".", 1)[0] for leaf_id in leaf_ids}),
                "branch_paths": sorted(
                    {" > ".join(leaves[leaf_id]["path"][:-1]) for leaf_id in leaf_ids}
                ),
                "leaf_count": len(leaf_ids),
                "root_count": len({leaf_id.split(".", 1)[0] for leaf_id in leaf_ids}),
                "branch_count": len({tuple(leaves[leaf_id]["path"][:-1]) for leaf_id in leaf_ids}),
                "semantic_conflict_weight": metric[0],
                "semantic_conflict_count": metric[1],
            }
        )
    return artifacts


def parse_args() -> argparse.Namespace:
    """解析语义批次组织与模型回放参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-result-path", type=Path, default=DEFAULT_ROOT_RESULT_PATH)
    parser.add_argument("--root-run-index", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument(
        "--organizer",
        choices=(
            "adjacent-order",
            "ordering-only",
            "minimal-swap",
            "global-dispersion",
        ),
        default="minimal-swap",
    )
    parser.add_argument("--max-swap-rounds", type=int, default=20)
    parser.add_argument("--top-fraction", type=float, default=DEFAULT_TOP_FRACTION)
    parser.add_argument("--restarts", type=int, default=DEFAULT_RESTARTS)
    parser.add_argument("--random-seed", type=int, default=DEFAULT_RANDOM_SEED)
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if not 0 < args.top_fraction <= 1:
        parser.error("--top-fraction must be in (0, 1]")
    if args.restarts <= 0:
        parser.error("--restarts must be positive")
    if args.max_swap_rounds <= 0:
        parser.error("--max-swap-rounds must be positive")
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    return args


async def main() -> None:
    """生成语义分散批次，按需调用当前挂接提示词并保存完整产物。"""

    args = parse_args()
    root_ids, root_source = load_retained_root_ids(
        args.root_result_path,
        args.root_run_index,
    )
    forest = load_json(FOREST_PATH)
    leaves = load_leaves(forest)
    selected_leaf_ids = [
        leaf_id for root_id in root_ids for leaf_id in collect_leaf_ids(find_node(forest, root_id))
    ]
    if len(selected_leaf_ids) != len(set(selected_leaf_ids)):
        raise RuntimeError("selected leaves contain duplicates")

    credentials = WindowsCredentialStore()
    embedding_key = credentials.get(EMBEDDING_API_KEY_USERNAME)
    if not embedding_key:
        raise RuntimeError("embedding API key is not configured in Windows Credential Manager")
    embedding_provider = DashScopeEmbeddingProvider(embedding_key)
    vectors = await embed_leaf_identities(embedding_provider, selected_leaf_ids, leaves)
    graph = build_conflict_graph(
        selected_leaf_ids,
        vectors,
        top_fraction=args.top_fraction,
    )

    structural_plan = build_batch_plan(
        root_ids,
        forest,
        leaves,
        batch_size=args.batch_size,
        coverage_passes=1,
    )
    structural_batches = [batch["leaf_ids"] for batch in structural_plan]
    structural_metrics = evaluate_plan(
        structural_batches,
        graph=graph,
        leaves=leaves,
    )
    semantic_batches, optimizer = build_semantic_dispersion_plan(
        structural_batches,
        graph=graph,
        leaves=leaves,
        restarts=args.restarts,
        random_seed=args.random_seed,
        organizer=args.organizer,
        max_swap_rounds=args.max_swap_rounds,
    )
    batches = build_batch_artifacts(semantic_batches, graph, leaves)

    identity_hashes = {
        leaf_id: hashlib.sha256(leaf_identity_text(leaves[leaf_id]).encode("utf-8")).hexdigest()
        for leaf_id in selected_leaf_ids
    }
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    artifact: dict[str, Any] = {
        "experiment": "semantic-dispersion cross-tree leaf batching",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "dry_run": args.dry_run,
        "forest_path": str(FOREST_PATH),
        "root_source": root_source,
        "retained_root_ids": root_ids,
        "leaf_count": len(selected_leaf_ids),
        "batch_size_target": args.batch_size,
        "system_prompt": BATCH_SYSTEM_PROMPT,
        "system_prompt_sha256": hashlib.sha256(BATCH_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "leaf_identity_policy_version": LEAF_IDENTITY_POLICY_VERSION,
        "leaf_identity_sha256": identity_hashes,
        "embedding_profile": embedding_provider.profile.model_dump(mode="json"),
        "conflict_graph": {
            "top_fraction": args.top_fraction,
            "top_k": graph.top_k,
            "edge_count": len(graph.conflict_weights),
            "max_degree": max(len(neighbors) for neighbors in graph.neighbors.values()),
        },
        "structural_plan_metrics": structural_metrics,
        "semantic_plan_metrics": optimizer["metrics"],
        "optimizer": optimizer,
        "batches": batches,
    }

    if args.dry_run:
        artifact["summary"] = {
            "batch_count": len(batches),
            "selected_leaf_count": len(selected_leaf_ids),
            "positive_count": sum(expected_supported(leaf_id) for leaf_id in selected_leaf_ids),
        }
    else:
        chat_key = credentials.get(CHAT_API_KEY_USERNAME)
        if not chat_key:
            raise RuntimeError("chat API key is not configured in Windows Credential Manager")
        chat_provider = DeepSeekChatProvider(
            chat_key,
            profile=ChatProviderProfile(
                provider="deepseek",
                model_id=MODEL_ID,
                base_url=DEEPSEEK_BASE_URL,
                temperature=0.0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                thinking_enabled=False,
            ),
        )
        block = load_block()
        semaphore = asyncio.Semaphore(args.max_concurrency)

        async def execute(batch: dict[str, Any], task_index: int) -> dict[str, Any]:
            probes = build_probes(batch["leaf_ids"])
            result = await run_one(
                chat_provider,
                semaphore,
                task_index,
                block,
                probes,
                leaves,
            )
            return {
                **result,
                "coverage_pass": 1,
                "batch_id": batch["batch_id"],
            }

        results = await asyncio.gather(
            *(execute(batch, index) for index, batch in enumerate(batches, start=1))
        )
        artifact["summary"] = summarize(
            results,
            batches,
            selected_leaf_ids=set(selected_leaf_ids),
        )
        artifact["results"] = results

    result_path = run_dir / "results.json"
    result_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "structural_plan_metrics": structural_metrics,
                "semantic_plan_metrics": optimizer["metrics"],
                "summary": artifact["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
