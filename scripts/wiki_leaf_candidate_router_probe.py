"""运行单 block、多叶节点的证据候选路由实验。"""

# 探针必须先设置 backend 搜索路径，之后才能导入项目模块。
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
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
    FOREST_PATH,
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    PROBES,
    load_block,
    load_json,
    load_leaves,
)

OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "leaf_candidate_router_probe"
DEFAULT_RUN_COUNT = 3
DEFAULT_MAX_CONCURRENCY = 12

SYSTEM_PROMPT = """\
你是知识库证据候选路由器。每次收到一个 block 和一个叶节点候选池。

你的任务是从候选池中找出可能值得交给后续单叶验证器复核的叶节点。本步骤
只负责高召回候选生成，不是最终挂接，也不是对 block 做完整语义标注。这里
允许误召回，再由后续单叶验证器拒绝；不得为了减少候选数而自行裁定唯一归属。

【输入】
- block.section_path：原文所在章节路径；
- block.text：本次唯一可用的原文；
- leaves：候选叶节点数组。每个叶节点包含 leaf_id、path、title，并可能包含
  description 和 exclusions；未提供的字段不参与判断。

【判定目标】
对每个 leaf 独立回答：

当前 block 中是否存在至少一个具体原文线索，可能需要交给后续单叶验证器
确认它是否支持 leaf.path 与 leaf.title 所表示的知识对象或关系？

原文出现与叶节点对象、阶段、动作、条件、边界、机制、方法、案例或效果有关
的具体表述时，就必须成为候选。候选不要求最终成立，也不要求覆盖叶节点的
全部内容；存在是否成立的疑问时，交由后续单叶验证器判断。

【叶节点范围】
若输入提供 leaf.description，只能用于确认叶节点的具体范围，不得扩张
或改写 leaf.path/leaf.title 规定的关系。若输入提供 leaf.exclusions，其中
内容不构成支持；未提供的字段不参与判断。

【关键规则】
各个叶节点独立判断，不允许互相影响。

【输出】
只输出一个 JSON 对象，不得输出 Markdown 或 JSON 之外的说明。

decisions 数组必须覆盖输入 leaves 中的每个叶节点，且每个 leaf_id 恰好出现
一次。不得只返回命中的叶节点。逐项检查完成后，再输出最终 JSON。

命中时，is_candidate 为 true：
{
  "decisions": [
    {
      "leaf_id": "输入 leaves 中的 leaf_id",
      "is_candidate": true
    }
  ]
}

未命中时，is_candidate 为 false：
{
  "decisions": [
    {
      "leaf_id": "输入 leaves 中的 leaf_id",
      "is_candidate": false
    }
  ]
}

不得输出输入中不存在的 leaf_id，不得重复或遗漏同一个 leaf_id。每个 decision
只能包含 leaf_id、is_candidate，字段顺序必须是 leaf_id、is_candidate；不得
输出 quote、reason 或其他字段。
"""


@dataclass(frozen=True, slots=True)
class RouterProbe:
    """候选路由实验中的一个固定叶节点及其人工金标准。"""

    leaf_id: str
    expected_supported: bool
    rationale: str


ROUTER_PROBES = tuple(
    RouterProbe(
        leaf_id=probe.leaf_id,
        expected_supported=probe.expected_supported,
        rationale=probe.rationale,
    )
    for probe in PROBES
)

