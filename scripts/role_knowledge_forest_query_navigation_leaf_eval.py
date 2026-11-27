"""按叶节点投影重新计算完整知识目录导航的召回率与精确率。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from role_knowledge_forest_query_navigation_probe import (
    QUERIES,
    flatten_nodes,
    load_forest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
RUNTIME_ROOT = BACKEND_ROOT / ".runtime"
RESULT_DIR_NAMES = (
    "role_knowledge_forest_query_navigation_20261123T122403416501Z",
    "role_knowledge_forest_query_navigation_20261123T122623609768Z",
    "role_knowledge_forest_query_navigation_20261123T122702411672Z",
    "role_knowledge_forest_query_navigation_20261123T122709572321Z",
)
EXPECTED_KEY_GROUP_GOLD_COUNTS = {
    "Q1_DATA_LINEAGE": 15,
    "Q2_ISOLATION_BOUNDARIES": 20,
    "Q3_FAILURE_RECOVERY_REGRESSION": 35,
    "Q4_PERMISSION_APPROVAL": 35,
}
ADJACENT_NON_MINIMAL_LEAVES = {
    "Q1_DATA_LINEAGE": {
        "K17.07.01",  # 训练来源合规；问题只要求血缘可追溯，没有要求来源合法性。
        "K17.07.05",  # 数据与模型授权链；同属相邻治理要求，不是血缘链路必需项。
    },
    "Q3_FAILURE_RECOVERY_REGRESSION": {
        "K12.09.03",  # 事件复盘；问题要求恢复有效性验证，没有要求事后复盘闭环。
    },
}


def build_tree_indexes(
    nodes: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """建立节点索引和父节点到直接子节点的映射。"""

    by_id = {node["node_id"]: node for node in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        if parent_id := node["parent_id"]:
            children[parent_id].append(node["node_id"])
    return by_id, children


def collect_leaf_descendants(
    root_id: str,
    children: dict[str, list[str]],
    cache: dict[str, frozenset[str]],
) -> frozenset[str]:
    """返回节点自身或其全部后代叶节点，并缓存递归结果。"""

    if root_id in cache:
        return cache[root_id]
    child_ids = children.get(root_id, [])
    if not child_ids:
        result = frozenset({root_id})
    else:
        leaves: set[str] = set()
        for child_id in child_ids:
            leaves.update(collect_leaf_descendants(child_id, children, cache))
        result = frozenset(leaves)
    cache[root_id] = result
    return result


def build_key_group_gold(
    children: dict[str, list[str]],
    cache: dict[str, frozenset[str]],
) -> dict[str, frozenset[str]]:
    """把既有关键知识组展开为每个问题的最低叶节点集合。"""

    gold: dict[str, frozenset[str]] = {}
    for query in QUERIES:
        leaves: set[str] = set()
        for group in query["required_groups"]:
            for node_id in group["any"]:
                leaves.update(collect_leaf_descendants(node_id, children, cache))
        gold[query["query_id"]] = frozenset(leaves)

    actual_counts = {query_id: len(ids) for query_id, ids in gold.items()}
    if actual_counts != EXPECTED_KEY_GROUP_GOLD_COUNTS:
        raise ValueError(
            f"关键组叶节点展开结果变化：expected={EXPECTED_KEY_GROUP_GOLD_COUNTS}, "
            f"actual={actual_counts}"
        )
    return gold


def build_gold_profiles(
    key_group_gold: dict[str, frozenset[str]],
) -> dict[str, dict[str, frozenset[str]]]:
    """构造基准口径和剔除相邻知识后的严格最小值口径。"""

    minimal = {
        query_id: frozenset(
            leaf_id
            for leaf_id in gold
            if leaf_id not in ADJACENT_NON_MINIMAL_LEAVES.get(query_id, set())
        )
        for query_id, gold in key_group_gold.items()
    }
    return {
        "key_group_expanded": key_group_gold,
        "minimal_semantic": minimal,
    }


def project_selected_nodes(
    selected_ids: list[str],
    known_ids: set[str],
    children: dict[str, list[str]],
    cache: dict[str, frozenset[str]],
) -> tuple[set[str], list[str]]:
    """把输出节点投影为叶节点；未知节点使该次运行失效。"""

    unknown_ids = sorted(set(selected_ids) - known_ids)
    projected: set[str] = set()
    for node_id in dict.fromkeys(selected_ids):
        if node_id in known_ids:
            projected.update(collect_leaf_descendants(node_id, children, cache))
    return projected, unknown_ids


def safe_ratio(numerator: int, denominator: int) -> float:
    """计算比例；分母为零时返回零，避免把空集合解释为完美结果。"""

    return numerator / denominator if denominator else 0.0


def evaluate_attempt(
    result: dict[str, Any],
    gold: frozenset[str],
    known_ids: set[str],
    children: dict[str, list[str]],
    cache: dict[str, frozenset[str]],
) -> dict[str, Any]:
    """计算一次目录导航结果的叶节点覆盖指标。"""

    selected_ids = list(result.get("selected_ids") or [])
    if result["status"] != "valid":
        errors = result.get("validation", {}).get("errors", [])
        return {
            "status": "invalid",
            "errors": errors or [result["status"]],
            "selected_leaf_count": 0,
            "matched_count": 0,
            "missing_count": len(gold),
            "extra_count": 0,
            "recall": 0.0,
            "precision": 0.0,
            "f1": 0.0,
            "coverage_status": "invalid",
            "missing_leaf_ids": sorted(gold),
            "extra_leaf_ids": [],
        }

    projected, unknown_ids = project_selected_nodes(
        selected_ids,
        known_ids,
        children,
        cache,
    )
    if unknown_ids:
        return {
            "status": "invalid",
            "errors": ["unknown_node_ids"],
            "unknown_node_ids": unknown_ids,
            "selected_leaf_count": len(projected),
            "matched_count": 0,
            "missing_count": len(gold),
            "extra_count": len(projected),
            "recall": 0.0,
            "precision": 0.0,
            "f1": 0.0,
            "coverage_status": "invalid",
            "missing_leaf_ids": sorted(gold),
            "extra_leaf_ids": sorted(projected),
        }

    matched = projected & gold
    missing = gold - projected
    extra = projected - gold
    recall = safe_ratio(len(matched), len(gold))
    precision = safe_ratio(len(matched), len(projected))
    f1 = safe_ratio(2 * len(matched), len(projected) + len(gold))
    if len(missing) == 0 and len(extra) == 0:
        coverage_status = "exact"
    elif len(missing) == 0:
        coverage_status = "complete_with_redundancy"
    else:
        coverage_status = "incomplete"
    return {
        "status": "valid",
        "errors": [],
        "selected_leaf_count": len(projected),
        "matched_count": len(matched),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "recall": round(recall, 6),
        "precision": round(precision, 6),
        "f1": round(f1, 6),
        "coverage_status": coverage_status,
        "missing_leaf_ids": sorted(missing),
        "extra_leaf_ids": sorted(extra),
    }


def load_result_runs() -> list[dict[str, Any]]:
    """读取四轮固定的原始导航结果。"""

    runs: list[dict[str, Any]] = []
    for dir_name in RESULT_DIR_NAMES:
        path = RUNTIME_ROOT / dir_name / "results.json"
        runs.append(
            {
                "run_id": dir_name,
                "path": str(path),
                "payload": json.loads(path.read_text(encoding="utf-8")),
            }
        )
    return runs


def summarize(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总四次独立运行，失败运行按召回率和精确率零计入。"""

    return {
        "attempt_count": len(attempts),
        "valid_count": sum(item["status"] == "valid" for item in attempts),
        "invalid_count": sum(item["status"] == "invalid" for item in attempts),
        "complete_count": sum(item["coverage_status"] in {"exact", "complete_with_redundancy"} for item in attempts),
        "exact_count": sum(item["coverage_status"] == "exact" for item in attempts),
        "complete_with_redundancy_count": sum(
            item["coverage_status"] == "complete_with_redundancy" for item in attempts
        ),
        "mean_recall": round(sum(item["recall"] for item in attempts) / len(attempts), 6),
        "mean_precision": round(sum(item["precision"] for item in attempts) / len(attempts), 6),
        "mean_f1": round(sum(item["f1"] for item in attempts) / len(attempts), 6),
        "mean_extra_count": round(sum(item["extra_count"] for item in attempts) / len(attempts), 3),
        "mean_selected_leaf_count": round(
            sum(item["selected_leaf_count"] for item in attempts) / len(attempts), 3
        ),
    }


