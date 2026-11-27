"""诊断单 block 对全部一级知识树的明确排除判断。"""

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
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
    DeepSeekChatProvider,
)
from app.storage.credentials import CHAT_API_KEY_USERNAME, WindowsCredentialStore
from wiki_leaf_support_probe import (
    BLOCK_ID,
    BLOCK_SET_PATH,
    FOREST_PATH,
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    load_block,
    load_json,
)

OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "root_exclusion_probe"
DEFAULT_RUN_COUNT = 10
DEFAULT_MAX_CONCURRENCY = 10

DEFAULT_EXPECTED_RETAINED_ROOT_IDS = frozenset({"K01", "K08", "K11", "K16", "K18", "K24"})

SYSTEM_PROMPT = """\
你是知识树一级候选筛选器。你会收到一个 block 和该领域全部一级知识树。

你的任务不是判断哪些一级树最相关，而是只排除已经可以确定“整棵一级树下的所有知识
叶节点都不可能从当前 block 获得直接证据”的一级树。这是后续叶节点挂接的高召回前置
步骤，误排除会造成不可恢复的漏挂，因此必须保守。

【输入】
- block.section_path：原文所在章节路径；
- block.text：本次唯一可用的原文；
- roots：全部一级候选范围。每项包含 root_id、title 和 children；children 中的每个
  二级节点包含 title，并可能包含 children，后者是该二级节点的直接三级子节点标题。
  输入最多提供到三级标题，不提供节点描述，也不提供更深层叶节点。

【保留规则】
只要某个一级树的一级标题、二级标题或三级标题所指向的范围，可能包含一个由当前
block 中的具体陈述直接支持的对象、关系、分类、组成、机制、条件、方法、实例、效果或
评价，就保留这个一级树。

标题只用于识别知识范围和目录位置，不需要在标题中出现 block 的原词。即使 block 使用
具体产品、操作或案例表述，只要其知识身份可能对应某个子节点标题下的叶节点，就必须
保留。

以下情况必须保留：
1. block 提供的只是具体产品案例、实现、条件或效果，但可能支持某个上位知识叶节点；
2. 当前 block 只覆盖一级树中的一个小分支；
3. 下级标题提示了可能范围，但需要查看更深层叶节点定义才能进一步确认；
4. 你只能在“该一级标题及全部已提供的下级标题所指的知识范围，都与当前 block 无关或
   明确冲突”时排除。

不得因为一级树范围很宽、当前 block 没有覆盖该一级树的全部标题、标题不是 block 的
原词，或你没有看到更深层叶节点就排除。候选是否最终成立由后续叶节点验证器决定。

【输出】
只输出一个 JSON 对象，不得输出 Markdown 或 JSON 之外的说明。

decisions 必须覆盖 roots 中的所有 root_id，每项恰好出现一次。字段顺序固定为
root_id、is_retained、reason。reason 用不超过 80 字说明为何保留或排除。

{
  "decisions": [
    {
      "root_id": "输入 roots 中的 root_id",
      "is_retained": true,
      "reason": "简要理由"
    }
  ]
}

不得输出输入中不存在的 root_id，不得重复或遗漏 root_id，不得增加其他字段。
"""


def build_messages(
    block: dict[str, Any],
    roots: list[dict[str, Any]],
    *,
    detail_mode: str,
) -> tuple[ChatMessage, ...]:
    """构造一级排除消息；主模式输入一级、二级和三级标题骨架。"""

    payload_roots: list[dict[str, Any]] = []
    for root in roots:
        item = {
            "root_id": root["node_id"],
            "title": root["title"],
        }
        if detail_mode == "skeleton":
            item["children"] = [child["title"] for child in root.get("children", [])]
        elif detail_mode == "skeleton-l3":
            item["children"] = [
                {
                    "title": child["title"],
                    "children": [grandchild["title"] for grandchild in child.get("children", [])],
                }
                for child in root.get("children", [])
            ]
        elif detail_mode == "full":
            item["description"] = root.get("description", "")
            item["exclusions"] = root.get("exclusions", [])
        payload_roots.append(item)

    payload = {
        "block": {key: value for key, value in block.items() if key != "block_id"},
        "roots": payload_roots,
    }
    return (
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(
            role="user",
            content=json.dumps(payload, ensure_ascii=False, indent=2),
        ),
    )


