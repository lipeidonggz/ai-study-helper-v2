"""运行单 block、单叶节点的证据支持判定实验。"""

# 提示词必须保持已回放文本逐字不变，长行不能重排；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import unicodedata
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

FOREST_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_tree_expansion_batch_20261123T113731009302Z"
    / "role_knowledge_forest.json"
)
SNAPSHOT_ROOT = (
    Path(os.environ["LOCALAPPDATA"])
    / "com.lipeidong.aistudyhelper"
    / "collections"
    / "s_6b787258ba7d700c"
)
BLOCK_SET_PATH = (
    SNAPSHOT_ROOT / "documents" / "doc_fd4dec9976e041308e2b39ed797c75d1" / "v0001" / "blocks.json"
)
BLOCK_ID = "doc_fd4dec9976e041308e2b39ed797c75d1#v0001#c1#b00008"
OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "leaf_support_probe"
MODEL_ID = "deepseek-flash"
DEFAULT_RUN_COUNT = 3
MAX_CONCURRENCY = 12
MAX_OUTPUT_TOKENS = 8192

SUPPORT_TYPES = (
    "definition",
    "structure",
    "capability",
    "mechanism",
    "condition",
    "method",
    "instance",
    "effect",
    "evaluation",
)

PROMPT_SECTIONS = (
    "任务",
    "输入",
    "判断方法",
    "输出",
)

SYSTEM_PROMPT = """\
你是知识库证据挂接判定器。每次只判断一个 block 是否能为一个知识节点leaf提供支持。

【任务】

判断 block.text 中是否存在可以支持 leaf 所要求知识的证据。

如果存在，给出逐字证据、对应的支持类型和判断理由。

为便于表述，下文用“当前知识节点”指代输入的 leaf。

【输入】

你会收到一个 block 和一个 leaf，各个字段含义如下：

- block.section_path：原文所在章节路径，只提供语境帮助理解原文位置；
- block.text：本次唯一可用的原文；

* leaf.path 给出当前知识节点在知识框架中的层级位置；
* leaf.title 给出当前知识节点的名称；
* leaf.path 和 leaf.title 共同说明本次判断针对哪个知识节点；
* leaf.description 说明该知识节点应涵盖的知识范围和可能涉及的方面，可能包括但不限于边界、条件、组成、作用、实例或评价范围等；
* leaf.exclusions 列出明确不属于当前知识节点的相邻内容。

【判断方法】

1. 从block.text中提取支持原文quote：即block.text 中一组连续逐字原文，可以由一个句子组成，也可以由多个连续句子组成
2. 从leaf中提取支持目标target：target默认为当前知识节点整体，也可以聚焦leaf.description中的某个具体方面；但target必须保留leaf.path和leaf.title规定的核心知识身份及其关系限定，不得把该方面从所属关系中剥离，改写成关系更弱的独立目标。并列项和“等”表示范围示例，不要求全部覆盖
3. 同时满足如下三个条件，判为支持，否则判为不支持：
（1）quote 的核心对象、关系或职责归属不落入 leaf.exclusions；
（2）任一quote以下面【支持类型列表】中的任一一种支持任意一个或多个目标target；
4. 每条 evidence 只对应一个 target；若同一 quote 支持多个 target，分别输出多个 evidence。
5. 允许且仅允许使用支持类型列表support_types如下：

* definition：quote为target提供定义；
* structure：quote说明target内部由哪些部分、角色或层级构成，以及这些部分的组成关系；
* capability：quote说明target能执行什么动作、提供什么功能，或能对其他对象实施什么允许、拒绝和限制规则；
* mechanism：quote说明target借助什么作用过程、因果链条或工作原理产生功能或变化；
* condition：quote说明target之外的某个事实必须先成立，target所包含的知识才能成立、运行或被使用；
* method：quote说明建立、操作、使用或处理target所采用的方法、步骤、策略或流程；
* instance：quote说明target的具体实例、案例、实现、产品或场景；
* effect：quote说明target产生、导致、避免或改变的结果、变化或影响；
* evaluation：quote评价target，可使用指标、判据、标准、权衡或优缺点结论等。

【输出】

只输出一个 JSON 对象，不得输出 Markdown 或额外说明，格式如下，不支持时，evidence为空数组:

{

"evidence": [

{

"quote": "block.text 中的连续逐字原文",

"support_types": ["capability", "instance"],

"target": "从leaf中按照前述方法提取的能被quote支持的target",

"reason": "不超过80字，说明该quote如何支持target"

}

]
}"""


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    """一个人工审核过的叶节点探针及其金标准。"""

    probe_id: str
    leaf_id: str
    expected_supported: bool
    required_types: tuple[str, ...]
    allowed_extra_types: tuple[str, ...]
    rationale: str


