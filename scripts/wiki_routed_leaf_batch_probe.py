"""按一级筛选结果组织跨树叶节点批次，并汇总逐叶支持结果。"""

# 探针必须先设置 backend 与 scripts 搜索路径，之后才能导入项目模块。
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_ROOT.parent / "backend"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import (
    DEEPSEEK_BASE_URL,
    ChatProviderProfile,
    DeepSeekChatProvider,
)
from app.storage.credentials import CHAT_API_KEY_USERNAME, WindowsCredentialStore
from wiki_leaf_batch_support_probe import (
    BATCH_SYSTEM_PROMPT,
    MODEL_ID,
    run_one,
)
from wiki_leaf_support_probe import (
    FOREST_PATH,
    MAX_OUTPUT_TOKENS,
    SUPPORT_TYPES,
    ProbeSpec,
    load_block,
    load_json,
    load_leaves,
)
from wiki_root_tree_batch_probe import (
    KNOWN_GOLD_BY_ROOT,
    collect_leaf_ids,
    find_node,
)

DEFAULT_ROOT_RESULT_PATH = (
    BACKEND_ROOT / ".runtime" / "root_exclusion_probe" / "20261124T124229827499Z" / "results.json"
)
DEFAULT_ROOT_RUN_INDEX = 1
DEFAULT_BATCH_SIZE = 10
DEFAULT_COVERAGE_PASSES = 1
DEFAULT_MAX_CONCURRENCY = 4
OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "routed_leaf_batch_probe"


def rotate(items: list[Any], offset: int) -> list[Any]:
    """按偏移旋转列表，用于让不同覆盖轮次产生不同的跨树组合。"""

    if not items:
        return []
    normalized = offset % len(items)
    return [*items[normalized:], *items[:normalized]]


def load_retained_root_ids(path: Path, run_index: int) -> tuple[list[str], dict[str, Any]]:
    """读取一级筛选产物的指定运行，并返回保留一级树编号。"""

    artifact = load_json(path)
    results = artifact.get("results")
    if not isinstance(results, list):
        raise TypeError("root result artifact has no results array")
    selected = [item for item in results if item.get("run_index") == run_index]
    if len(selected) != 1:
        raise RuntimeError(f"root result run_index {run_index} matched {len(selected)} runs")
    validation = selected[0].get("validation") or {}
    if validation.get("valid") is not True:
        raise RuntimeError(f"root result run {run_index} is not structurally valid")
    retained_ids = validation.get("retained_ids")
    if not isinstance(retained_ids, list) or not all(
        isinstance(root_id, str) for root_id in retained_ids
    ):
        raise RuntimeError("root result retained_ids is invalid")
    if len(retained_ids) != len(set(retained_ids)):
        raise RuntimeError("root result retained_ids contains duplicates")
    return sorted(retained_ids), {
        "path": str(path),
        "root_run_index": run_index,
        "system_prompt_sha256": artifact.get("system_prompt_sha256"),
        "detail_mode": artifact.get("detail_mode"),
    }


def build_root_strand(
    root_id: str,
    forest: dict[str, Any],
    leaves: dict[str, dict[str, Any]],
    *,
    rotation: int,
) -> list[str]:
    """把一个一级树的叶节点按父分支轮转，避免同父节点相邻竞争。"""

    root_node = find_node(forest, root_id)
    leaf_ids = collect_leaf_ids(root_node)
    branch_groups: dict[tuple[str, ...], list[str]] = {}
    for leaf_id in leaf_ids:
        leaf = leaves.get(leaf_id)
        if leaf is None:
            raise RuntimeError(f"leaf missing from flattened forest: {leaf_id}")
        branch_key = tuple(leaf["path"][:-1])
        branch_groups.setdefault(branch_key, []).append(leaf_id)

    branches = [branch_groups[key] for key in sorted(branch_groups)]
    rotated_branches = rotate(branches, rotation)
    max_branch_size = max((len(branch) for branch in rotated_branches), default=0)
    strand: list[str] = []
    for leaf_offset in range(max_branch_size):
        for branch in rotated_branches:
            if leaf_offset >= len(branch):
                continue
            root_branch = rotate(branch, rotation)
            strand.append(root_branch[leaf_offset])
    return strand