EXPANDED_PROBES = (
    RouterProbe(
        "K08.04.02",
        False,
        "原文没有超时作用域、取消请求传播、取消确认或资源释放内容。",
    ),
    RouterProbe(
        "K08.04.04",
        False,
        "原文没有执行状态快照、检查点粒度、恢复点或继续执行语义。",
    ),
    RouterProbe(
        "K08.05.02",
        False,
        "原文没有浏览器会话、页面操作、导航状态或页面环境清理内容。",
    ),
    RouterProbe(
        "K18.01.03",
        False,
        "人工审批不等于人机任务交接、责任转移或问责边界。",
    ),
    RouterProbe(
        "K18.02.01",
        False,
        "原文只有按操作配置审批和沙箱访问边界，没有提出自治等级模型或按风险、可逆性、影响范围确定等级的依据。",
    ),
    RouterProbe(
        "K18.02.02",
        False,
        "原文没有控制权分配、模式切换触发条件或切换时的人机确认。",
    ),
    RouterProbe(
        "K18.04.03",
        False,
        "原文没有超出能力后的升级路径、上下文交接或人工兜底责任承接。",
    ),
    RouterProbe(
        "K18.06.01",
        False,
        "原文讨论人类审批能力与审批疲劳导致的注意力下降，没有直接陈述信任形成、过度信任、信任不足或依赖程度校准。",
    ),
    RouterProbe(
        "K18.06.02",
        False,
        "原文没有向用户表达置信度、不确定性来源、风险等级或误差边界。",
    ),
    RouterProbe(
        "K18.07.01",
        False,
        "原文没有解释类型、决策因素、行动理由或结果归因的组织与呈现。",
    ),
    RouterProbe(
        "K12.03.02",
        False,
        "原文中的审查不等于遥测采样、聚合、基数控制或日志降噪。",
    ),
    RouterProbe(
        "K23.02.01",
        False,
        "原文只有边界可审计的表述，没有审计追踪标识、跨度或跨服务传播语义。",
    ),
)


def build_messages(
    block: dict[str, Any],
    leaves: dict[str, dict[str, Any]],
    router_probes: tuple[RouterProbe, ...],
    omitted_detail_leaf_ids: frozenset[str] = frozenset(),
    omitted_exclusion_leaf_ids: frozenset[str] = frozenset(),
) -> tuple[ChatMessage, ...]:
    """构造单 block 候选路由消息，可定向隐藏叶节点的解释性字段。"""

    payload = {
        "block": {key: value for key, value in block.items() if key != "block_id"},
        "leaves": [
            {
                key: value
                for key, value in leaves[probe.leaf_id].items()
                if not (
                    (
                        probe.leaf_id in omitted_detail_leaf_ids
                        and key in {"description", "exclusions"}
                    )
                    or (probe.leaf_id in omitted_exclusion_leaf_ids and key == "exclusions")
                )
            }
            for probe in router_probes
        ],
    }
    return (
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False, indent=2)),
    )


def parse_and_validate(
    content: str,
    *,
    allowed_leaf_ids: frozenset[str],
    expected_positive_ids: frozenset[str],
    expected_negative_ids: frozenset[str],
) -> dict[str, Any]:
    """解析逐叶判定结果，并检查字段顺序、候选 ID 和输入覆盖。"""

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
            and isinstance(item.get("leaf_id"), str)
            and item["leaf_id"] in allowed_leaf_ids
            and isinstance(item.get("is_candidate"), bool)
            and set(item) == {"leaf_id", "is_candidate"}
            and list(item) == ["leaf_id", "is_candidate"]
            for item in decisions
        )
        if decisions_array_ok
        else False
    )
    candidate_ids = (
        [
            item["leaf_id"]
            for item in decisions
            if isinstance(item, dict)
            and item.get("is_candidate") is True
            and isinstance(item.get("leaf_id"), str)
        ]
        if decisions_array_ok
        else []
    )
    decision_ids = (
        [
            item["leaf_id"]
            for item in decisions
            if isinstance(item, dict) and isinstance(item.get("leaf_id"), str)
        ]
        if decisions_array_ok
        else []
    )
    unique_ids_ok = len(candidate_ids) == len(set(candidate_ids))
    unique_decision_ids_ok = len(decision_ids) == len(set(decision_ids))
    decided_leaf_ids = frozenset(decision_ids)
    missing_leaf_ids = sorted(allowed_leaf_ids - decided_leaf_ids)
    extra_leaf_ids = sorted(decided_leaf_ids - allowed_leaf_ids)
    all_leaf_ids_covered = unique_decision_ids_ok and decided_leaf_ids == allowed_leaf_ids
    candidate_set = frozenset(candidate_ids)
    missing_positive_ids = sorted(expected_positive_ids - candidate_set)
    selected_negative_ids = sorted(expected_negative_ids & candidate_set)
    all_positives_recalled = not missing_positive_ids
    valid = (
        isinstance(parsed, dict)
        and set(parsed) == {"decisions"}
        and decisions_array_ok
        and decision_items_ok
        and unique_ids_ok
        and all_leaf_ids_covered
    )

    return {
        "parse_ok": True,
        "parsed": parsed,
        "valid": valid,
        "keys_ok": isinstance(parsed, dict) and set(parsed) == {"decisions"},
        "decisions_array_ok": decisions_array_ok,
        "decision_items_ok": decision_items_ok,
        "unique_ids_ok": unique_ids_ok,
        "unique_decision_ids_ok": unique_decision_ids_ok,
        "all_leaf_ids_covered": all_leaf_ids_covered,
        "missing_leaf_ids": missing_leaf_ids,
        "extra_leaf_ids": extra_leaf_ids,
        "candidate_ids": sorted(candidate_set),
        "candidate_count": len(candidate_set),
        "missing_positive_ids": missing_positive_ids,
        "selected_negative_ids": selected_negative_ids,
        "all_positives_recalled": all_positives_recalled,
    }