def parse_and_validate(
    content: str,
    *,
    expected_root_ids: frozenset[str],
    expected_retained_root_ids: frozenset[str],
) -> dict[str, Any]:
    """解析逐一级树的保留判断并检查输入覆盖。"""

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        return {
            "parse_ok": False,
            "parse_error": f"{type(error).__name__}: {error}",
            "valid": False,
        }

    if not isinstance(parsed, dict) or set(parsed) != {"decisions"}:
        return {
            "parse_ok": True,
            "parsed": parsed,
            "valid": False,
            "keys_ok": False,
        }

    decisions = parsed.get("decisions")
    decisions_array_ok = isinstance(decisions, list)
    decision_items_ok = (
        decisions_array_ok
        and all(
            isinstance(item, dict)
            and list(item) == ["root_id", "is_retained", "reason"]
            and isinstance(item.get("root_id"), str)
            and item["root_id"] in expected_root_ids
            and isinstance(item.get("is_retained"), bool)
            and isinstance(item.get("reason"), str)
            and bool(item["reason"].strip())
            for item in decisions
        )
        if decisions_array_ok
        else False
    )
    decision_ids = (
        [
            item["root_id"]
            for item in decisions
            if isinstance(item, dict) and isinstance(item.get("root_id"), str)
        ]
        if decisions_array_ok
        else []
    )
    decided_ids = frozenset(decision_ids)
    all_roots_covered = (
        len(decision_ids) == len(set(decision_ids)) and decided_ids == expected_root_ids
    )
    retained_ids = (
        frozenset(
            item["root_id"]
            for item in decisions
            if isinstance(item, dict)
            and isinstance(item.get("root_id"), str)
            and item.get("is_retained") is True
        )
        if decisions_array_ok
        else frozenset()
    )
    false_excluded_ids = expected_retained_root_ids - retained_ids
    retained_negative_ids = retained_ids - expected_retained_root_ids
    valid = decisions_array_ok and decision_items_ok and all_roots_covered

    return {
        "parse_ok": True,
        "parsed": parsed,
        "valid": valid,
        "keys_ok": True,
        "decisions_array_ok": decisions_array_ok,
        "decision_items_ok": decision_items_ok,
        "all_roots_covered": all_roots_covered,
        "retained_ids": sorted(retained_ids),
        "retained_count": len(retained_ids),
        "false_excluded_ids": sorted(false_excluded_ids),
        "retained_negative_ids": sorted(retained_negative_ids),
        "all_expected_retained": not false_excluded_ids,
    }