def balanced_batch_sizes(total_size: int, target_size: int) -> list[int]:
    """计算接近目标大小的均衡批次容量，避免产生小尾批。"""

    if total_size <= 0:
        return []
    batch_count = (total_size + target_size - 1) // target_size
    base_size, larger_count = divmod(total_size, batch_count)
    return [
        base_size + (1 if batch_index < larger_count else 0) for batch_index in range(batch_count)
    ]


def allocate_mixed_batches(
    root_ids: list[str],
    strands: dict[str, list[str]],
    *,
    batch_sizes: list[int],
    rotation: int,
) -> list[list[str]]:
    """把每棵树的叶节点均匀撒入全部批次，同时优先跨树且跨父分支组合。"""

    batch_count = len(batch_sizes)
    remaining = list(batch_sizes)
    batches: list[list[str]] = [[] for _ in range(batch_count)]
    root_counts = {root_id: [0] * batch_count for root_id in root_ids}
    rotated_roots = rotate(sorted(root_ids), rotation)
    for root_index, root_id in enumerate(rotated_roots):
        candidate_order = rotate(list(range(batch_count)), rotation + root_index)
        candidate_rank = {batch_index: rank for rank, batch_index in enumerate(candidate_order)}
        for leaf_id in strands[root_id]:
            available = [
                batch_index for batch_index in candidate_order if remaining[batch_index] > 0
            ]
            if not available:
                raise RuntimeError("mixed batch allocator exhausted capacity")
            batch_index = min(
                available,
                key=lambda candidate: (
                    root_counts[root_id][candidate],
                    -remaining[candidate],
                    candidate_rank[candidate],
                ),
            )
            batches[batch_index].append(leaf_id)
            root_counts[root_id][batch_index] += 1
            remaining[batch_index] -= 1

    if any(remaining):
        raise RuntimeError(f"mixed batch allocator left capacity unused: {remaining}")
    return batches


def build_batch_plan(
    root_ids: list[str],
    forest: dict[str, Any],
    leaves: dict[str, dict[str, Any]],
    *,
    batch_size: int,
    coverage_passes: int,
) -> list[dict[str, Any]]:
    """生成多轮覆盖计划；每轮每个叶节点恰好出现一次，跨轮重新混排。"""

    selected_leaf_ids = {
        leaf_id for root_id in root_ids for leaf_id in collect_leaf_ids(find_node(forest, root_id))
    }
    batches: list[dict[str, Any]] = []
    batch_sizes = balanced_batch_sizes(len(selected_leaf_ids), batch_size)
    for pass_index in range(coverage_passes):
        strands = {
            root_id: build_root_strand(
                root_id,
                forest,
                leaves,
                rotation=pass_index + root_index,
            )
            for root_index, root_id in enumerate(sorted(root_ids))
        }
        allocated = allocate_mixed_batches(
            sorted(root_ids),
            strands,
            batch_sizes=batch_sizes,
            rotation=pass_index,
        )
        allocated_leaf_ids = {leaf_id for batch_leaf_ids in allocated for leaf_id in batch_leaf_ids}
        if allocated_leaf_ids != selected_leaf_ids or sum(len(batch) for batch in allocated) != len(
            selected_leaf_ids
        ):
            raise RuntimeError("coverage pass does not contain every selected leaf once")

        for batch_index, allocated_leaf_ids in enumerate(allocated, start=1):
            leaf_ids = rotate(
                allocated_leaf_ids,
                pass_index + batch_index,
            )
            batch_root_ids = sorted({leaf_id.split(".", 1)[0] for leaf_id in leaf_ids})
            branch_paths = sorted(
                {" > ".join(leaves[leaf_id]["path"][:-1]) for leaf_id in leaf_ids}
            )
            batches.append(
                {
                    "batch_id": f"P{pass_index + 1:02d}-B{batch_index:03d}",
                    "coverage_pass": pass_index + 1,
                    "batch_index": batch_index,
                    "leaf_ids": leaf_ids,
                    "root_ids": batch_root_ids,
                    "branch_paths": branch_paths,
                    "leaf_count": len(leaf_ids),
                    "root_count": len(batch_root_ids),
                    "branch_count": len(branch_paths),
                }
            )
    return batches