PROBES = (
    ProbeSpec(
        "P1",
        "K08.05.01",
        True,
        ("capability",),
        (
            "definition",
            "structure",
            "mechanism",
            "condition",
            "method",
            "instance",
            "effect",
            "evaluation",
        ),
        "沙箱访问控制必须以 capability 识别；其他陈述若分别构成前提、实现、案例或效果，可归入对应类型。",
    ),
    ProbeSpec(
        "P2",
        "K08.05.03",
        True,
        ("condition",),
        ("mechanism", "instance"),
        "原文具体限制工作区写入和网络访问，属于资源隔离执行边界的一个方面。",
    ),
    ProbeSpec(
        "P3",
        "K18.03.01",
        True,
        ("method",),
        ("instance",),
        "人在回路加上按动作授权审批，是监督模式的一个具体方法实例。",
    ),
    ProbeSpec(
        "P4",
        "K18.04.01",
        True,
        ("instance",),
        ("evaluation",),
        "原文说明允许对话框、审批对象和审批疲劳效果，是审批交互的具体实例。",
    ),
    ProbeSpec(
        "P5",
        "K18.01.02",
        True,
        ("condition",),
        ("instance",),
        "读取由 Agent 执行，写入、bash 和网络访问由人类审批，构成权限划分。",
    ),
    ProbeSpec(
        "P6",
        "K16.07.02",
        True,
        ("instance",),
        ("condition",),
        "原文把审批作为人类监督机制，并指出监督疲劳可能削弱控制效果。",
    ),
    ProbeSpec(
        "N1",
        "K09.03.01",
        False,
        (),
        (),
        "原文没有容器镜像、容器运行时或 Kubernetes 资源组织内容。",
    ),
    ProbeSpec(
        "N2",
        "K12.07.02",
        False,
        (),
        (),
        "原文是审批疲劳，不是生产监控告警、异常检测或告警疲劳。",
    ),
    ProbeSpec(
        "N3",
        "K03.06.03",
        False,
        (),
        (),
        "原文描述运行时审批，不涉及规划阶段的约束识别与方案选择。",
    ),
    ProbeSpec(
        "N4",
        "K23.01.01",
        False,
        (),
        (),
        "原文只有边界可审计的表述，没有审计事件定义、构成要素或日志边界。",
    ),
    ProbeSpec(
        "N5",
        "K18.04.02",
        False,
        (),
        (),
        "人工审批不等于人类接管、主动中断、覆盖决策或紧急停止。",
    ),
    ProbeSpec(
        "N6",
        "K18.03.02",
        True,
        ("instance",),
        ("condition", "mechanism", "method", "effect", "evaluation"),
        "“allow this”对话框请求人类评估Agent操作及其风险，构成对工具调用的在环审查请求和具体交互实例。",
    ),
)


def load_json(path: Path) -> Any:
    """读取 UTF-8 JSON 文件。"""

    return json.loads(path.read_text(encoding="utf-8"))


