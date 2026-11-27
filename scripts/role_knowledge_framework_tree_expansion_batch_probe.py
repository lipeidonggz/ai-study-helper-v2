"""并发展开一级知识框架中除 K08 外的全部节点，并组装完整森林。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
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
    JsonChatResult,
)
from role_knowledge_framework_task_coverage_audit_probe import load_chat_key
from role_knowledge_framework_tree_expansion_probe import (
    FRAMEWORK_RESULT_PATH,
    MAX_ATTEMPTS,
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    assign_path_ids,
    load_system_prompt,
    validate_tree_payload,
)

DEFAULT_CONCURRENCY = 15
EXCLUDED_TARGET_IDS = frozenset({"K08"})
K08_TREE_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_tree_expansion_20261123T112514539832Z"
    / "expanded_tree.json"
)


def load_framework() -> dict[str, Any]:
    """读取已经冻结的 24 个一级节点框架。"""

    return json.loads(FRAMEWORK_RESULT_PATH.read_text(encoding="utf-8"))


def build_user_input(
    framework: dict[str, Any],
    target_node: dict[str, Any],
) -> dict[str, Any]:
    """构造单个目标节点的五字段展开输入。"""

    return {
        "domain_name": "AI Agent",
        "role_name": "系统架构师",
        "framework_scope": framework["framework_scope"],
        "first_level_directory": framework["first_level_nodes"],
        "target_node": target_node,
    }


async def expand_node(
    framework: dict[str, Any],
    target_node: dict[str, Any],
    system_prompt: str,
    client: DeepSeekJsonClient,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """在并发护栏内展开一个一级节点，并保存显式失败信息。"""

    target_node_id = target_node["node_id"]
    async with semaphore:
        print(f"START {target_node_id} {target_node['title']}", flush=True)
        started = time.perf_counter()
        user_input = build_user_input(framework, target_node)
        base_user_prompt = json.dumps(user_input, ensure_ascii=False, indent=2)
        current_user_prompt = base_user_prompt
        attempts: list[dict[str, Any]] = []
        last_payload: dict[str, Any] | None = None

        for attempt_index in range(MAX_ATTEMPTS):
            attempt_started = time.perf_counter()
            result: JsonChatResult | None = None
            summary: dict[str, Any] = {}
            try:
                result = await client.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=current_user_prompt,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                )
                last_payload = result.payload
                errors, summary = validate_tree_payload(result.payload, target_node_id)
            except InductionFailure as error:
                errors = [str(error)]
            except Exception as error:  # noqa: BLE001 - 批量边界必须记录单节点异常
                errors = [f"unexpected:{type(error).__name__}:{error}"]

            attempts.append(
                {
                    "attempt": attempt_index + 1,
                    "elapsed_seconds": round(time.perf_counter() - attempt_started, 3),
                    "validation_errors": errors,
                    "input_tokens": result.input_tokens if result else 0,
                    "output_tokens": result.output_tokens if result else 0,
                    "cache_hit_tokens": result.cache_hit_tokens if result else 0,
                    "cache_miss_tokens": result.cache_miss_tokens if result else 0,
                }
            )
            if result is not None and not errors:
                expanded_children = assign_path_ids(
                    result.payload["children"],
                    target_node_id,
                )
                print(
                    f"DONE  {target_node_id} nodes={summary['descendant_node_count']} "
                    f"attempts={attempt_index + 1}",
                    flush=True,
                )
                return {
                    "status": "valid",
                    "node_id": target_node_id,
                    "title": target_node["title"],
                    "target_node": target_node,
                    "requested_model_id": DEFAULT_MODEL_ID,
                    "actual_model_id": result.actual_model_id,
                    "thinking_enabled": True,
                    "temperature": 0.0,
                    "max_output_tokens": MAX_OUTPUT_TOKENS,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "attempts": attempts,
                    "summary": summary,
                    "raw_children": result.payload["children"],
                    "expanded_children": expanded_children,
                }

            if result is None:
                await asyncio.sleep(min(8, 2 ** (attempt_index + 1)))
            current_user_prompt = (
                base_user_prompt
                + "\n\n上一次输出未通过宿主校验。必须修正后重新输出完整 JSON。错误："
                + json.dumps(errors, ensure_ascii=False)
            )

        print(
            f"FAIL  {target_node_id} attempts={len(attempts)} "
            f"last={attempts[-1]['validation_errors']}",
            flush=True,
        )
        return {
            "status": "failed",
            "node_id": target_node_id,
            "title": target_node["title"],
            "target_node": target_node,
            "requested_model_id": DEFAULT_MODEL_ID,
            "thinking_enabled": True,
            "temperature": 0.0,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "attempts": attempts,
            "last_payload": last_payload,
        }


def aggregate_usage(node_results: list[dict[str, Any]]) -> dict[str, int]:
    """汇总全部节点的调用轮次和 token 用量。"""

    attempts = [
        attempt
        for result in node_results
        for attempt in result.get("attempts", [])
    ]
    return {
        "attempt_count": len(attempts),
        "input_tokens": sum(item["input_tokens"] for item in attempts),
        "output_tokens": sum(item["output_tokens"] for item in attempts),
        "cache_hit_tokens": sum(item["cache_hit_tokens"] for item in attempts),
        "cache_miss_tokens": sum(item["cache_miss_tokens"] for item in attempts),
    }


def assemble_forest(
    framework: dict[str, Any],
    node_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """把 23 个新子树与既有 K08 子树组装成统一森林。"""

    result_by_id = {result["node_id"]: result for result in node_results}
    framework_nodes = {
        node["node_id"]: node for node in framework["first_level_nodes"]
    }
    k08_tree = json.loads(K08_TREE_PATH.read_text(encoding="utf-8"))
    roots: list[dict[str, Any]] = []
    missing_node_ids: list[str] = []

    for node_id in sorted(framework_nodes):
        node = framework_nodes[node_id]
        if node_id == "K08":
            children = k08_tree["children"]
        else:
            result = result_by_id[node_id]
            if result["status"] != "valid":
                missing_node_ids.append(node_id)
                continue
            children = result["expanded_children"]
        roots.append(
            {
                "node_id": node_id,
                "title": node["title"],
                "description": node["description"],
                "exclusions": node["exclusions"],
                "children": children,
            }
        )

    return {
        "status": "complete" if not missing_node_ids else "partial",
        "domain_name": "AI Agent",
        "role_name": "系统架构师",
        "framework_scope": framework["framework_scope"],
        "missing_node_ids": missing_node_ids,
        "roots": roots,
    }


async def run_batch(concurrency: int) -> dict[str, Any]:
    """并发执行 23 个一级节点展开并返回批量报告。"""

    framework = load_framework()
    prompt = load_system_prompt()
    target_nodes = [
        node
        for node in framework["first_level_nodes"]
        if node["node_id"] not in EXCLUDED_TARGET_IDS
    ]
    client = DeepSeekJsonClient(
        load_chat_key(),
        model_id=DEFAULT_MODEL_ID,
        base_url=DEFAULT_BASE_URL,
        timeout_seconds=600.0,
        thinking_enabled=True,
    )
    semaphore = asyncio.Semaphore(concurrency)
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    node_results = await asyncio.gather(
        *(
            expand_node(framework, node, prompt, client, semaphore)
            for node in target_nodes
        )
    )
    node_results = sorted(node_results, key=lambda item: item["node_id"])
    return {
        "status": (
            "complete"
            if all(result["status"] == "valid" for result in node_results)
            else "partial"
        ),
        "prompt_version": PROMPT_VERSION,
        "requested_model_id": DEFAULT_MODEL_ID,
        "thinking_enabled": True,
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "concurrency": concurrency,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "framework_result_path": str(FRAMEWORK_RESULT_PATH),
        "target_node_ids": [node["node_id"] for node in target_nodes],
        "valid_node_ids": [
            result["node_id"] for result in node_results if result["status"] == "valid"
        ],
        "failed_node_ids": [
            result["node_id"] for result in node_results if result["status"] == "failed"
        ],
        "usage": aggregate_usage(node_results),
        "nodes": node_results,
        "forest": assemble_forest(framework, node_results),
    }


def write_outputs(report: dict[str, Any]) -> Path:
    """保存批量报告、单节点子树和合并森林。"""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        BACKEND_ROOT
        / ".runtime"
        / f"role_knowledge_framework_tree_expansion_batch_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    nodes_dir = output_dir / "nodes"
    nodes_dir.mkdir()

    for node_result in report["nodes"]:
        node_dir = nodes_dir / node_result["node_id"]
        node_dir.mkdir()
        (node_dir / "results.json").write_text(
            json.dumps(node_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        if node_result["status"] == "valid":
            (node_dir / "expanded_tree.json").write_text(
                json.dumps(
                    {
                        "parent_node_id": node_result["node_id"],
                        "children": node_result["expanded_children"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    (output_dir / "batch_results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "role_knowledge_forest.json").write_text(
        json.dumps(report["forest"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output_dir


def parse_args() -> argparse.Namespace:
    """解析批量展开并发度。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    args = parser.parse_args()
    if args.concurrency < 1 or args.concurrency > 23:
        parser.error("--concurrency must be between 1 and 23")
    return args


def main() -> None:
    """执行 23 节点批量展开并打印可追踪汇总。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    args = parse_args()
    report = asyncio.run(run_batch(args.concurrency))
    output_dir = write_outputs(report)
    printable = {
        "status": report["status"],
        "elapsed_seconds": report["elapsed_seconds"],
        "valid_count": len(report["valid_node_ids"]),
        "failed_node_ids": report["failed_node_ids"],
        "usage": report["usage"],
        "forest_status": report["forest"]["status"],
        "forest_missing_node_ids": report["forest"]["missing_node_ids"],
        "output_dir": str(output_dir),
        "node_summaries": [
            {
                "node_id": result["node_id"],
                "title": result["title"],
                "status": result["status"],
                "attempt_count": len(result["attempts"]),
                "summary": result.get("summary"),
            }
            for result in report["nodes"]
        ],
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