def expected_supported(leaf_id: str) -> bool:
    """读取 b00008 全叶人工审计结论；未列出的叶节点按已审核负例处理。"""

    root_id = leaf_id.split(".", 1)[0]
    return KNOWN_GOLD_BY_ROOT.get(root_id, {}).get(leaf_id, False)


def build_probes(leaf_ids: list[str]) -> tuple[ProbeSpec, ...]:
    """为当前批次构造只供宿主校验使用的探针规格，金标准不进入提示词。"""

    probes: list[ProbeSpec] = []
    for leaf_id in leaf_ids:
        is_supported = expected_supported(leaf_id)
        probes.append(
            ProbeSpec(
                probe_id=leaf_id,
                leaf_id=leaf_id,
                expected_supported=is_supported,
                required_types=(),
                allowed_extra_types=SUPPORT_TYPES if is_supported else (),
                rationale="跨树叶批量诊断，仅用于宿主比较已审核结果。",
            )
        )
    return tuple(probes)


def collect_supported_leaf_ids(result: dict[str, Any]) -> list[str]:
    """只接受结构合法且证据自证成立的支持决定。"""

    validation = result.get("validation") or {}
    parsed = validation.get("parsed") or {}
    decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
    leaf_validations = validation.get("leaves") or {}
    if not isinstance(decisions, list):
        return []
    supported_ids: list[str] = []
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("is_supported") is not True:
            continue
        leaf_id = decision.get("leaf_id")
        if not isinstance(leaf_id, str):
            continue
        if (leaf_validations.get(leaf_id) or {}).get("valid") is True:
            supported_ids.append(leaf_id)
    return supported_ids


def summarize(
    results: list[dict[str, Any]],
    batches: list[dict[str, Any]],
    *,
    selected_leaf_ids: set[str],
) -> dict[str, Any]:
    """按叶节点汇总跨批次支持并集，并计算相对人工审计的召回与精确率。"""

    support_counts = {leaf_id: 0 for leaf_id in selected_leaf_ids}
    support_events: list[dict[str, Any]] = []
    pass_supports: dict[int, set[str]] = {}
    valid_batches = 0
    parsed_batches = 0
    for result in results:
        validation = result.get("validation") or {}
        if validation.get("parse_ok") is True:
            parsed_batches += 1
        if validation.get("valid") is True:
            valid_batches += 1
        supported_ids = collect_supported_leaf_ids(result)
        pass_index = result["coverage_pass"]
        pass_supports.setdefault(pass_index, set()).update(supported_ids)
        for leaf_id in supported_ids:
            support_counts[leaf_id] += 1
            support_events.append(
                {
                    "coverage_pass": pass_index,
                    "batch_id": result["batch_id"],
                    "leaf_id": leaf_id,
                }
            )

    union_supported_ids = {leaf_id for leaf_id, count in support_counts.items() if count > 0}
    positive_ids = {leaf_id for leaf_id in selected_leaf_ids if expected_supported(leaf_id)}
    negative_ids = selected_leaf_ids - positive_ids
    recalled_positive_ids = positive_ids & union_supported_ids
    false_positive_ids = negative_ids & union_supported_ids
    precision_denominator = len(union_supported_ids)
    return {
        "batch_count": len(batches),
        "completed_batches": sum("validation" in result for result in results),
        "parsed_batches": parsed_batches,
        "valid_batches": valid_batches,
        "selected_leaf_count": len(selected_leaf_ids),
        "positive_count": len(positive_ids),
        "negative_count": len(negative_ids),
        "recalled_positive_count": len(recalled_positive_ids),
        "recall": (len(recalled_positive_ids) / len(positive_ids) if positive_ids else 1.0),
        "union_supported_count": len(union_supported_ids),
        "false_positive_count": len(false_positive_ids),
        "precision": (
            len(recalled_positive_ids) / precision_denominator if precision_denominator else 1.0
        ),
        "recalled_positive_ids": sorted(recalled_positive_ids),
        "missed_positive_ids": sorted(positive_ids - union_supported_ids),
        "false_positive_ids": sorted(false_positive_ids),
        "pass_supported_ids": {
            str(pass_index): sorted(leaf_ids)
            for pass_index, leaf_ids in sorted(pass_supports.items())
        },
        "support_counts": support_counts,
        "support_events": support_events,
        "input_tokens": sum(result.get("input_tokens", 0) for result in results),
        "output_tokens": sum(result.get("output_tokens", 0) for result in results),
    }