async def run_one(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    run_index: int,
    messages: tuple[ChatMessage, ...],
    *,
    max_output_tokens: int,
    allowed_leaf_ids: frozenset[str],
    expected_positive_ids: frozenset[str],
    expected_negative_ids: frozenset[str],
) -> dict[str, Any]:
    """执行一次候选路由调用，并保留完整输入输出。"""

    started_at = datetime.now(UTC).isoformat()
    async with semaphore:
        try:
            result = await provider.complete(messages, max_output_tokens=max_output_tokens)
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
        allowed_leaf_ids=allowed_leaf_ids,
        expected_positive_ids=expected_positive_ids,
        expected_negative_ids=expected_negative_ids,
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
    router_probes: tuple[RouterProbe, ...],
) -> dict[str, Any]:
    """汇总候选召回、误召回和宿主合法性指标。"""

    completed = [result for result in results if "validation" in result]
    valid_rows = [row for row in completed if row["validation"].get("valid") is True]
    all_positive_rows = [
        row for row in completed if row["validation"].get("all_positives_recalled") is True
    ]
    candidate_counts = [row["validation"].get("candidate_count", 0) for row in completed]
    selection_counts = {
        probe.leaf_id: sum(
            probe.leaf_id in row["validation"].get("candidate_ids", []) for row in completed
        )
        for probe in router_probes
    }
    return {
        "calls": len(results),
        "completed": len(completed),
        "parsed": sum(bool(row["validation"].get("parse_ok")) for row in completed),
        "valid": len(valid_rows),
        "all_positives_recalled": len(all_positive_rows),
        "candidate_count_min": min(candidate_counts, default=0),
        "candidate_count_max": max(candidate_counts, default=0),
        "candidate_count_mean": (
            sum(candidate_counts) / len(candidate_counts) if candidate_counts else 0.0
        ),
        "selection_counts": selection_counts,
        "input_tokens": sum(result.get("input_tokens", 0) for result in results),
        "output_tokens": sum(result.get("output_tokens", 0) for result in results),
    }


