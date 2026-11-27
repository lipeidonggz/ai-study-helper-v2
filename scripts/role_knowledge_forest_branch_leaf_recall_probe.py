"""验证一级分支隔离并行选择叶节点的召回率与精确率。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.induction import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
    InductionFailure,
)
from role_knowledge_forest_query_navigation_leaf_eval import (
    build_gold_profiles,
    build_key_group_gold,
    build_tree_indexes,
    safe_ratio,
)
from role_knowledge_forest_query_navigation_probe import (
    FOREST_PATH,
    QUERIES,
    load_forest,
)
from role_knowledge_framework_task_coverage_audit_probe import (
    load_chat_key,
)

EXPERIMENT_DOCUMENT = (
    PROJECT_ROOT
    / "docs"
    / "development"
    / "experiments"
    / "2026-11-23-角色知识框架两阶段分层生成.md"
)
MAX_OUTPUT_TOKENS = 16_384
BRANCH_CONCURRENCY = 24
EXPECTED_FIRST_LEVEL_BRANCHES = 24
EXPECTED_LEAF_COUNTS = {
    "Q1_DATA_LINEAGE": 13,
    "Q2_ISOLATION_BOUNDARIES": 20,
    "Q3_FAILURE_RECOVERY_REGRESSION": 34,
    "Q4_PERMISSION_APPROVAL": 35,
}
SELECTED_QUERY_IDS = ("Q2_ISOLATION_BOUNDARIES",)
LEAF_DESCRIPTION_INCLUDED = False
THINKING_ENABLED = False
QUESTION_OVERRIDE = (
    "除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？"
    "请分别说明各限制什么。"
)


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""

    return datetime.now(UTC)


def json_hash(value: Any) -> str:
    """计算稳定 JSON 表示的 SHA-256。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_system_prompt(include_leaf_description: bool) -> str:
    """读取分支叶节点选择提示词，并按输入模式移除 description 约定。"""

    document = EXPERIMENT_DOCUMENT.read_text(encoding="utf-8")
    start = document.index("#### 10.20.3 系统提示词")
    end = document.index("#### 10.20.4 宿主校验", start)
    section = document[start:end]
    prompts = re.findall(r"```text\r?\n(.*?)\r?\n```", section, re.DOTALL)
    if len(prompts) != 1:
        raise RuntimeError(f"branch_leaf_prompt_count_invalid:{len(prompts)}")
    prompt = prompts[0]
    if include_leaf_description:
        return prompt

    directory_line = (
        "- leaf_directory：该分支下全部叶节点的 id、完整路径、标题和 description。"
    )
    title_only_directory_line = (
        "- leaf_directory：该分支下全部叶节点的 id、完整路径和标题。"
    )
    if directory_line not in prompt:
        raise RuntimeError("branch_leaf_description_input_line_missing")
    prompt = prompt.replace(directory_line, title_only_directory_line)

    description_rule = (
        "4. description 描述该叶节点整体覆盖的知识范围，不是候选证据。要结合 description\n"
        "   判断它能否实际帮助回答问题；不要仅因问题与标题或 description 共享关键词就选择，\n"
        "   也不要把 description 中偶然出现的一个词当成完整选择依据。"
    )
    title_only_rule = (
        "4. 只根据叶节点的完整路径和标题判断它能否实际帮助回答问题；不要仅因问题与叶节点\n"
        "   共享关键词就选择，也不要把路径或标题中偶然出现的一个词当成完整选择依据。"
    )
    if description_rule not in prompt:
        raise RuntimeError("branch_leaf_description_rule_missing")
    return prompt.replace(description_rule, title_only_rule)


def collect_branch_leaves(
    root: dict[str, Any],
    include_description: bool,
) -> list[dict[str, str]]:
    """收集一级分支下的真实叶节点，并生成带标题的完整路径。"""

    leaves: list[dict[str, str]] = []

    def visit(node: dict[str, Any], path_parts: list[str]) -> None:
        current_path = [*path_parts, f"{node['node_id']} {node['title']}"]
        if not node["children"]:
            leaf = {
                "leaf_id": node["node_id"],
                "path": " > ".join(current_path),
                "title": node["title"],
            }
            if include_description:
                leaf["description"] = node["description"]
            leaves.append(leaf)
            return
        for child in node["children"]:
            visit(child, current_path)

    visit(root, [])
    return leaves