def main() -> None:
    """执行确定性重算并输出逐次与汇总指标。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    nodes = flatten_nodes(load_forest())
    by_id, children = build_tree_indexes(nodes)
    cache: dict[str, frozenset[str]] = {}
    key_group_gold = build_key_group_gold(children, cache)
    gold_profiles = build_gold_profiles(key_group_gold)
    runs = load_result_runs()

    per_attempt: list[dict[str, Any]] = []
    for profile_name, profile_gold in gold_profiles.items():
        for run in runs:
            payload = run["payload"]
            for mode_name in ("title_mode", "description_mode"):
                for result in payload[mode_name]["results"]:
                    metrics = evaluate_attempt(
                        result,
                        profile_gold[result["query_id"]],
                        set(by_id),
                        children,
                        cache,
                    )
                    per_attempt.append(
                        {
                            "profile": profile_name,
                            "run_id": run["run_id"],
                            "mode": mode_name,
                            "query_id": result["query_id"],
                            **metrics,
                        }
                    )

    summaries: list[dict[str, Any]] = []
    for profile_name in gold_profiles:
        for mode_name in ("title_mode", "description_mode"):
            for query in QUERIES:
                query_id = query["query_id"]
                attempts = [
                    item
                    for item in per_attempt
                    if item["profile"] == profile_name
                    and item["mode"] == mode_name
                    and item["query_id"] == query_id
                ]
                summaries.append(
                    {
                        "profile": profile_name,
                        "mode": mode_name,
                        "query_id": query_id,
                        **summarize(attempts),
                    }
                )

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = RUNTIME_ROOT / f"role_knowledge_forest_query_navigation_leaf_eval_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "gold_profiles": {
            profile_name: {
                query_id: sorted(leaf_ids) for query_id, leaf_ids in profile.items()
            }
            for profile_name, profile in gold_profiles.items()
        },
        "gold_counts": {
            profile_name: {query_id: len(leaf_ids) for query_id, leaf_ids in profile.items()}
            for profile_name, profile in gold_profiles.items()
        },
        "attempts": per_attempt,
        "summaries": summaries,
    }
    output_path = output_dir / "leaf_evaluation.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("GOLD_COUNTS")
    print(json.dumps(report["gold_counts"], ensure_ascii=False, indent=2))
    print("\nSUMMARIES")
    print(
        "profile\tmode\tquery\tvalid\tcomplete\texact\trecall\tprecision\tf1\tmean_extra"
    )
    for item in summaries:
        print(
            f"{item['profile']}\t{item['mode']}\t{item['query_id']}\t"
            f"{item['valid_count']}/4\t{item['complete_count']}/4\t"
            f"{item['exact_count']}/4\t{item['mean_recall']:.4f}\t"
            f"{item['mean_precision']:.4f}\t{item['mean_f1']:.4f}\t"
            f"{item['mean_extra_count']:.2f}"
        )
    print(f"\nOUTPUT {output_path}")


if __name__ == "__main__":
    main()