async def run_one(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    run_index: int,
    messages: tuple[ChatMessage, ...],
    *,
    expected_root_ids: frozenset[str],
    expected_retained_root_ids: frozenset[str],
) -> dict[str, Any]:
    """执行一次一级排除调用并保存完整输入输出。"""

    started_at = datetime.now(UTC).isoformat()
    async with semaphore:
        try:
            result = await provider.complete(
                messages,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        except ChatProviderError as error:  # 实验保留单次失败，不做静默重试。
            return {
                "run_index": run_index,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
                "error": f"{type(error).__name__}: {error}",
            }

    validation = parse_and_validate(
        result.content,
        expected_root_ids=expected_root_ids,
        expected_retained_root_ids=expected_retained_root_ids,
    )
    return {
        "run_index": run_index,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "messages": [{"role": message.role, "content": message.content} for message in messages],
        "raw_content": result.content,
        "request_id": result.request_id,
        "actual_model_id": result.actual_model_id,
        "finish_reason": result.finish_reason,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "validation": validation,
    }


def summarize(
    results: list[dict[str, Any]],
    roots: list[dict[str, Any]],
) -> dict[str, Any]:
    """汇总一级排除稳定性、漏排除和误保留。"""

    completed = [result for result in results if "validation" in result]
    valid_rows = [row for row in completed if row["validation"].get("valid") is True]
    retain_counts = {
        root["node_id"]: sum(
            root["node_id"] in row["validation"].get("retained_ids", []) for row in completed
        )
        for root in roots
    }
    completed_count = len(completed)
    return {
        "calls": len(results),
        "completed": len(completed),
        "parsed": sum(bool(row["validation"].get("parse_ok")) for row in completed),
        "valid": len(valid_rows),
        "all_expected_retained": sum(
            row["validation"].get("all_expected_retained") is True for row in completed
        ),
        "run_summaries": [
            {
                "run_index": row["run_index"],
                "valid": row["validation"].get("valid") is True,
                "retained_ids": row["validation"].get("retained_ids", []),
                "false_excluded_ids": row["validation"].get("false_excluded_ids", []),
                "retained_negative_ids": row["validation"].get("retained_negative_ids", []),
            }
            for row in completed
        ],
        "retain_counts": retain_counts,
        "stable_retained_ids": sorted(
            root_id
            for root_id, count in retain_counts.items()
            if completed_count > 0 and count == completed_count
        ),
        "stable_excluded_ids": sorted(
            root_id
            for root_id, count in retain_counts.items()
            if completed_count > 0 and count == 0
        ),
        "mixed_ids": sorted(
            root_id for root_id, count in retain_counts.items() if 0 < count < completed_count
        ),
        "input_tokens": sum(result.get("input_tokens", 0) for result in results),
        "output_tokens": sum(result.get("output_tokens", 0) for result in results),
    }


def load_expected_retained_root_ids(gold_path: Path) -> frozenset[str]:
    """从逐叶金标准推导一级保留集合；一级只使用正例叶节点的祖先根 ID。"""

    gold_rows = load_json(gold_path)
    if not isinstance(gold_rows, list) or not gold_rows:
        raise RuntimeError("leaf gold must be a non-empty JSON array")

    retained_root_ids: set[str] = set()
    seen_leaf_ids: set[str] = set()
    allowed_grades = {"no_support", "weak_support", "direct_support"}
    for index, row in enumerate(gold_rows, start=1):
        if not isinstance(row, dict):
            raise TypeError(f"leaf gold row #{index} must be an object")
        leaf_id = row.get("leaf_id")
        support_grade = row.get("support_grade")
        expected_supported = row.get("expected_supported")
        if not isinstance(leaf_id, str) or "." not in leaf_id:
            raise RuntimeError(f"leaf gold row #{index} has an invalid leaf_id")
        if leaf_id in seen_leaf_ids:
            raise RuntimeError(f"duplicate leaf_id in gold: {leaf_id}")
        seen_leaf_ids.add(leaf_id)
        if support_grade not in allowed_grades:
            raise RuntimeError(f"leaf {leaf_id} has an invalid support_grade")
        if expected_supported is not (support_grade != "no_support"):
            raise RuntimeError(f"leaf {leaf_id} expected_supported does not match support_grade")
        if support_grade != "no_support":
            retained_root_ids.add(leaf_id.split(".", 1)[0])

    if not retained_root_ids:
        raise RuntimeError("leaf gold contains no positive support leaves")
    return frozenset(retained_root_ids)


def parse_args() -> argparse.Namespace:
    """解析回放输入、次数、并发数和一级树输入模式。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUN_COUNT)
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
    )
    parser.add_argument(
        "--detail-mode",
        choices=("skeleton", "skeleton-l3", "full", "roots-only"),
        default="skeleton-l3",
        help=(
            "skeleton 输入一级与二级标题；skeleton-l3 再输入三级标题；"
            "full 输入一级描述与排除项；roots-only 只输入一级编号和标题"
        ),
    )
    parser.add_argument(
        "--block-id",
        default=BLOCK_ID,
        help="待诊断的 block ID；默认保持既有 b00008 探针",
    )
    parser.add_argument(
        "--block-set-path",
        type=Path,
        default=BLOCK_SET_PATH,
        help="包含目标 block 的 blocks.json; 默认保持既有 b00008 快照路径",
    )
    parser.add_argument(
        "--gold-path",
        type=Path,
        help="逐叶三档金标准；提供后从正例叶节点动态推导一级保留集合",
    )
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be positive")
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    if args.block_id != BLOCK_ID and args.gold_path is None:
        parser.error("--gold-path is required when --block-id changes")
    return args


async def main() -> None:
    """加载指定 block 和全部一级树，执行一级排除回放。"""

    args = parse_args()
    forest = load_json(FOREST_PATH)
    roots = forest["roots"]
    root_ids = frozenset(root["node_id"] for root in roots)
    expected_retained_root_ids = (
        load_expected_retained_root_ids(args.gold_path)
        if args.gold_path is not None
        else DEFAULT_EXPECTED_RETAINED_ROOT_IDS
    )
    missing_retained_roots = expected_retained_root_ids - root_ids
    if missing_retained_roots:
        raise RuntimeError(
            f"gold-derived retained roots are missing from forest: {sorted(missing_retained_roots)}"
        )
    block = load_block(args.block_set_path, args.block_id)
    messages = build_messages(block, roots, detail_mode=args.detail_mode)

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
            thinking_enabled=False,
        ),
    )

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.max_concurrency)
    results = sorted(
        await asyncio.gather(
            *(
                run_one(
                    provider,
                    semaphore,
                    run_index,
                    messages,
                    expected_root_ids=root_ids,
                    expected_retained_root_ids=expected_retained_root_ids,
                )
                for run_index in range(1, args.runs + 1)
            )
        ),
        key=lambda item: item["run_index"],
    )
    summary = summarize(results, roots)
    artifact = {
        "experiment": "root-level explicit exclusion diagnostic",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "forest_path": str(FOREST_PATH),
        "block_set_path": str(args.block_set_path),
        "block_id": block["block_id"],
        "model": MODEL_ID,
        "thinking": "disabled",
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "run_count": args.runs,
        "max_concurrency": args.max_concurrency,
        "detail_mode": args.detail_mode,
        "root_ids": sorted(root_ids),
        "gold_path": str(args.gold_path) if args.gold_path is not None else None,
        "gold_sha256": (
            hashlib.sha256(args.gold_path.read_bytes()).hexdigest()
            if args.gold_path is not None
            else None
        ),
        "expected_retained_root_ids": sorted(expected_retained_root_ids),
        "system_prompt": SYSTEM_PROMPT,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "input_messages": [
            {"role": message.role, "content": message.content} for message in messages
        ],
        "summary": summary,
        "results": results,
    }
    result_path = run_dir / "results.json"
    result_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {"run_dir": str(run_dir), "summary": summary},
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