def build_branch_inputs(
    forest: dict[str, Any],
    include_description: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """构造 24 个一级分支输入和叶节点归属索引。"""

    branches: list[dict[str, Any]] = []
    leaf_owner: dict[str, dict[str, Any]] = {}
    for root in forest["roots"]:
        leaves = collect_branch_leaves(root, include_description)
        branch_id = root["node_id"]
        branch = {
            "first_level_branch": {
                "node_id": branch_id,
                "title": root["title"],
                "description": root["description"],
                "exclusions": root["exclusions"],
            },
            "leaf_directory": leaves,
        }
        branches.append(branch)
        for leaf in leaves:
            if leaf["leaf_id"] in leaf_owner:
                raise ValueError(f"duplicate_leaf_id:{leaf['leaf_id']}")
            leaf_owner[leaf["leaf_id"]] = {
                "branch_id": branch_id,
                "branch_title": root["title"],
                **leaf,
            }
    if len(branches) != EXPECTED_FIRST_LEVEL_BRANCHES:
        raise ValueError(f"first_level_branch_count_invalid:{len(branches)}")
    return branches, leaf_owner


def validate_branch_payload(
    payload: dict[str, Any],
    branch: dict[str, Any],
) -> tuple[list[str], list[str], list[str], str, list[str]]:
    """严格校验一次分支输出，并把 reason 超长记录为非阻断警告。"""

    errors: list[str] = []
    warnings: list[str] = []
    if set(payload) != {"selected_leaves", "branch_reason"}:
        errors.append("root_fields_invalid")

    branch_reason = payload.get("branch_reason")
    if not isinstance(branch_reason, str) or not branch_reason.strip():
        errors.append("branch_reason_missing")
        branch_reason = ""
    elif len(branch_reason) > 100:
        warnings.append("branch_reason_too_long")

    selected_entries = payload.get("selected_leaves")
    if not isinstance(selected_entries, list):
        errors.append("selected_leaves_not_array")
        selected_entries = []

    allowed_ids = {leaf["leaf_id"] for leaf in branch["leaf_directory"]}
    required_ids: list[str] = []
    optional_ids: list[str] = []
    seen_ids: set[str] = set()
    for index, entry in enumerate(selected_entries):
        if not isinstance(entry, dict):
            errors.append(f"selected_entry_not_object:{index}")
            continue
        if set(entry) != {"leaf_id", "tier", "reason"}:
            errors.append(f"selected_entry_fields_invalid:{index}")
        leaf_id = entry.get("leaf_id")
        tier = entry.get("tier")
        reason = entry.get("reason")
        if not isinstance(leaf_id, str) or not leaf_id.strip():
            errors.append(f"leaf_id_missing:{index}")
            continue
        if leaf_id not in allowed_ids:
            errors.append(f"leaf_id_not_in_current_branch:{leaf_id}")
            continue
        if leaf_id in seen_ids:
            errors.append(f"duplicate_leaf_id:{leaf_id}")
            continue
        seen_ids.add(leaf_id)
        if tier == "required":
            required_ids.append(leaf_id)
        elif tier == "optional":
            optional_ids.append(leaf_id)
        else:
            errors.append(f"leaf_tier_invalid:{leaf_id}")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"leaf_reason_missing:{leaf_id}")
        elif len(reason) > 50:
            warnings.append(f"leaf_reason_too_long:{leaf_id}")

    return errors, required_ids, optional_ids, branch_reason, warnings


