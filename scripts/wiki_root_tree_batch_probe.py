"""把指定一级知识子树的所有叶节点作为一个批次，诊断整树输入的判定行为。"""

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
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    build_messages,
    run_one,
)
from wiki_leaf_support_probe import (
    FOREST_PATH,
    SUPPORT_TYPES,
    ProbeSpec,
    load_block,
    load_json,
    load_leaves,
)

DEFAULT_ROOT_ID = "K18"
DEFAULT_RUN_COUNT = 1
MAX_CONCURRENCY = 4
OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "root_tree_batch_probe"

# 仅用于区分已人工确认的结果和未审核结果，不向模型发送。
KNOWN_GOLD_BY_ROOT = {
    "K01": {
        "K01.02.02": True,
        # 该叶节点明确排除具体安全控制实现，b00008 中的沙箱与审批只能作为相邻知识实例。
        "K01.02.03": False,
        "K01.07.03": False,
    },
    "K04": {
        "K04.06.02": False,
        "K04.06.03": False,
    },
    "K08": {
        "K08.04.05": False,
        "K08.05.01": True,
        "K08.05.03": True,
        "K08.05.04": False,
    },
    "K11": {
        "K11.01.03": True,
        "K11.06.04": False,
    },
    "K16": {
        "K16.03.01": True,
        "K16.03.02": True,
        "K16.03.03": True,
        "K16.05.01": False,
        "K16.06.01": True,
        "K16.06.02": False,
        "K16.07.01": True,
        "K16.07.02": True,
    },
    "K18": {
        "K18.01.01": True,
        "K18.01.02": True,
        "K18.01.03": False,
        "K18.02.01": False,
        "K18.02.02": False,
        "K18.03.01": True,
        "K18.03.02": True,
        "K18.03.03": True,
        "K18.04.01": True,
        "K18.04.02": False,
        "K18.04.03": False,
        "K18.06.01": False,
        "K18.06.02": False,
        "K18.07.01": False,
    },
    "K24": {
        "K24.02.02": False,
        "K24.04.01": True,
    },
}


def find_node(forest: dict[str, Any], node_id: str) -> dict[str, Any]:
    """在知识森林中按编号查找节点。"""

    def walk(nodes: list[dict[str, Any]]) -> dict[str, Any] | None:
        for node in nodes:
            if node["node_id"] == node_id:
                return node
            found = walk(node.get("children") or [])
            if found is not None:
                return found
        return None

    node = walk(forest["roots"])
    if node is None:
        raise RuntimeError(f"root node not found: {node_id}")
    return node


def collect_leaf_ids(node: dict[str, Any]) -> list[str]:
    """按树中的原始顺序收集叶节点编号。"""

    leaf_ids: list[str] = []

    def walk(current: dict[str, Any]) -> None:
        children = current.get("children") or []
        if not children:
            leaf_ids.append(current["node_id"])
            return
        for child in children:
            walk(child)

    walk(node)
    return leaf_ids