def parse_args() -> argparse.Namespace:
    """解析候选路由探针的运行次数和并发数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUN_COUNT, help="运行次数")
    parser.add_argument(
        "--probe-set",
        choices=("baseline-12", "expanded-24"),
        default="baseline-12",
        help="候选叶节点探针集合；expanded-24 在高相似负例中加入 12 个扩容干扰项",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
        help="最大并发调用数",
    )
    parser.add_argument(
        "--leaf-order",
        choices=("fixed", "reverse", "seeded-random"),
        default="fixed",
        help="候选叶节点的排列方式；seeded-random 按运行序号生成稳定随机顺序",
    )
    parser.add_argument(
        "--omit-details",
        action="append",
        default=[],
        metavar="LEAF_ID",
        help="从输入中隐藏指定叶节点的 description 和 exclusions；可重复指定",
    )
    parser.add_argument(
        "--omit-all-details",
        action="store_true",
        help="从所有候选叶节点的输入中隐藏 description 和 exclusions",
    )
    parser.add_argument(
        "--omit-all-exclusions",
        action="store_true",
        help="从所有候选叶节点的输入中只隐藏 exclusions，保留 description",
    )
    parser.add_argument(
        "--seed-offset",
        type=int,
        default=0,
        help="seeded-random 的稳定随机种子偏移，用于生成多路独立排列",
    )
    parser.add_argument(
        "--thinking-enabled",
        action="store_true",
        help="开启供应商的 thinking 模式；默认关闭",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=MAX_OUTPUT_TOKENS,
        help="单次调用的最大输出 token 数；thinking 模式需要包含思考预算",
    )
    parser.add_argument(
        "--exclude-leaf-id",
        action="append",
        default=[],
        metavar="LEAF_ID",
        help="从候选池中移除指定叶节点；可重复指定",
    )
    parser.add_argument(
        "--only-leaf-id",
        action="append",
        default=[],
        metavar="LEAF_ID",
        help="只保留指定叶节点构造候选池；可重复指定",
    )
    parser.add_argument(
        "--extra-positive-leaf-id",
        action="append",
        default=[],
        metavar="LEAF_ID",
        help="向实验池追加已由单叶判定确认支持的正例；可重复指定",
    )
    parser.add_argument(
        "--pin-leaf-id",
        help="在基础排列上把指定叶节点移动到 --pin-position",
    )
    parser.add_argument(
        "--pin-position",
        type=int,
        help="与 --pin-leaf-id 配合使用的一基位置",
    )
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be positive")
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    if args.max_output_tokens <= 0:
        parser.error("--max-output-tokens must be positive")
    if (args.pin_leaf_id is None) != (args.pin_position is None):
        parser.error("--pin-leaf-id and --pin-position must be provided together")
    return args


def order_router_probes(
    router_probes: tuple[RouterProbe, ...],
    *,
    order_mode: str,
    run_index: int,
    pinned_leaf_id: str | None = None,
    pinned_position: int | None = None,
) -> tuple[RouterProbe, ...]:
    """按实验配置返回一次调用使用的叶节点顺序。"""

    ordered = list(router_probes)
    if order_mode == "reverse":
        ordered.reverse()
    elif order_mode == "seeded-random":
        random.Random(run_index).shuffle(ordered)
    if pinned_leaf_id is not None and pinned_position is not None:
        pinned_probe = next(
            (probe for probe in ordered if probe.leaf_id == pinned_leaf_id),
            None,
        )
        if pinned_probe is None:
            raise ValueError(f"pinned leaf is not in probe pool: {pinned_leaf_id}")
        if not 1 <= pinned_position <= len(ordered):
            raise ValueError(
                f"pinned position must be between 1 and {len(ordered)}: {pinned_position}"
            )
        ordered.remove(pinned_probe)
        ordered.insert(pinned_position - 1, pinned_probe)
    return tuple(ordered)


async def main() -> None:
    """加载固定输入，执行候选路由并保存可追溯产物。"""

    args = parse_args()
    forest = load_json(FOREST_PATH)
    leaves = load_leaves(forest)
    router_probes = ROUTER_PROBES
    if args.probe_set == "expanded-24":
        router_probes = (*ROUTER_PROBES, *EXPANDED_PROBES)
    extra_positive_leaf_ids = tuple(dict.fromkeys(args.extra_positive_leaf_id))
    duplicate_extra_positive_leaf_ids = frozenset(extra_positive_leaf_ids) & frozenset(
        probe.leaf_id for probe in router_probes
    )
    if duplicate_extra_positive_leaf_ids:
        raise RuntimeError(
            "extra positive leaves already exist in probe pool: "
            f"{sorted(duplicate_extra_positive_leaf_ids)}"
        )
    unknown_extra_positive_leaf_ids = frozenset(extra_positive_leaf_ids) - frozenset(leaves)
    if unknown_extra_positive_leaf_ids:
        raise RuntimeError(
            f"extra positive leaves missing from forest: {sorted(unknown_extra_positive_leaf_ids)}"
        )
    router_probes = (
        *router_probes,
        *(
            RouterProbe(
                leaf_id,
                True,
                "命令行扩容正例：已经由单叶判定确认支持当前测试 block。",
            )
            for leaf_id in extra_positive_leaf_ids
        ),
    )
    excluded_leaf_ids = frozenset(args.exclude_leaf_id)
    included_leaf_ids = frozenset(args.only_leaf_id)
    unknown_excluded_leaf_ids = excluded_leaf_ids - frozenset(
        probe.leaf_id for probe in router_probes
    )
    if unknown_excluded_leaf_ids:
        raise RuntimeError(
            f"cannot exclude leaves outside probe pool: {sorted(unknown_excluded_leaf_ids)}"
        )
    unknown_included_leaf_ids = included_leaf_ids - frozenset(
        probe.leaf_id for probe in router_probes
    )
    if unknown_included_leaf_ids:
        raise RuntimeError(
            f"cannot include leaves outside probe pool: {sorted(unknown_included_leaf_ids)}"
        )
    active_probes = tuple(
        probe
        for probe in router_probes
        if probe.leaf_id not in excluded_leaf_ids
        and (not included_leaf_ids or probe.leaf_id in included_leaf_ids)
    )
    if not active_probes:
        raise RuntimeError("candidate probe pool is empty after exclusions")
    missing_leaf_ids = [probe.leaf_id for probe in active_probes if probe.leaf_id not in leaves]
    if missing_leaf_ids:
        raise RuntimeError(f"probe leaves missing from forest: {missing_leaf_ids}")

    block = load_block()
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
            max_output_tokens=args.max_output_tokens,
            thinking_enabled=args.thinking_enabled,
        ),
    )

    allowed_leaf_ids = frozenset(probe.leaf_id for probe in active_probes)
    expected_positive_ids = frozenset(
        probe.leaf_id for probe in active_probes if probe.expected_supported
    )
    expected_negative_ids = allowed_leaf_ids - expected_positive_ids
    omitted_detail_leaf_ids = (
        allowed_leaf_ids if args.omit_all_details else frozenset(args.omit_details)
    )
    omitted_exclusion_leaf_ids = allowed_leaf_ids if args.omit_all_exclusions else frozenset()
    unknown_omitted_leaf_ids = omitted_detail_leaf_ids - allowed_leaf_ids
    if unknown_omitted_leaf_ids:
        raise RuntimeError(
            f"cannot omit details for leaves outside probe pool: {sorted(unknown_omitted_leaf_ids)}"
        )
    semaphore = asyncio.Semaphore(args.max_concurrency)
    tasks = []
    for run_index in range(1, args.runs + 1):
        ordered_probes = order_router_probes(
            active_probes,
            order_mode=args.leaf_order,
            run_index=run_index + args.seed_offset,
            pinned_leaf_id=args.pin_leaf_id,
            pinned_position=args.pin_position,
        )
        messages = build_messages(
            block,
            leaves,
            ordered_probes,
            omitted_detail_leaf_ids,
            omitted_exclusion_leaf_ids,
        )
        tasks.append(
            run_one(
                provider,
                semaphore,
                run_index,
                messages,
                max_output_tokens=args.max_output_tokens,
                allowed_leaf_ids=allowed_leaf_ids,
                expected_positive_ids=expected_positive_ids,
                expected_negative_ids=expected_negative_ids,
            )
        )
    results = sorted(await asyncio.gather(*tasks), key=lambda row: row["run_index"])
    summary = summarize(results, active_probes)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    artifact = {
        "experiment": "E-2 multi-leaf candidate routing",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "forest_path": str(FOREST_PATH),
        "block_id": BLOCK_ID,
        "model": MODEL_ID,
        "thinking": "enabled" if args.thinking_enabled else "disabled",
        "temperature": 0.0,
        "max_output_tokens": args.max_output_tokens,
        "run_count": args.runs,
        "probe_set": args.probe_set,
        "max_concurrency": args.max_concurrency,
        "leaf_order": args.leaf_order,
        "seed_offset": args.seed_offset,
        "pinned_leaf_id": args.pin_leaf_id,
        "pinned_position": args.pin_position,
        "excluded_leaf_ids": sorted(excluded_leaf_ids),
        "included_leaf_ids": sorted(included_leaf_ids),
        "extra_positive_leaf_ids": list(extra_positive_leaf_ids),
        "omitted_detail_leaf_ids": sorted(omitted_detail_leaf_ids),
        "omitted_exclusion_leaf_ids": sorted(omitted_exclusion_leaf_ids),
        "system_prompt": SYSTEM_PROMPT,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "probes": [
            {
                "leaf_id": probe.leaf_id,
                "expected_supported": probe.expected_supported,
                "gold_rationale": probe.rationale,
                "leaf": leaves[probe.leaf_id],
            }
            for probe in active_probes
        ],
        "summary": summary,
        "results": results,
    }
    result_path = run_dir / "results.json"
    result_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