def load_leaves(forest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把知识框架压平成叶节点映射，并保留完整路径。"""

    leaves: dict[str, dict[str, Any]] = {}

    def walk(nodes: list[dict[str, Any]], path: list[str]) -> None:
        for node in nodes:
            next_path = [*path, node["title"]]
            children = node.get("children") or []
            if not children:
                leaves[node["node_id"]] = {
                    "leaf_id": node["node_id"],
                    "path": next_path,
                    "title": node["title"],
                    "description": node["description"],
                    "exclusions": node.get("exclusions") or [],
                }
                continue
            walk(children, next_path)

    walk(forest["roots"], [])
    return leaves


def load_external_leaf_probes(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], tuple[ProbeSpec, ...]]:
    """读取知识森林外的临时叶节点，保持挂接提示词和输入结构不变。"""

    payload = load_json(path)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("external leaf input must be a non-empty JSON array")

    leaves: dict[str, dict[str, Any]] = {}
    probes: list[ProbeSpec] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise TypeError(f"external leaf #{index} must be an object")

        leaf_id = item.get("leaf_id")
        path_parts = item.get("path")
        title = item.get("title")
        description = item.get("description")
        exclusions = item.get("exclusions")
        tree_context = item.get("tree_context")
        expected_supported = item.get("expected_supported")
        if not isinstance(leaf_id, str) or not leaf_id.strip():
            raise RuntimeError(f"external leaf #{index} has an invalid leaf_id")
        if (
            not isinstance(path_parts, list)
            or not path_parts
            or not all(isinstance(part, str) and part.strip() for part in path_parts)
            or path_parts[-1] != title
        ):
            raise RuntimeError(f"external leaf {leaf_id} has an invalid path")
        if not isinstance(title, str) or not title.strip():
            raise RuntimeError(f"external leaf {leaf_id} has an invalid title")
        if not isinstance(description, str) or not description.strip():
            raise RuntimeError(f"external leaf {leaf_id} has an invalid description")
        if not isinstance(exclusions, list) or not all(
            isinstance(exclusion, str) for exclusion in exclusions
        ):
            raise RuntimeError(f"external leaf {leaf_id} has invalid exclusions")
        if tree_context is not None and not isinstance(tree_context, dict):
            raise TypeError(f"external leaf {leaf_id} has invalid tree_context")
        if not isinstance(expected_supported, bool):
            raise TypeError(f"external leaf {leaf_id} has an invalid expected_supported")
        if leaf_id in leaves:
            raise RuntimeError(f"duplicate external leaf: {leaf_id}")

        leaf_payload = {
            "leaf_id": leaf_id,
            "path": path_parts,
            "title": title,
            "description": description,
            "exclusions": exclusions,
        }
        if tree_context is not None:
            leaf_payload["tree_context"] = tree_context
        leaves[leaf_id] = leaf_payload
        probes.append(
            ProbeSpec(
                f"X-{leaf_id}",
                leaf_id,
                expected_supported,
                (),
                SUPPORT_TYPES if expected_supported else (),
                "外部临时叶节点回归探针；金标准由命令行输入显式提供。",
            )
        )

    return leaves, tuple(probes)


def load_block(
    block_set_path: Path = BLOCK_SET_PATH,
    block_id: str = BLOCK_ID,
) -> dict[str, Any]:
    """从 collection 快照读取指定 block；默认保持既有 b00008 探针输入。"""

    block_set = load_json(block_set_path)
    for block in block_set["blocks"]:
        if block["block_id"] == block_id:
            return {
                "block_id": block["block_id"],
                "section_path": block["section_path"],
                "text": block["text"],
            }
    raise RuntimeError(f"test block not found: {block_id}")


def build_messages(
    block: dict[str, Any],
    leaf: dict[str, Any],
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[ChatMessage, ...]:
    """构造只包含一个 block、一个叶节点和可追溯系统提示词的消息。"""

    payload = {
        "block": {key: value for key, value in block.items() if key != "block_id"},
        "leaf": {key: value for key, value in leaf.items() if key != "leaf_id"},
    }
    return (
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False, indent=2)),
    )


def normalize_quote_for_grounding(text: str) -> str:
    """归一化 quote 的排版差异，同时保留连续原文的可定位性。"""

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.translate(
        str.maketrans(
            {
                "“": '"',
                "”": '"',
                "‘": "'",
                "’": "'",
                "—": "-",
                "–": "-",
                "…": "...",
                "\u00a0": " ",
            }
        )
    )
    return re.sub(r"\s+", " ", normalized).strip()


def is_quote_grounded_in_block(quote: object, block_text: str) -> tuple[bool, bool]:
    """判断 quote 是否逐字或经排版归一化后仍是 block 的连续子串。"""

    if not isinstance(quote, str):
        return False, False
    exact_substring = quote in block_text
    normalized_substring = normalize_quote_for_grounding(quote) in normalize_quote_for_grounding(
        block_text
    )
    return exact_substring, normalized_substring


def parse_and_validate(
    content: str,
    *,
    block_text: str,
    expected_supported: bool,
    required_types: tuple[str, ...],
    allowed_extra_types: tuple[str, ...],
    allowed_support_types: tuple[str, ...] = SUPPORT_TYPES,
    require_quote_grounding: bool = True,
) -> dict[str, Any]:
    """解析模型 JSON，并执行宿主可确定检查。

    新响应契约以 ``evidence`` 是否为非空数组直接决定支持关系，避免模型同时输出
    证据和布尔结论后产生自相矛盾。旧响应中的 ``is_supported`` 仍继续兼容，供既有
    实验产物和批量探针回放使用。
    """

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        return {
            "parse_ok": False,
            "parse_error": f"{type(error).__name__}: {error}",
            "valid": False,
        }

    if not isinstance(parsed, dict):
        return {
            "parse_ok": True,
            "parsed": parsed,
            "valid": False,
            "keys_ok": False,
        }

    evidence = parsed.get("evidence")
    evidence_array_ok = isinstance(evidence, list)
    parsed_keys = set(parsed)
    if "is_supported" not in parsed:
        response_contract = "evidence_presence"
        required_keys = {"evidence"}
        allowed_keys = {"evidence"}
        supported = bool(evidence) if evidence_array_ok else None
        outer_reason_ok = True
    else:
        response_contract = "explicit_support_flag"
        supported = parsed.get("is_supported")
        if supported is True:
            required_keys = {"is_supported", "evidence"}
            allowed_keys = {"is_supported", "evidence", "reason"}
            outer_reason_ok = True
        elif supported is False:
            required_keys = {"reason", "is_supported", "evidence"}
            allowed_keys = {"reason", "is_supported", "evidence"}
            outer_reason = parsed.get("reason")
            outer_reason_ok = isinstance(outer_reason, str) and bool(outer_reason.strip())
        else:
            required_keys = set()
            allowed_keys = set()
            outer_reason_ok = False

    # 新契约只允许 evidence；旧布尔契约中支持结果偶尔附带的外层 reason 继续忽略，
    # 以保留历史产物和批量探针的兼容性。
    keys_ok = bool(required_keys) and required_keys <= parsed_keys and parsed_keys <= allowed_keys
    # target 只作为解释性 trace 保存，宿主不据此改变挂接语义。
    evidence_items_ok = (
        evidence_array_ok
        and all(
            isinstance(item, dict)
            and set(item) == {"quote", "support_types", "target", "reason"}
            and isinstance(item.get("quote"), str)
            and bool(item["quote"])
            and isinstance(item.get("support_types"), list)
            and bool(item["support_types"])
            and all(
                isinstance(support_type, str) and support_type in allowed_support_types
                for support_type in item["support_types"]
            )
            and len(item["support_types"]) == len(set(item["support_types"]))
            and isinstance(item.get("target"), str)
            and bool(item["target"].strip())
            and isinstance(item.get("reason"), str)
            and bool(item["reason"].strip())
            for item in evidence
        )
        if evidence_array_ok
        else False
    )
    if not evidence_array_ok:
        quote_check = []
    elif require_quote_grounding:
        quote_check = [
            {
                "quote": item.get("quote"),
                "exact_substring": (
                    exact_substring if isinstance(item.get("quote"), str) else False
                ),
                "normalized_substring": (
                    normalized_substring if isinstance(item.get("quote"), str) else False
                ),
                "grounding_checked": True,
            }
            for item in evidence
            if isinstance(item, dict)
            for exact_substring, normalized_substring in [
                is_quote_grounded_in_block(item.get("quote", ""), block_text)
            ]
        ]
    else:
        quote_check = [
            {
                "quote": item.get("quote"),
                "grounding_checked": False,
            }
            for item in evidence
            if isinstance(item, dict)
        ]
    quotes_ok = evidence_items_ok and (
        not require_quote_grounding or all(item.get("normalized_substring") for item in quote_check)
    )
    returned_types = (
        {
            support_type
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("support_types"), list)
            for support_type in item["support_types"]
            if isinstance(support_type, str)
        }
        if evidence_array_ok
        else set()
    )
    payload_ok = keys_ok and outer_reason_ok and evidence_items_ok and isinstance(supported, bool)

    binary_match = supported is expected_supported
    required_set = set(required_types)
    allowed_set = required_set | set(allowed_extra_types)
    type_required_pass = not expected_supported or required_set <= returned_types
    type_unexpected = sorted(returned_types - allowed_set) if expected_supported else []
    type_allowed_pass = not type_unexpected
    valid = keys_ok and outer_reason_ok and evidence_items_ok and payload_ok and quotes_ok
    support_contract_pass = valid and binary_match
    legacy_type_contract_pass = valid and binary_match and type_required_pass and type_allowed_pass

    return {
        "parse_ok": True,
        "parsed": parsed,
        "valid": valid,
        "response_contract": response_contract,
        "derived_supported": supported,
        "keys_ok": keys_ok,
        "outer_reason_ok": outer_reason_ok,
        "evidence_array_ok": evidence_array_ok,
        "evidence_items_ok": evidence_items_ok,
        "quote_check": quote_check,
        "quotes_ok": quotes_ok,
        "returned_types": sorted(returned_types),
        "payload_ok": payload_ok,
        "binary_match": binary_match,
        "type_required_pass": type_required_pass,
        "type_unexpected": type_unexpected,
        "type_allowed_pass": type_allowed_pass,
        "support_contract_pass": support_contract_pass,
        "legacy_type_contract_pass": legacy_type_contract_pass,
    }


async def run_one(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    probe: ProbeSpec,
    run_index: int,
    block: dict[str, Any],
    leaf: dict[str, Any],
    system_prompt: str,
    allowed_support_types: tuple[str, ...],
    require_quote_grounding: bool,
) -> dict[str, Any]:
    """执行一次独立判定，并保留输入、输出和检查结果。"""

    messages = build_messages(block, leaf, system_prompt)
    started_at = datetime.now(UTC).isoformat()
    async with semaphore:
        try:
            result = await provider.complete(messages, max_output_tokens=MAX_OUTPUT_TOKENS)
        except ChatProviderError as error:  # 实验保留单次失败，不做静默重试。
            return {
                "probe_id": probe.probe_id,
                "run_index": run_index,
                "leaf_id": probe.leaf_id,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "messages": [
                    {"role": message.role, "content": message.content} for message in messages
                ],
                "error": f"{type(error).__name__}: {error}",
            }

    validation = parse_and_validate(
        result.content,
        block_text=block["text"],
        expected_supported=probe.expected_supported,
        required_types=probe.required_types,
        allowed_extra_types=probe.allowed_extra_types,
        allowed_support_types=allowed_support_types,
        require_quote_grounding=require_quote_grounding,
    )
    return {
        "probe_id": probe.probe_id,
        "run_index": run_index,
        "leaf_id": probe.leaf_id,
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
    probes: tuple[ProbeSpec, ...],
    allowed_support_types: tuple[str, ...],
) -> dict[str, Any]:
    """计算确定性执行指标；产品语义仍由负责人逐项审核。"""

    completed = [result for result in results if "validation" in result]
    parsed = [result for result in completed if result["validation"]["parse_ok"]]
    probe_summary: dict[str, dict[str, Any]] = {}
    for probe in probes:
        rows = [result for result in results if result["probe_id"] == probe.probe_id]
        valid_rows = [row for row in rows if row.get("validation", {}).get("valid")]
        binary_rows = [row for row in rows if row.get("validation", {}).get("binary_match") is True]
        support_rows = [
            row for row in rows if row.get("validation", {}).get("support_contract_pass") is True
        ]
        type_rows = [
            row
            for row in rows
            if row.get("validation", {}).get("legacy_type_contract_pass") is True
        ]
        probe_summary[probe.probe_id] = {
            "leaf_id": probe.leaf_id,
            "expected_supported": probe.expected_supported,
            "required_types": list(probe.required_types),
            "allowed_extra_types": [
                support_type
                for support_type in probe.allowed_extra_types
                if support_type in allowed_support_types
            ],
            "calls": len(rows),
            "valid": len(valid_rows),
            "binary_match": len(binary_rows),
            "support_contract_pass": len(support_rows),
            "legacy_type_contract_pass": len(type_rows),
        }

    return {
        "calls": len(results),
        "completed": len(completed),
        "parsed": len(parsed),
        "host_valid": sum(bool(result.get("validation", {}).get("valid")) for result in results),
        "binary_match": sum(
            bool(result.get("validation", {}).get("binary_match")) for result in results
        ),
        "support_contract_pass": sum(
            bool(result.get("validation", {}).get("support_contract_pass")) for result in results
        ),
        "legacy_type_contract_pass": sum(
            bool(result.get("validation", {}).get("legacy_type_contract_pass"))
            for result in results
        ),
        "input_tokens": sum(result.get("input_tokens", 0) for result in results),
        "output_tokens": sum(result.get("output_tokens", 0) for result in results),
        "probes": probe_summary,
    }


def parse_args() -> argparse.Namespace:
    """解析本次回放选择的探针、次数和并发数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probes",
        default=",".join(probe.probe_id for probe in PROBES),
        help="逗号分隔的探针 ID，默认执行全部探针",
    )
    parser.add_argument("--runs", type=int, default=DEFAULT_RUN_COUNT, help="每个探针运行次数")
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=MAX_CONCURRENCY,
        help="本次运行的最大并发调用数",
    )
    parser.add_argument(
        "--block-id",
        default=BLOCK_ID,
        help="待判定的 block ID；默认保持既有 b00008 探针",
    )
    parser.add_argument(
        "--block-set-path",
        type=Path,
        default=BLOCK_SET_PATH,
        help="包含目标 block 的 blocks.json；默认保持既有 b00008 快照路径",
    )
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="开启模型 thinking；默认关闭",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("low", "high", "max"),
        default=None,
        help="thinking 推理强度；不传时使用供应商默认值，必须与 --thinking 同时使用",
    )
    parser.add_argument(
        "--extra-supported-leaf-id",
        action="append",
        default=[],
        metavar="LEAF_ID",
        help="为临时回归追加一个预期支持的叶节点；可重复指定",
    )
    parser.add_argument(
        "--extra-unsupported-leaf-id",
        action="append",
        default=[],
        metavar="LEAF_ID",
        help="为临时回归追加一个预期不支持的叶节点；可重复指定",
    )
    parser.add_argument(
        "--system-prompt-artifact",
        type=Path,
        help="从既有结果 JSON 复用完整 system_prompt，用于固定输入下的提示词回放",
    )
    parser.add_argument(
        "--external-leaf-input",
        type=Path,
        help="知识森林外临时叶节点的 JSON 数组，用于验证候选拆分结果",
    )
    parser.add_argument(
        "--allow-fixed-leaf-overlap",
        action="store_true",
        help="允许外部回放覆盖与固定探针同 ID 的叶节点，用于全量回归",
    )
    parser.add_argument(
        "--omit-support-type",
        action="append",
        choices=SUPPORT_TYPES,
        default=[],
        metavar="TYPE",
        help="实验时从提示词和宿主校验中临时移除一个支持类型；可重复指定",
    )
    parser.add_argument(
        "--omit-prompt-section",
        action="append",
        choices=PROMPT_SECTIONS,
        default=[],
        metavar="SECTION",
        help="实验时从提示词中确定性移除一个完整段落；可重复指定",
    )
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be positive")
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    if args.reasoning_effort is not None and not args.thinking:
        parser.error("--reasoning-effort requires --thinking")
    return args