class ConcurrencyTracker:
    """记录一次问题运行中的实际活动并发和峰值。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        """记录一个分支请求开始。"""

        self.active += 1
        self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        """记录一个分支请求结束。"""

        self.active -= 1


async def run_branch(
    client: DeepSeekJsonClient,
    system_prompt: str,
    question: str,
    branch: dict[str, Any],
    semaphore: asyncio.Semaphore,
    tracker: ConcurrencyTracker,
) -> dict[str, Any]:
    """执行并严格校验一个一级分支的叶节点选择。"""

    branch_id = branch["first_level_branch"]["node_id"]
    request_payload = {"question": question, **branch}
    started_at = utc_now()
    started = time.perf_counter()
    async with semaphore:
        tracker.enter()
        try:
            result = await client.complete_json(
                system_prompt=system_prompt,
                user_prompt=json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            errors, required_ids, optional_ids, branch_reason, warnings = (
                validate_branch_payload(
                    result.payload,
                    branch,
                )
            )
            status = "valid" if not errors else "invalid"
            return {
                "branch_id": branch_id,
                "branch_title": branch["first_level_branch"]["title"],
                "status": status,
                "errors": errors,
                "warnings": warnings,
                "input_payload": request_payload,
                "input_leaf_count": len(branch["leaf_directory"]),
                "response_payload": result.payload,
                "required_leaf_ids": required_ids,
                "optional_leaf_ids": optional_ids,
                "selected_leaf_ids": [*required_ids, *optional_ids],
                "branch_reason": branch_reason,
                "actual_model_id": result.actual_model_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_hit_tokens": result.cache_hit_tokens,
                "cache_miss_tokens": result.cache_miss_tokens,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "started_at": started_at.isoformat(),
            }
        except InductionFailure as error:
            return {
                "branch_id": branch_id,
                "branch_title": branch["first_level_branch"]["title"],
                "status": "invalid",
                "errors": [str(error)],
                "warnings": [],
                "input_payload": request_payload,
                "input_leaf_count": len(branch["leaf_directory"]),
                "response_payload": None,
                "selected_leaf_ids": [],
                "required_leaf_ids": [],
                "optional_leaf_ids": [],
                "branch_reason": "",
                "actual_model_id": None,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "started_at": started_at.isoformat(),
            }
        finally:
            tracker.leave()


def evaluate_selection(
    selected_ids: set[str],
    gold_ids: frozenset[str],
) -> dict[str, Any]:
    """计算叶节点级召回率、精确率、F1 和多余项。"""

    matched = selected_ids & gold_ids
    missing = gold_ids - selected_ids
    extra = selected_ids - gold_ids
    recall = safe_ratio(len(matched), len(gold_ids))
    precision = safe_ratio(len(matched), len(selected_ids))
    f1 = safe_ratio(2 * len(matched), len(selected_ids) + len(gold_ids))
    if not missing and not extra:
        coverage_status = "exact"
    elif not missing:
        coverage_status = "complete_with_redundancy"
    else:
        coverage_status = "incomplete"
    return {
        "selected_leaf_count": len(selected_ids),
        "gold_leaf_count": len(gold_ids),
        "matched_count": len(matched),
        "missing_count": len(missing),
        "extra_count": len(extra),
        "recall": round(recall, 6),
        "precision": round(precision, 6),
        "f1": round(f1, 6),
        "coverage_status": coverage_status,
        "matched_leaf_ids": sorted(matched),
        "missing_leaf_ids": sorted(missing),
        "extra_leaf_ids": sorted(extra),
    }


def summarize_run(
    query: dict[str, Any],
    branches: list[dict[str, Any]],
    gold_ids: frozenset[str],
    leaf_owner: dict[str, dict[str, Any]],
    elapsed_seconds: float,
    max_active: int,
) -> dict[str, Any]:
    """汇总一个问题下 24 个分支调用及其问题级联盟结果。"""

    valid_branches = [item for item in branches if item["status"] == "valid"]
    failed_branches = [item for item in branches if item["status"] != "valid"]
    required_ids = {
        leaf_id for item in valid_branches for leaf_id in item["required_leaf_ids"]
    }
    selected_ids = {
        leaf_id for item in valid_branches for leaf_id in item["selected_leaf_ids"]
    }
    metrics_by_tier = {
        "required_only": evaluate_selection(required_ids, gold_ids),
        "required_plus_optional": evaluate_selection(selected_ids, gold_ids),
    }
    branch_by_leaf: dict[str, list[str]] = defaultdict(list)
    for item in valid_branches:
        for leaf_id in item["selected_leaf_ids"]:
            branch_by_leaf[leaf_id].append(item["branch_id"])
    return {
        "query_id": query["query_id"],
        "question": query["question"],
        "status": (
            "complete" if not failed_branches else "incomplete_due_to_branch_failure"
        ),
        "branch_call_count": len(branches),
        "valid_branch_count": len(valid_branches),
        "invalid_branch_count": len(failed_branches),
        "invalid_branch_ids": sorted(item["branch_id"] for item in failed_branches),
        "warning_branch_count": sum(bool(item["warnings"]) for item in branches),
        "actual_max_branch_concurrency": max_active,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "input_tokens": sum(item["input_tokens"] for item in branches),
        "output_tokens": sum(item["output_tokens"] for item in branches),
        "cache_hit_tokens": sum(item["cache_hit_tokens"] for item in branches),
        "cache_miss_tokens": sum(item["cache_miss_tokens"] for item in branches),
        "returned_leaf_count": len(selected_ids),
        "returned_required_leaf_count": len(required_ids),
        "returned_optional_leaf_count": len(selected_ids - required_ids),
        "selected_leaf_ids": sorted(selected_ids),
        "metrics": metrics_by_tier["required_only"],
        "metrics_by_tier": metrics_by_tier,
        "gold_routes": {leaf_id: leaf_owner[leaf_id] for leaf_id in sorted(gold_ids)},
        "gold_recalled_by_branches": {
            leaf_id: branch_by_leaf.get(leaf_id, []) for leaf_id in sorted(gold_ids)
        },
        "branches": sorted(branches, key=lambda item: item["branch_id"]),
    }


async def run_question(
    client: DeepSeekJsonClient,
    system_prompt: str,
    query: dict[str, Any],
    branches: list[dict[str, Any]],
    gold_ids: frozenset[str],
    leaf_owner: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """并行执行一个问题的全部一级分支调用。"""

    tracker = ConcurrencyTracker()
    semaphore = asyncio.Semaphore(BRANCH_CONCURRENCY)
    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            run_branch(
                client,
                system_prompt,
                query["question"],
                branch,
                semaphore,
                tracker,
            )
            for branch in branches
        )
    )
    return summarize_run(
        query,
        results,
        gold_ids,
        leaf_owner,
        time.perf_counter() - started,
        tracker.max_active,
    )


def write_results(
    result_path: Path,
    experiment: dict[str, Any],
) -> None:
    """原子写入当前实验进度，避免中断后丢失已完成问题。"""

    temp_path = result_path.with_suffix(".json.tmp")
    temp_path.write_text(
        json.dumps(experiment, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(result_path)


async def run_experiment() -> None:
    """执行本轮选定的分支隔离选择并记录完整诊断。"""

    started_at = utc_now()
    stamp = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    result_dir = (
        BACKEND_ROOT / ".runtime" / f"role_knowledge_forest_branch_leaf_recall_{stamp}"
    )
    result_dir.mkdir(parents=True, exist_ok=False)
    result_path = result_dir / "results.json"

    forest = load_forest()
    branches, leaf_owner = build_branch_inputs(
        forest,
        include_description=LEAF_DESCRIPTION_INCLUDED,
    )
    nodes = []

    def flatten(node: dict[str, Any], parent_id: str | None) -> None:
        nodes.append({**node, "parent_id": parent_id})
        for child in node["children"]:
            flatten(child, node["node_id"])

    for root in forest["roots"]:
        flatten(root, None)
    by_id, children = build_tree_indexes(nodes)
    leaf_cache: dict[str, frozenset[str]] = {}
    key_group_gold = build_key_group_gold(children, leaf_cache)
    strict_gold = build_gold_profiles(key_group_gold)["minimal_semantic"]
    actual_gold_counts = {query_id: len(ids) for query_id, ids in strict_gold.items()}
    if actual_gold_counts != EXPECTED_LEAF_COUNTS:
        raise ValueError(f"strict_gold_count_mismatch:{actual_gold_counts}")
    unknown_gold_ids = sorted(set().union(*strict_gold.values()) - set(by_id))
    if unknown_gold_ids:
        raise ValueError(f"gold_ids_missing_from_forest:{unknown_gold_ids}")

    system_prompt = load_system_prompt(LEAF_DESCRIPTION_INCLUDED)
    queries_to_run = [
        query for query in QUERIES if query["query_id"] in SELECTED_QUERY_IDS
    ]
    if len(queries_to_run) != len(SELECTED_QUERY_IDS):
        raise ValueError("selected_query_missing")
    if QUESTION_OVERRIDE:
        queries_to_run = [
            {**query, "question": QUESTION_OVERRIDE} for query in queries_to_run
        ]
    experiment: dict[str, Any] = {
        "experiment": "role_knowledge_forest_branch_leaf_recall",
        "status": "running",
        "started_at": started_at.isoformat(),
        "result_path": str(result_path),
        "forest_path": str(FOREST_PATH),
        "forest_sha256": hashlib.sha256(FOREST_PATH.read_bytes()).hexdigest(),
        "system_prompt": system_prompt,
        "system_prompt_sha256": hashlib.sha256(
            system_prompt.encode("utf-8")
        ).hexdigest(),
        "model_id": DEFAULT_MODEL_ID,
        "base_url": DEFAULT_BASE_URL,
        "thinking_enabled": THINKING_ENABLED,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "configured_branch_concurrency": BRANCH_CONCURRENCY,
        "first_level_branch_count": len(branches),
        "leaf_count": len(leaf_owner),
        "leaf_description_included": LEAF_DESCRIPTION_INCLUDED,
        "leaf_exclusions_included": False,
        "strict_gold_counts": actual_gold_counts,
        "selected_query_ids": list(SELECTED_QUERY_IDS),
        "question_override": QUESTION_OVERRIDE,
        "query_order": [query["query_id"] for query in queries_to_run],
        "queries": [],
    }
    write_results(result_path, experiment)

    client = DeepSeekJsonClient(
        load_chat_key(),
        model_id=DEFAULT_MODEL_ID,
        base_url=DEFAULT_BASE_URL,
        timeout_seconds=600.0,
        thinking_enabled=THINKING_ENABLED,
    )
    print(
        f"START branches={len(branches)} leaves={len(leaf_owner)} "
        f"questions={len(queries_to_run)} concurrency={BRANCH_CONCURRENCY}",
        flush=True,
    )
    for query in queries_to_run:
        query_id = query["query_id"]
        print(f"START {query_id}", flush=True)
        summary = await run_question(
            client,
            system_prompt,
            query,
            branches,
            strict_gold[query_id],
            leaf_owner,
        )
        experiment["queries"].append(summary)
        write_results(result_path, experiment)
        required_metrics = summary["metrics_by_tier"]["required_only"]
        union_metrics = summary["metrics_by_tier"]["required_plus_optional"]
        print(
            f"DONE {query_id} status={summary['status']} "
            f"valid={summary['valid_branch_count']}/24 "
            f"max_active={summary['actual_max_branch_concurrency']} "
            f"required_recall={required_metrics['recall']:.4f} "
            f"required_precision={required_metrics['precision']:.4f} "
            f"union_recall={union_metrics['recall']:.4f} "
            f"union_precision={union_metrics['precision']:.4f} "
            f"elapsed={summary['elapsed_seconds']:.2f}s",
            flush=True,
        )

    experiment["status"] = (
        "complete"
        if all(query["status"] == "complete" for query in experiment["queries"])
        else "incomplete_due_to_branch_failure"
    )
    experiment["finished_at"] = utc_now().isoformat()
    experiment["summary"] = summarize_experiment(experiment["queries"])
    write_results(result_path, experiment)
    print(f"RESULT {result_path}", flush=True)
    print(
        f"SUMMARY status={experiment['status']} "
        f"required_recall={experiment['summary']['required']['mean_recall']:.4f} "
        f"required_precision={experiment['summary']['required']['mean_precision']:.4f} "
        f"union_recall={experiment['summary']['union']['mean_recall']:.4f} "
        f"union_precision={experiment['summary']['union']['mean_precision']:.4f}",
        flush=True,
    )


def summarize_experiment(queries: list[dict[str, Any]]) -> dict[str, Any]:
    """分别汇总 required-only 与 required-plus-optional 的问题级结果。"""

    if not queries:
        return {
            "question_count": 0,
            "complete_question_count": 0,
            "required": {
                "mean_recall": 0.0,
                "mean_precision": 0.0,
                "mean_f1": 0.0,
                "mean_extra_count": 0.0,
            },
            "union": {
                "mean_recall": 0.0,
                "mean_precision": 0.0,
                "mean_f1": 0.0,
                "mean_extra_count": 0.0,
            },
        }

    def aggregate(tier_key: str) -> dict[str, float]:
        metrics = [query["metrics_by_tier"][tier_key] for query in queries]
        return {
            "mean_recall": round(
                sum(metric["recall"] for metric in metrics) / len(metrics),
                6,
            ),
            "mean_precision": round(
                sum(metric["precision"] for metric in metrics) / len(metrics),
                6,
            ),
            "mean_f1": round(
                sum(metric["f1"] for metric in metrics) / len(metrics),
                6,
            ),
            "mean_extra_count": round(
                sum(metric["extra_count"] for metric in metrics) / len(metrics),
                3,
            ),
        }

    return {
        "question_count": len(queries),
        "complete_question_count": sum(
            query["status"] == "complete" for query in queries
        ),
        "required": aggregate("required_only"),
        "union": aggregate("required_plus_optional"),
    }


def main() -> None:
    """运行分支隔离叶节点召回实验。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    asyncio.run(run_experiment())


if __name__ == "__main__":
    main()