def parse_args() -> argparse.Namespace:
    """解析一级筛选来源、批大小、覆盖轮次和并发配置。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root-result-path",
        type=Path,
        default=DEFAULT_ROOT_RESULT_PATH,
    )
    parser.add_argument("--root-run-index", type=int, default=DEFAULT_ROOT_RUN_INDEX)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--coverage-passes",
        type=int,
        default=DEFAULT_COVERAGE_PASSES,
        help="完整覆盖轮数；每轮每个叶节点出现一次，跨轮重新混排。",
    )
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只生成并保存批次计划，不读取凭证、不调用模型。",
    )
    args = parser.parse_args()
    if args.root_run_index <= 0:
        parser.error("--root-run-index must be positive")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.coverage_passes <= 0:
        parser.error("--coverage-passes must be positive")
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    return args


async def main() -> None:
    """生成跨树叶批次计划，执行当前阶段允许的调用并保存完整证据。"""

    args = parse_args()
    root_ids, root_source = load_retained_root_ids(
        args.root_result_path,
        args.root_run_index,
    )
    forest = load_json(FOREST_PATH)
    leaves = load_leaves(forest)
    block = load_block()
    batches = build_batch_plan(
        root_ids,
        forest,
        leaves,
        batch_size=args.batch_size,
        coverage_passes=args.coverage_passes,
    )
    selected_leaf_ids = {leaf_id for batch in batches for leaf_id in batch["leaf_ids"]}
    if len(selected_leaf_ids) != sum(
        len(collect_leaf_ids(find_node(forest, root_id))) for root_id in root_ids
    ):
        raise RuntimeError("batch plan does not cover every selected root leaf")

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    artifact: dict[str, Any] = {
        "experiment": "root-routed cross-tree leaf batch attachment",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "dry_run": args.dry_run,
        "forest_path": str(FOREST_PATH),
        "block_id": block["block_id"],
        "model": MODEL_ID,
        "thinking": "enabled" if args.thinking else "disabled",
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "root_source": root_source,
        "retained_root_ids": root_ids,
        "batch_size": args.batch_size,
        "coverage_passes": args.coverage_passes,
        "max_concurrency": args.max_concurrency,
        "system_prompt": BATCH_SYSTEM_PROMPT,
        "system_prompt_sha256": hashlib.sha256(BATCH_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "batches": batches,
    }

    if args.dry_run:
        artifact["summary"] = {
            "batch_count": len(batches),
            "selected_leaf_count": len(selected_leaf_ids),
            "positive_count": sum(expected_supported(leaf_id) for leaf_id in selected_leaf_ids),
        }
    else:
        api_key = WindowsCredentialStore().get(CHAT_API_KEY_USERNAME)
        if not api_key:
            raise RuntimeError("chat API key is not configured in Windows Credential Manager")
        provider = DeepSeekChatProvider(
            api_key,
            profile=ChatProviderProfile(
                provider="deepseek",
                model_id=MODEL_ID,
                base_url=DEEPSEEK_BASE_URL,
                temperature=0.0,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                thinking_enabled=args.thinking,
            ),
        )
        semaphore = asyncio.Semaphore(args.max_concurrency)

        async def execute(batch: dict[str, Any], task_index: int) -> dict[str, Any]:
            probes = build_probes(batch["leaf_ids"])
            result = await run_one(
                provider,
                semaphore,
                task_index,
                block,
                probes,
                leaves,
            )
            return {
                **result,
                "coverage_pass": batch["coverage_pass"],
                "batch_id": batch["batch_id"],
            }

        results = await asyncio.gather(
            *(execute(batch, index) for index, batch in enumerate(batches, start=1))
        )
        artifact["summary"] = summarize(
            results,
            batches,
            selected_leaf_ids=selected_leaf_ids,
        )
        artifact["results"] = results

    result_path = run_dir / "results.json"
    result_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"run_dir": str(run_dir), "summary": artifact["summary"]},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