def summarize(
    results: list[dict[str, Any]],
    *,
    leaf_ids: list[str],
    leaves: dict[str, dict[str, Any]],
    known_gold: dict[str, bool],
) -> dict[str, Any]:
    """汇总每次调用中的支持项，并只对已人工确认的节点比较金标准。"""

    run_summaries: list[dict[str, Any]] = []
    support_counts = {leaf_id: 0 for leaf_id in leaf_ids}
    for result in results:
        validation = result.get("validation") or {}
        parsed = validation.get("parsed") or {}
        decisions = parsed.get("decisions") if isinstance(parsed, dict) else None
        supported_ids: list[str] = []
        if isinstance(decisions, list):
            supported_ids = [
                decision["leaf_id"]
                for decision in decisions
                if isinstance(decision, dict)
                and decision.get("leaf_id") in support_counts
                and decision.get("is_supported") is True
            ]
        for leaf_id in supported_ids:
            support_counts[leaf_id] += 1
        run_summaries.append(
            {
                "run_index": result["run_index"],
                "batch_valid": validation.get("valid") is True,
                "supported_count": len(supported_ids),
                "supported_ids": supported_ids,
            }
        )

    known_results = []
    for leaf_id in leaf_ids:
        expected = known_gold.get(leaf_id)
        if expected is None:
            continue
        actual_by_run = []
        for result in results:
            validation = result.get("validation") or {}
            parsed = validation.get("parsed") or {}
            decisions = parsed.get("decisions") if isinstance(parsed, dict) else []
            decision = next(
                (
                    item
                    for item in decisions
                    if isinstance(item, dict) and item.get("leaf_id") == leaf_id
                ),
                None,
            )
            actual_by_run.append(decision.get("is_supported") if decision else None)
        known_results.append(
            {
                "leaf_id": leaf_id,
                "title": leaves[leaf_id]["title"],
                "expected_supported": expected,
                "actual_by_run": actual_by_run,
                "match_count": sum(actual is expected for actual in actual_by_run),
            }
        )

    return {
        "calls": len(results),
        "batch_valid": sum(item["batch_valid"] for item in run_summaries),
        "run_summaries": run_summaries,
        "support_counts": support_counts,
        "known_gold": known_results,
    }


def parse_args() -> argparse.Namespace:
    """解析根节点、回放次数和并发数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-id", default=DEFAULT_ROOT_ID)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUN_COUNT)
    parser.add_argument("--max-concurrency", type=int, default=MAX_CONCURRENCY)
    parser.add_argument("--thinking", action="store_true")
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be positive")
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    return args


async def main() -> None:
    """加载指定根节点的全部叶节点，执行整树批量判定并保存原始证据。"""

    args = parse_args()
    forest = load_json(FOREST_PATH)
    leaves = load_leaves(forest)
    leaf_ids = collect_leaf_ids(find_node(forest, args.root_id))
    missing_leaf_ids = [leaf_id for leaf_id in leaf_ids if leaf_id not in leaves]
    if missing_leaf_ids:
        raise RuntimeError(f"{args.root_id} leaves missing from forest: {missing_leaf_ids}")
    known_gold = KNOWN_GOLD_BY_ROOT.get(args.root_id, {})

    # 金标准不参与提示词构造；这里仅复用批量调用与宿主结构校验。
    probes = tuple(
        ProbeSpec(
            leaf_id,
            leaf_id,
            False,
            (),
            SUPPORT_TYPES,
            f"{args.root_id}整树诊断，未输入金标准。",
        )
        for leaf_id in leaf_ids
    )
    block = load_block()
    messages = build_messages(block, probes, leaves)

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

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.max_concurrency)
    results = sorted(
        await asyncio.gather(
            *(
                run_one(provider, semaphore, run_index, block, probes, leaves)
                for run_index in range(1, args.runs + 1)
            )
        ),
        key=lambda item: item["run_index"],
    )
    summary = summarize(
        results,
        leaf_ids=leaf_ids,
        leaves=leaves,
        known_gold=known_gold,
    )
    artifact = {
        "experiment": "root subtree batch support diagnostic",
        "root_id": args.root_id,
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "forest_path": str(FOREST_PATH),
        "block_id": block["block_id"],
        "model": MODEL_ID,
        "thinking": "enabled" if args.thinking else "disabled",
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "run_count": args.runs,
        "max_concurrency": args.max_concurrency,
        "leaf_ids": leaf_ids,
        "system_prompt": BATCH_SYSTEM_PROMPT,
        "system_prompt_sha256": hashlib.sha256(BATCH_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "input_messages": [
            {"role": message.role, "content": message.content} for message in messages
        ],
        "known_gold_expectations": known_gold,
        "summary": summary,
        "results": results,
    }
    result_path = run_dir / "results.json"
    result_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