def apply_support_type_omissions(
    system_prompt: str,
    omitted_types: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    """从提示词和宿主校验中同步移除指定支持类型，供受限类型诊断使用。"""

    if not omitted_types:
        return system_prompt, SUPPORT_TYPES

    omitted_set = set(omitted_types)
    active_types = tuple(
        support_type for support_type in SUPPORT_TYPES if support_type not in omitted_set
    )
    if not active_types:
        raise RuntimeError("at least one support type must remain enabled")

    prompt_lines = [
        line
        for line in system_prompt.splitlines()
        if not any(
            line.strip().startswith((f"- {support_type}：", f"* {support_type}："))
            for support_type in omitted_set
        )
    ]
    restricted_prompt = "\n".join(prompt_lines) + "\n"
    restricted_prompt = re.sub(
        r"[一二三四五六七八九十]+类支持类型",
        "上方列出的支持类型",
        restricted_prompt,
    )
    return restricted_prompt, active_types


def apply_prompt_section_omissions(
    system_prompt: str,
    omitted_sections: tuple[str, ...],
) -> str:
    """从提示词中移除指定标题段落，并保留其余内容逐字不变。"""

    restricted_prompt = system_prompt
    for section in omitted_sections:
        marker = f"【{section}】"
        section_start = restricted_prompt.find(marker)
        if section_start < 0:
            raise RuntimeError(f"prompt section not found: {section}")

        next_section_offset = restricted_prompt.find(
            "\n【",
            section_start + len(marker),
        )
        section_end = len(restricted_prompt) if next_section_offset < 0 else next_section_offset + 1
        restricted_prompt = restricted_prompt[:section_start] + restricted_prompt[section_end:]
    return restricted_prompt


async def main() -> None:
    """加载固定输入，执行选择的探针并保存可追溯产物。"""

    args = parse_args()
    system_prompt = SYSTEM_PROMPT
    if args.system_prompt_artifact:
        prompt_artifact = load_json(args.system_prompt_artifact)
        system_prompt = prompt_artifact.get("system_prompt")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise RuntimeError("system prompt artifact does not contain a valid system_prompt")
    system_prompt = apply_prompt_section_omissions(
        system_prompt,
        tuple(args.omit_prompt_section),
    )
    system_prompt, active_support_types = apply_support_type_omissions(
        system_prompt,
        tuple(args.omit_support_type),
    )
    extra_probe_inputs = (
        *((leaf_id, True) for leaf_id in args.extra_supported_leaf_id),
        *((leaf_id, False) for leaf_id in args.extra_unsupported_leaf_id),
    )
    extra_leaf_ids = [leaf_id for leaf_id, _ in extra_probe_inputs]
    duplicate_extra_leaf_ids = {
        leaf_id for leaf_id in extra_leaf_ids if extra_leaf_ids.count(leaf_id) > 1
    }
    fixed_leaf_ids = {probe.leaf_id for probe in PROBES}
    conflicting_leaf_ids = fixed_leaf_ids & set(extra_leaf_ids)
    if duplicate_extra_leaf_ids or conflicting_leaf_ids:
        raise RuntimeError(
            "invalid extra probe leaves: "
            f"duplicates={sorted(duplicate_extra_leaf_ids)}, "
            f"fixed={sorted(conflicting_leaf_ids)}"
        )
    extra_probes = tuple(
        ProbeSpec(
            f"X-{leaf_id}",
            leaf_id,
            expected_supported,
            (),
            SUPPORT_TYPES if expected_supported else (),
            "命令行临时回归探针；不进入固定人工复核表。",
        )
        for leaf_id, expected_supported in extra_probe_inputs
    )
    external_leaves: dict[str, dict[str, Any]] = {}
    external_probes: tuple[ProbeSpec, ...] = ()
    if args.external_leaf_input:
        external_leaves, external_probes = load_external_leaf_probes(args.external_leaf_input)
    all_extra_leaf_ids = [probe.leaf_id for probe in (*extra_probes, *external_probes)]
    duplicate_all_extra_leaf_ids = {
        leaf_id for leaf_id in all_extra_leaf_ids if all_extra_leaf_ids.count(leaf_id) > 1
    }
    conflicting_all_extra_leaf_ids = fixed_leaf_ids & set(all_extra_leaf_ids)
    if duplicate_all_extra_leaf_ids or (
        conflicting_all_extra_leaf_ids and not args.allow_fixed_leaf_overlap
    ):
        raise RuntimeError(
            "invalid external or extra probe leaves: "
            f"duplicates={sorted(duplicate_all_extra_leaf_ids)}, "
            f"fixed={sorted(conflicting_all_extra_leaf_ids)}"
        )
    available_probes = (*PROBES, *extra_probes, *external_probes)
    probe_map = {probe.probe_id: probe for probe in available_probes}
    probe_ids = tuple(item.strip() for item in args.probes.split(",") if item.strip())
    unknown_probe_ids = [probe_id for probe_id in probe_ids if probe_id not in probe_map]
    if not probe_ids or unknown_probe_ids:
        raise RuntimeError(f"unknown or empty probe selection: {unknown_probe_ids}")
    selected_probes = tuple(probe_map[probe_id] for probe_id in probe_ids)
    omitted_required_types = {
        support_type
        for probe in selected_probes
        for support_type in probe.required_types
        if support_type not in active_support_types
    }
    if omitted_required_types:
        raise RuntimeError(
            "cannot omit support types required by selected probes: "
            f"{sorted(omitted_required_types)}"
        )
    forest = load_json(FOREST_PATH)
    leaves = load_leaves(forest)
    leaves.update(external_leaves)
    missing_leaf_ids = [probe.leaf_id for probe in selected_probes if probe.leaf_id not in leaves]
    if missing_leaf_ids:
        raise RuntimeError(f"probe leaves missing from forest: {missing_leaf_ids}")
    block = load_block(args.block_set_path, args.block_id)

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
            reasoning_effort=args.reasoning_effort,
        ),
    )

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    semaphore = asyncio.Semaphore(args.max_concurrency)
    tasks = [
        run_one(
            provider,
            semaphore,
            probe,
            run_index,
            block,
            leaves[probe.leaf_id],
            system_prompt,
            active_support_types,
            not args.thinking,
        )
        for probe in selected_probes
        for run_index in range(1, args.runs + 1)
    ]
    results = sorted(
        await asyncio.gather(*tasks),
        key=lambda row: (row["probe_id"], row["run_index"]),
    )
    summary = summarize(results, selected_probes, active_support_types)
    prompt_hash = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    artifact = {
        "experiment": "E-1 single-leaf support attachment",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "forest_path": str(FOREST_PATH),
        "external_leaf_input_path": (
            str(args.external_leaf_input) if args.external_leaf_input else None
        ),
        "block_set_path": str(args.block_set_path),
        "block_id": args.block_id,
        "model": MODEL_ID,
        "thinking": "enabled" if args.thinking else "disabled",
        "reasoning_effort": args.reasoning_effort,
        "quote_grounding_enabled": not args.thinking,
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "run_count_per_probe": args.runs,
        "max_concurrency": args.max_concurrency,
        "probe_ids": list(probe_ids),
        "system_prompt": system_prompt,
        "system_prompt_sha256": prompt_hash,
        "omitted_prompt_sections": list(args.omit_prompt_section),
        "support_types": list(active_support_types),
        "probes": [
            {
                "probe_id": probe.probe_id,
                "leaf_id": probe.leaf_id,
                "leaf": leaves[probe.leaf_id],
                "expected_supported": probe.expected_supported,
                "required_types": list(probe.required_types),
                "allowed_extra_types": [
                    support_type
                    for support_type in probe.allowed_extra_types
                    if support_type in active_support_types
                ],
                "gold_rationale": probe.rationale,
            }
            for probe in selected_probes
        ],
        "summary": summary,
        "results": results,
    }
    result_path = run_dir / "results.json"
    result_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"run_dir": str(run_dir), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
