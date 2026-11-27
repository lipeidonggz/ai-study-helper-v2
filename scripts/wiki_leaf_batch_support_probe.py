"""运行单 block、多叶节点批量证据支持判定实验。"""

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
from wiki_leaf_candidate_router_probe import EXPANDED_PROBES
from wiki_leaf_support_probe import (
    BLOCK_SET_PATH,
    FOREST_PATH,
    MAX_OUTPUT_TOKENS,
    MODEL_ID,
    PROBES,
    SUPPORT_TYPES,
    ProbeSpec,
    load_block,
    load_json,
    load_leaves,
    parse_and_validate,
)
from wiki_leaf_support_probe import (
    SYSTEM_PROMPT as SINGLE_LEAF_SYSTEM_PROMPT,
)

BASELINE_LEAF_IDS = (
    "K08.05.01",
    "K08.05.03",
    "K18.03.01",
    "K18.04.01",
    "K18.01.02",
    "K16.07.02",
    "K09.03.01",
    "K12.07.02",
    "K23.01.01",
    "K03.06.03",
    "K18.04.02",
    "K18.03.02",
)
EXPANDED_LEAF_IDS = (
    *BASELINE_LEAF_IDS[:-1],
    *(probe.leaf_id for probe in EXPANDED_PROBES),
    BASELINE_LEAF_IDS[-1],
)
ADDITIONAL_PROBES = (
    ProbeSpec(
        "A01",
        "K16.03.02",
        True,
        (),
        SUPPORT_TYPES,
        "原文按读取、写入、bash 与网络访问划分权限和审批，构成最小权限与运行时授权检查。",
    ),
    ProbeSpec(
        "A02",
        "K16.03.03",
        True,
        (),
        SUPPORT_TYPES,
        "原文对高影响写入、bash 和网络访问要求审批并在沙箱中隔离执行，构成工具副作用控制实例。",
    ),
    ProbeSpec(
        "A03",
        "K16.06.01",
        True,
        (),
        SUPPORT_TYPES,
        "原文给出工作区写入边界、默认禁止网络和 OS 级沙箱，构成执行环境隔离与沙箱架构实例。",
    ),
    ProbeSpec(
        "A04",
        "K24.04.01",
        True,
        (),
        SUPPORT_TYPES,
        "原文在写入、bash 和网络访问工具调用前要求批准，构成事前审批闸门及触发条件实例。",
    ),
    ProbeSpec(
        "A05",
        "K05.01.01",
        False,
        (),
        (),
        "原文没有工作记忆、短期记忆、上下文保持或记忆容量内容。",
    ),
    ProbeSpec(
        "A06",
        "K06.03.01",
        False,
        (),
        (),
        "原文没有检索嵌入、语义向量表示或向量空间内容。",
    ),
    ProbeSpec(
        "A07",
        "K07.01.01",
        False,
        (),
        (),
        "原文没有多 Agent 组成、角色协作或跨 Agent 通信内容。",
    ),
    ProbeSpec(
        "A08",
        "K10.01.02",
        False,
        (),
        (),
        "原文没有数据模式、数据库迁移或模式版本演进内容。",
    ),
    ProbeSpec(
        "A09",
        "K11.02.01",
        False,
        (),
        (),
        "原文没有任务成功度量、评测指标或评测集设计内容。",
    ),
    ProbeSpec(
        "A10",
        "K14.02.01.01",
        False,
        (),
        (),
        "原文没有 token 用量、模型推理成本或成本计量内容。",
    ),
    ProbeSpec(
        "A11",
        "K21.02.02",
        False,
        (),
        (),
        "原文没有流程自动化应用模式、业务系统编排或流程改造内容。",
    ),
    ProbeSpec(
        "A12",
        "K22.01.02",
        False,
        (),
        (),
        "原文没有图编排、工作流式 Agent 框架或开发框架能力内容。",
    ),
)
ADDITIONAL_LEAF_IDS = tuple(probe.leaf_id for probe in ADDITIONAL_PROBES)
EXPANDED_36_LEAF_IDS = (
    *EXPANDED_LEAF_IDS[:-1],
    *ADDITIONAL_LEAF_IDS,
    BASELINE_LEAF_IDS[-1],
)
BATCH_48_PROBES = (
    ProbeSpec(
        "B01",
        "K01.02.03",
        False,
        (),
        (),
        "该叶节点明确排除具体安全控制实现；原文中的沙箱与审批不能替代能力边界与约束来源的概念说明。",
    ),
    ProbeSpec(
        "B02",
        "K08.05.04",
        False,
        (),
        (),
        "原文只支持运行时隔离，没有工具执行中断、部分结果、恢复入口或清理语义；标题要求的是隔离与中断恢复的联合知识身份。",
    ),
    ProbeSpec(
        "B03",
        "K16.06.02",
        False,
        (),
        (),
        "原文有审批和沙箱两种控制，但没有给出护栏分层结构及失效关闭或开放设计。",
    ),
    ProbeSpec(
        "B04",
        "K24.02.02",
        False,
        (),
        (),
        "原文是沙箱实现行为，没有直接陈述策略执行点的类型、布置或失败关闭架构。",
    ),
    ProbeSpec(
        "B05",
        "K04.06.01",
        False,
        (),
        (),
        "原文没有工具调用主体标识、凭据引用、令牌受众或认证方案契约。",
    ),
    ProbeSpec(
        "B06",
        "K04.06.02",
        False,
        (),
        (),
        "原文的读取放行和写入等审批属于运行时策略，不是工具接口契约中的权限范围或作用域声明。",
    ),
    ProbeSpec(
        "B07",
        "K08.04.05",
        False,
        (),
        (),
        "原文要求人工批准，但没有说明运行时挂起、状态保存、上下文交接、恢复或终止机制。",
    ),
    ProbeSpec(
        "B08",
        "K11.06.02",
        False,
        (),
        (),
        "原文没有工具滥用、越权或数据泄露的测试场景、判据和证据要求。",
    ),
    ProbeSpec(
        "B09",
        "K16.01.02",
        False,
        (),
        (),
        "原文没有攻击树、攻击链、跨信任边界的传播路径、威胁影响评估或风险排序。",
    ),
    ProbeSpec(
        "B10",
        "K18.03.03",
        True,
        (),
        SUPPORT_TYPES,
        "审批弹窗要求监督者评估Agent操作及风险，构成监督职责与监督信息需求的具体案例。",
    ),
    ProbeSpec(
        "B11",
        "K21.07.03",
        False,
        (),
        (),
        "原文展示逐动作审批，没有业务层面的监督触发条件、接管范围、升级路径或人工兜底责任。",
    ),
    ProbeSpec(
        "B12",
        "K23.01.03.03",
        False,
        (),
        (),
        "原文只说运行时开源、边界可审计，没有定义工具调用审计事件及其参数、结果、错误、副作用或发起者字段。",
    ),
)
MIXED_K08_K16_PROBES = (
    ProbeSpec(
        "M01",
        "K08.05.01",
        True,
        (),
        SUPPORT_TYPES,
        "原文给出OS级沙箱及文件系统、网络访问边界，是代码执行沙箱的稳定正例。",
    ),
    ProbeSpec(
        "M02",
        "K16.03.02",
        True,
        (),
        SUPPORT_TYPES,
        "原文按读取、写入、bash和网络访问划分权限并默认拒绝网络，体现最小权限与授权检查。",
    ),
    ProbeSpec(
        "M03",
        "K08.05.03",
        True,
        (),
        SUPPORT_TYPES,
        "原文限制工作区写入并默认拒绝网络，是资源隔离与执行边界的稳定正例。",
    ),
    ProbeSpec(
        "M04",
        "K16.03.03",
        True,
        (),
        SUPPORT_TYPES,
        "原文对写入、bash和网络访问要求审批，并以沙箱限制副作用，是工具副作用控制实例。",
    ),
    ProbeSpec(
        "M05",
        "K16.06.01",
        True,
        (),
        SUPPORT_TYPES,
        "原文给出OS级沙箱、工作区写入边界和默认拒绝网络，是执行环境隔离与沙箱架构实例。",
    ),
    ProbeSpec(
        "M06",
        "K08.05.04",
        False,
        (),
        (),
        "原文支持运行时隔离，但没有工具执行中断、部分结果、恢复入口或清理语义。",
    ),
    ProbeSpec(
        "M07",
        "K16.07.02",
        True,
        (),
        SUPPORT_TYPES,
        "原文把人工审批作为人类监督机制，并说明审批疲劳可能削弱监督有效性。",
    ),
    ProbeSpec(
        "M08",
        "K08.04.05",
        False,
        (),
        (),
        "原文要求人工批准，但没有运行时挂起、状态保存、恢复入口或终止机制。",
    ),
    ProbeSpec(
        "M09",
        "K16.06.02",
        False,
        (),
        (),
        "原文有审批和沙箱两种控制，但没有给出护栏分层结构及失效关闭或开放设计。",
    ),
    ProbeSpec(
        "M10",
        "K16.01.02",
        False,
        (),
        (),
        "原文没有攻击链、跨信任边界传播路径、威胁影响评估或风险排序。",
    ),
)
P01_B014_ORIGINAL_PROBES = (
    ProbeSpec("O01", "K11.07.03", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O02", "K13.04.04.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O03", "K16.07.01", True, (), SUPPORT_TYPES, "P01-B014 已审核正例。"),
    ProbeSpec("O04", "K18.04.02", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O05", "K21.05.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O06", "K24.02.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O07", "K01.06.02", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O08", "K03.03.04", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O09", "K04.07.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("O10", "K08.04.01", False, (), (), "P01-B014 已审核负例。"),
)
P01_B014_REORDERED_PROBES = (
    ProbeSpec("R01", "K11.07.03", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R02", "K03.03.04", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R03", "K18.04.02", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R04", "K24.02.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R05", "K16.07.01", True, (), SUPPORT_TYPES, "P01-B014 已审核正例。"),
    ProbeSpec("R06", "K01.06.02", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R07", "K13.04.04.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R08", "K21.05.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R09", "K04.07.01", False, (), (), "P01-B014 已审核负例。"),
    ProbeSpec("R10", "K08.04.01", False, (), (), "P01-B014 已审核负例。"),
)
BATCH_48_LEAF_IDS = tuple(probe.leaf_id for probe in BATCH_48_PROBES)
EXPANDED_48_LEAF_IDS = (
    *EXPANDED_36_LEAF_IDS[:-1],
    *BATCH_48_LEAF_IDS,
    BASELINE_LEAF_IDS[-1],
)
OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "leaf_batch_support_probe"
DEFAULT_RUN_COUNT = 3
MAX_CONCURRENCY = 10


def build_batch_system_prompt() -> str:
    """在单叶 E-1 判据不变的前提下，只替换输入和输出契约。"""

    rules_marker = "【leaf 的含义】"
    output_marker = "【输出】"
    if (
        rules_marker not in SINGLE_LEAF_SYSTEM_PROMPT
        or output_marker not in SINGLE_LEAF_SYSTEM_PROMPT
    ):
        raise RuntimeError("single-leaf prompt markers are missing")
    semantic_rules = (
        rules_marker
        + SINGLE_LEAF_SYSTEM_PROMPT.split(rules_marker, 1)[1].split(
            output_marker,
            1,
        )[0]
    )
    return f"""\
你是知识库证据挂接判定器。每次收到一个 block 和一个叶节点数组，并对 leaves 中每个
叶节点独立判断它是否得到当前 block 的支持。

【输入】
- block.section_path：原文所在章节路径；
- block.text：本次唯一可用的原文；
- leaves：叶节点数组。每个 leaf 包含 leaf_id、path、title、description、exclusions。

必须逐个检查 leaves，不得因为其他叶节点的判断结果改变当前叶节点的判断。以下单叶
判断规则对每个 leaf 原样适用。

{semantic_rules}
【输出】
只输出一个 JSON 对象，不得输出 Markdown 或额外说明。

decisions 数组必须覆盖输入 leaves 中的每个 leaf_id，且每个 leaf_id 恰好出现一次。
不得只返回支持项，也不得输出输入中不存在的 leaf_id。每个 decision 的 leaf_id 必须是
该 decision 的第一个字段。

支持时，不输出该 decision 的 reason，格式为：
{{
  "leaf_id": "输入 leaves 中的 leaf_id",
  "is_supported": true,
  "evidence": [
    {{
      "quote": "block.text 中的连续逐字原文",
      "support_types": ["capability", "instance"],
      "reason": "不超过80字，说明该证据如何构成数组中的每一种类型"
    }}
  ]
}}

不支持时，输出该 decision 的 reason，evidence 为空数组，格式为：
{{
  "leaf_id": "输入 leaves 中的 leaf_id",
  "reason": "不超过120字的判断理由",
  "is_supported": false,
  "evidence": []
}}

最终输出格式：
{{
  "decisions": [
    {{"leaf_id": "第一个 leaf", "is_supported": true, "evidence": []}},
    {{"leaf_id": "第二个 leaf", "reason": "判断理由", "is_supported": false, "evidence": []}}
  ]
}}
"""


BATCH_SYSTEM_PROMPT = build_batch_system_prompt()
PROBE_MAP = {
    probe.leaf_id: probe
    for probe in (
        *PROBES,
        *(
            ProbeSpec(
                probe.leaf_id,
                probe.leaf_id,
                probe.expected_supported,
                (),
                SUPPORT_TYPES if probe.expected_supported else (),
                probe.rationale,
            )
            for probe in EXPANDED_PROBES
        ),
        *ADDITIONAL_PROBES,
        *BATCH_48_PROBES,
    )
}
PROBE_SETS = {
    "baseline-12": tuple(PROBE_MAP[leaf_id] for leaf_id in BASELINE_LEAF_IDS),
    "expanded-24": tuple(PROBE_MAP[leaf_id] for leaf_id in EXPANDED_LEAF_IDS),
    "expanded-36": tuple(PROBE_MAP[leaf_id] for leaf_id in EXPANDED_36_LEAF_IDS),
    "batch-48-new-12": tuple(BATCH_48_PROBES),
    "expanded-48": tuple(PROBE_MAP[leaf_id] for leaf_id in EXPANDED_48_LEAF_IDS),
    "mixed-k08-k16-10": MIXED_K08_K16_PROBES,
    "p01-b014-original": P01_B014_ORIGINAL_PROBES,
    "p01-b014-reordered": P01_B014_REORDERED_PROBES,
}


def build_messages(
    block: dict[str, Any],
    probes: tuple[ProbeSpec, ...],
    leaves: dict[str, dict[str, Any]],
) -> tuple[ChatMessage, ...]:
    """构造一个 block 加多个完整叶节点的用户消息。"""

    payload = {
        "block": {key: value for key, value in block.items() if key != "block_id"},
        "leaves": [leaves[probe.leaf_id] for probe in probes],
    }
    return (
        ChatMessage(role="system", content=BATCH_SYSTEM_PROMPT),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False, indent=2)),
    )


def parse_batch(
    content: str,
    *,
    block_text: str,
    probes: tuple[ProbeSpec, ...],
) -> dict[str, Any]:
    """解析批量 JSON，并复用 E-1 的单叶校验器逐项检查。"""

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
            "top_level_ok": False,
        }

    decisions = parsed.get("decisions")
    if not isinstance(decisions, list):
        return {
            "parse_ok": True,
            "parsed": parsed,
            "valid": False,
            "top_level_ok": False,
        }

    expected_ids = [probe.leaf_id for probe in probes]
    expected_id_set = set(expected_ids)
    returned_ids: list[str] = []
    decision_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    for decision in decisions:
        leaf_id = decision.get("leaf_id") if isinstance(decision, dict) else None
        if not isinstance(leaf_id, str):
            continue
        if leaf_id in decision_by_id:
            duplicate_ids.append(leaf_id)
            continue
        decision_by_id[leaf_id] = decision
        returned_ids.append(leaf_id)

    returned_id_set = set(returned_ids)
    missing_ids = sorted(expected_id_set - returned_id_set)
    extra_ids = sorted(returned_id_set - expected_id_set)
    coverage_ok = not missing_ids and not extra_ids and not duplicate_ids
    leaf_validations: dict[str, dict[str, Any]] = {}
    for probe in probes:
        decision = decision_by_id.get(probe.leaf_id)
        if decision is None:
            leaf_validations[probe.leaf_id] = {
                "parse_ok": False,
                "parse_error": "decision missing",
                "valid": False,
            }
            continue
        leaf_payload = {key: value for key, value in decision.items() if key != "leaf_id"}
        leaf_validations[probe.leaf_id] = parse_and_validate(
            json.dumps(leaf_payload, ensure_ascii=False),
            block_text=block_text,
            expected_supported=probe.expected_supported,
            required_types=probe.required_types,
            allowed_extra_types=probe.allowed_extra_types,
        )

    all_leaf_valid = all(item["valid"] for item in leaf_validations.values())
    all_binary_match = all(item.get("binary_match") is True for item in leaf_validations.values())
    return {
        "parse_ok": True,
        "parsed": parsed,
        "valid": coverage_ok and all_leaf_valid,
        "top_level_ok": True,
        "coverage_ok": coverage_ok,
        "duplicate_ids": sorted(duplicate_ids),
        "missing_ids": missing_ids,
        "extra_ids": extra_ids,
        "all_leaf_valid": all_leaf_valid,
        "all_binary_match": all_binary_match,
        "leaves": leaf_validations,
    }


async def run_one(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    run_index: int,
    block: dict[str, Any],
    probes: tuple[ProbeSpec, ...],
    leaves: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """执行一次当前叶节点集合的批量判定，并保留完整输入输出。"""

    messages = build_messages(block, probes, leaves)
    started_at = datetime.now(UTC).isoformat()
    async with semaphore:
        try:
            result = await provider.complete(messages, max_output_tokens=MAX_OUTPUT_TOKENS)
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

    validation = parse_batch(result.content, block_text=block["text"], probes=probes)
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


def summarize(results: list[dict[str, Any]], probes: tuple[ProbeSpec, ...]) -> dict[str, Any]:
    """按 run 与叶节点统计批量判定的确定性结果。"""

    per_leaf: dict[str, dict[str, Any]] = {}
    for probe in probes:
        rows = [
            result["validation"]["leaves"][probe.leaf_id]
            for result in results
            if result.get("validation", {}).get("parse_ok")
            and probe.leaf_id in result["validation"].get("leaves", {})
        ]
        per_leaf[probe.leaf_id] = {
            "expected_supported": probe.expected_supported,
            "decisions": len(rows),
            "valid": sum(item.get("valid") is True for item in rows),
            "binary_match": sum(item.get("binary_match") is True for item in rows),
            "support_contract_pass": sum(
                item.get("support_contract_pass") is True for item in rows
            ),
            "legacy_type_contract_pass": sum(
                item.get("legacy_type_contract_pass") is True for item in rows
            ),
        }

    completed = [result for result in results if "validation" in result]
    parsed = [result for result in completed if result["validation"].get("parse_ok")]
    return {
        "calls": len(results),
        "completed": len(completed),
        "parsed": len(parsed),
        "batch_valid": sum(result["validation"].get("valid") is True for result in parsed),
        "all_leaf_binary_match_runs": sum(
            result["validation"].get("all_binary_match") is True for result in parsed
        ),
        "input_tokens": sum(result.get("input_tokens", 0) for result in results),
        "output_tokens": sum(result.get("output_tokens", 0) for result in results),
        "per_leaf": per_leaf,
    }


def parse_args() -> argparse.Namespace:
    """解析回放次数、并发和 thinking 配置。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-set",
        choices=tuple(PROBE_SETS),
        default="baseline-12",
        help="本次批量回放使用的叶节点探针集",
    )
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
    """加载固定输入，执行批量回放并保存可追溯产物。"""

    args = parse_args()
    selected_probes = PROBE_SETS[args.probe_set]
    selected_leaf_ids = tuple(probe.leaf_id for probe in selected_probes)
    forest = load_json(FOREST_PATH)
    leaves = load_leaves(forest)
    missing_leaf_ids = [leaf_id for leaf_id in selected_leaf_ids if leaf_id not in leaves]
    if missing_leaf_ids:
        raise RuntimeError(f"batch leaves missing from forest: {missing_leaf_ids}")
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
                run_one(provider, semaphore, run_index, block, selected_probes, leaves)
                for run_index in range(1, args.runs + 1)
            )
        ),
        key=lambda row: row["run_index"],
    )
    summary = summarize(results, selected_probes)
    artifact = {
        "experiment": "E-3 batch leaf support attachment",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "forest_path": str(FOREST_PATH),
        "block_set_path": str(BLOCK_SET_PATH),
        "block_id": block["block_id"],
        "model": MODEL_ID,
        "thinking": "enabled" if args.thinking else "disabled",
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "run_count": args.runs,
        "max_concurrency": args.max_concurrency,
        "probe_set": args.probe_set,
        "leaf_ids": list(selected_leaf_ids),
        "system_prompt": BATCH_SYSTEM_PROMPT,
        "system_prompt_sha256": hashlib.sha256(BATCH_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "support_types": list(SUPPORT_TYPES),
        "probes": [
            {
                "leaf_id": probe.leaf_id,
                "leaf": leaves[probe.leaf_id],
                "expected_supported": probe.expected_supported,
                "required_types": list(probe.required_types),
                "allowed_extra_types": list(probe.allowed_extra_types),
                "gold_rationale": probe.rationale,
            }
            for probe in selected_probes
        ],
        "summary": summary,
        "results": results,
    }
    result_path = run_dir / "results.json"
    result_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"run_dir": str(run_dir), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
