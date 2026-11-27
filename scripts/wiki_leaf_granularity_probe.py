"""运行知识子树叶节点颗粒度审计与替换子树生成实验。"""

# 探针必须先设置 backend 搜索路径，之后才能导入项目模块。
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import keyring

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.induction import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
    InductionFailure,
    JsonChatResult,
)

FOREST_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_tree_expansion_batch_20261123T113731009302Z"
    / "role_knowledge_forest.json"
)
FOREST_SHA256 = "5d1ddb61a2bd3126d5361d0bd470bf3dd64ef25ec471f06a51c51899e9a43864"
REFERENCE_ROOT_ID = "K21"
REFERENCE_ROOT_SHA256 = "2c3f2f095a0eed4010d1f8903cd1b8c4b4e93f22198073c2aab6f55f4d5c273f"
DEFAULT_TARGET_NODE_ID = "K21.06"
TARGET_NODE_SHA256 = {
    "K21": REFERENCE_ROOT_SHA256,
    "K21.06": "57cca0035b45d890c2a81ba9ce96b328996fa85687e254fab54aeb2494f261c0",
    "K21.06.04": "6777189c8df3a9b026125902764ba873b2f958d7d4a9a6bcc36ff4c9710e7370",
}
OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "leaf_granularity_probe"
MODEL_ID = DEFAULT_MODEL_ID
MAX_OUTPUT_TOKENS = 16_384
TIMEOUT_SECONDS = 180.0
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"

SYSTEM_PROMPT = """\
你是一位知识节点颗粒度审计专家。

给定一个领域、一个岗位角色、整套一级知识目录，以及其中一个一级分支的完整
知识树，请只审计该分支当前的叶节点，并直接返回每个叶节点应保持还是应拆分的
决定。对需要拆分的叶节点，同时生成可替换其叶节点身份的完整下级子树。

【输入】
- domain_name：领域名称；
- role_name：岗位角色名称；
- framework_scope：该岗位角色在领域中需要掌握的知识范围；
- first_level_directory：全部一级节点的 node_id 与 title；
- target_branch：本次待审计的一级分支完整子树。

【审计范围】
1. 只审计 target_branch 中 children 为空的当前叶节点。
2. 每个当前叶节点必须且只能出现一次决定，顺序与输入树中的遍历顺序一致。
3. 不属于当前叶节点的节点不输出决定，也不得修改其标题、描述和排除项。
4. 不重新划分一级目录，不重命名原叶节点，不改变已有父子关系。
5. first_level_directory 只用于识别当前分支与其他一级范围的边界。

【知识身份判据】
1. 不使用固定深度，也不追求绝对逻辑原子。
2. 一个知识身份应能脱离父路径独立命名，并可作为独立知识节点被检索。
3. 如果一个范围包含两个或更多可以分别命名、分别检索的知识身份，应继续拆分为
   子节点。
4. 如果内部内容只是同一知识身份的定义、范围、作用、边界、条件、代价等属性，
   不应拆成兄弟节点，而应写入该节点的 description。
5. 不同分支可以根据实际知识结构使用不同深度。
6. 一个知识对象只回答一个主问题；同一主问题下的维度、指标、条件、方法和示例
   属于该知识身份，并列的不同判断目标必须拆为不同叶节点。

【替换子树要求】
1. split 时，replacement_children 是该原叶节点的直接下级子树，不是新的
   一级或二级节点。
2. replacement_children 至少包含两个节点。若只能形成一个新节点，应保留原叶
   节点并输出 keep。
3. 每个新节点只包含 title、description、exclusions、children 四个字段。
4. title 必须脱离父路径仍可独立识别知识范围；不得使用“概述”“相关内容”
   “其他知识”等无法定位的标题。
5. description 直接说明该范围包含什么知识、主要边界是什么，不得用学习任务或
   执行动作代替知识内容。
6. exclusions 只记录确实容易与该节点混淆但不属于它的相邻知识；没有这种边界
   时返回空数组，不得为了填充而编造排除项。
7. 替换子树共同覆盖原叶节点承诺的知识范围，既不能漏掉原范围中的主要知识，
   也不能把范围外知识塞入。
8. 任何带 children 的新节点至少有两个直接子节点。没有子节点的节点必须返回
   children: []。
9. 同一替换子树中不得出现重复标题、仅更换措辞的同义节点，或父子范围倒置。

【输出要求】
1. 只输出一个符合输出契约的 JSON，不输出 Markdown、解释或额外文字。
2. branch_id 必须与 target_branch.node_id 逐字一致。
3. decisions 必须覆盖全部当前叶节点，不得遗漏、重复或加入未知 node_id。
4. action 只能是 keep 或 split。
5. keep 时 replacement_children 必须为空数组。
6. split 时 replacement_children 必须是至少包含两个节点的有效子树数组。
7. reason 用一句话说明触发 keep 或 split 的主要依据，不展开分析过程。
8. 字符串必须使用合法 JSON 转义；不得输出注释、尾逗号或额外字段。

【输出契约】
{
  "branch_id": "输入中的 target_branch.node_id",
  "decisions": [
    {
      "leaf_node_id": "输入中的当前叶节点 node_id",
      "action": "keep 或 split",
      "reason": "一句话依据",
      "replacement_children": [
        {
          "title": "直接下级知识范围标题",
          "description": "该知识范围的内容与边界",
          "exclusions": [],
          "children": []
        }
      ]
    }
  ]
}
"""


def parse_args() -> argparse.Namespace:
    """解析首轮分支审计参数。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-node-id", default=DEFAULT_TARGET_NODE_ID)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.target_node_id not in TARGET_NODE_SHA256:
        raise ValueError(
            f"target node is not frozen: {args.target_node_id}; known={sorted(TARGET_NODE_SHA256)}"
        )
    if args.runs < 1:
        raise ValueError("runs must be positive")
    return args


def load_json(path: Path) -> dict[str, Any]:
    """读取 UTF-8 JSON 对象。"""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON root must be an object: {path}")
    return value


def canonical_sha256(value: object) -> str:
    """按实验文档记录的紧凑、排序键口径计算哈希。"""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def iter_nodes(nodes: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """按输入顺序深度优先遍历节点。"""

    for node in nodes:
        yield node
        children = node.get("children")
        if children:
            yield from iter_nodes(children)


def current_leaves(branch: Mapping[str, Any]) -> list[dict[str, Any]]:
    """返回目标子树中 children 为空的当前叶节点；目标自身为叶时也纳入。"""

    return [node for node in iter_nodes([dict(branch)]) if not node.get("children")]


def build_input(forest: Mapping[str, Any], target_node_id: str) -> dict[str, Any]:
    """构造分支审计的唯一输入对象。"""

    roots = forest.get("roots")
    if not isinstance(roots, list):
        raise TypeError("forest roots must be a list")
    reference_root = next(
        (node for node in roots if node.get("node_id") == REFERENCE_ROOT_ID),
        None,
    )
    if not isinstance(reference_root, dict):
        raise TypeError(f"reference root not found: {REFERENCE_ROOT_ID}")
    if canonical_sha256(reference_root) != REFERENCE_ROOT_SHA256:
        raise ValueError("reference root hash does not match the frozen REF-0/K21 baseline")
    target = next(
        (node for node in iter_nodes([reference_root]) if node.get("node_id") == target_node_id),
        None,
    )
    if not isinstance(target, dict):
        raise TypeError(f"target node not found under REF-0/K21: {target_node_id}")
    if canonical_sha256(target) != TARGET_NODE_SHA256[target_node_id]:
        raise ValueError(f"target node hash mismatch: {target_node_id}")
    return {
        "domain_name": forest["domain_name"],
        "role_name": forest["role_name"],
        "framework_scope": forest["framework_scope"],
        "first_level_directory": [
            {"node_id": node["node_id"], "title": node["title"]} for node in roots
        ],
        "target_branch": target,
    }


def load_chat_key() -> str:
    """从环境变量或 Windows Credential Manager 读取对话 Key。"""

    key = os.environ.get("DEEPSEEK_API_KEY") or keyring.get_password(
        CREDENTIAL_SERVICE_NAME,
        CHAT_API_KEY_USERNAME,
    )
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def require_exact_keys(
    value: Mapping[str, Any],
    expected: set[str],
    location: str,
) -> None:
    """要求对象只包含契约声明的字段。"""

    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"unexpected_keys:{location}:missing={sorted(expected - actual)}:"
            f"extra={sorted(actual - expected)}"
        )


def require_text(value: object, location: str) -> str:
    """读取并规范化非空字符串。"""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"text_blank:{location}")
    return value.strip()


def validate_replacements(
    raw_nodes: object,
    *,
    location: str,
) -> list[dict[str, Any]]:
    """校验一个替换子树并保留模型给出的字段。"""

    if not isinstance(raw_nodes, list) or len(raw_nodes) < 2:
        raise ValueError(f"replacement_children_minimum_two:{location}")
    normalized: list[dict[str, Any]] = []
    sibling_titles: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        node_location = f"{location}.children[{index}]"
        if not isinstance(raw_node, dict):
            raise TypeError(f"node_must_be_object:{node_location}")
        require_exact_keys(
            raw_node,
            {"title", "description", "exclusions", "children"},
            node_location,
        )
        title = require_text(raw_node.get("title"), f"{node_location}.title")
        description = require_text(
            raw_node.get("description"),
            f"{node_location}.description",
        )
        exclusions = raw_node.get("exclusions")
        if not isinstance(exclusions, list) or any(
            not isinstance(item, str) or not item.strip() for item in exclusions
        ):
            raise ValueError(f"exclusions_must_be_nonblank_strings:{node_location}")
        children = raw_node.get("children")
        if not isinstance(children, list):
            raise TypeError(f"children_must_be_array:{node_location}")
        normalized_title = " ".join(title.casefold().split())
        if normalized_title in sibling_titles:
            raise ValueError(f"duplicate_sibling_title:{node_location}")
        sibling_titles.add(normalized_title)
        normalized_children = (
            validate_replacements(children, location=node_location) if children else []
        )
        normalized.append(
            {
                "title": title,
                "description": description,
                "exclusions": exclusions,
                "children": normalized_children,
            }
        )
    return normalized


def count_leaves(nodes: list[dict[str, Any]]) -> int:
    """统计子树叶节点数。"""

    return sum(1 for node in iter_nodes(nodes) if not node.get("children"))


def validate_response(
    payload: Mapping[str, Any],
    expected_leaf_ids: list[str],
    expected_node_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验模型输出，返回规范化决定和结构错误。"""

    try:
        require_exact_keys(payload, {"branch_id", "decisions"}, "root")
        if payload.get("branch_id") != expected_node_id:
            raise ValueError("branch_id_mismatch")
        decisions = payload.get("decisions")
        if not isinstance(decisions, list):
            raise TypeError("decisions_must_be_array")
        actual_leaf_ids = [
            decision.get("leaf_node_id") for decision in decisions if isinstance(decision, dict)
        ]
        if actual_leaf_ids != expected_leaf_ids:
            raise ValueError(
                "leaf_decision_set_or_order_mismatch:"
                f"expected={expected_leaf_ids}:actual={actual_leaf_ids}"
            )
        normalized: list[dict[str, Any]] = []
        for index, raw_decision in enumerate(decisions):
            location = f"decisions[{index}]"
            if not isinstance(raw_decision, dict):
                raise TypeError(f"decision_must_be_object:{location}")
            require_exact_keys(
                raw_decision,
                {"leaf_node_id", "action", "reason", "replacement_children"},
                location,
            )
            leaf_id = require_text(
                raw_decision.get("leaf_node_id"),
                f"{location}.leaf_node_id",
            )
            action = raw_decision.get("action")
            if action not in {"keep", "split"}:
                raise ValueError(f"invalid_action:{location}")
            reason = require_text(raw_decision.get("reason"), f"{location}.reason")
            raw_replacements = raw_decision.get("replacement_children")
            if action == "keep":
                if raw_replacements != []:
                    raise ValueError(f"keep_requires_empty_replacements:{location}")
                replacements: list[dict[str, Any]] = []
            else:
                replacements = validate_replacements(
                    raw_replacements,
                    location=location,
                )
            normalized.append(
                {
                    "leaf_node_id": leaf_id,
                    "action": action,
                    "reason": reason,
                    "replacement_children": replacements,
                }
            )
        return normalized, []
    except (TypeError, ValueError) as error:
        return [], [str(error)]


def summarize_decisions(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总叶节点决定、替换规模和深度。"""

    split = [item for item in decisions if item["action"] == "split"]
    replacement_nodes = [
        node for item in split for node in iter_nodes(item["replacement_children"])
    ]
    return {
        "decision_count": len(decisions),
        "keep_count": len(decisions) - len(split),
        "split_count": len(split),
        "split_leaf_ids": [item["leaf_node_id"] for item in split],
        "replacement_node_count": len(replacement_nodes),
        "replacement_leaf_count": sum(1 for node in replacement_nodes if not node.get("children")),
        "replacement_internal_count": sum(1 for node in replacement_nodes if node.get("children")),
        "replacement_max_depth": max(
            (node_depth(item["replacement_children"], 1) for item in split),
            default=0,
        ),
    }


def node_depth(nodes: list[dict[str, Any]], depth: int) -> int:
    """计算子树相对其替换根的最大深度。"""

    child_depths = [
        node_depth(node["children"], depth + 1) for node in nodes if node.get("children")
    ]
    return max(child_depths, default=depth)


async def run_once(
    client: DeepSeekJsonClient,
    input_payload: dict[str, Any],
    expected_leaf_ids: list[str],
) -> dict[str, Any]:
    """执行一次模型调用并记录可供后续复核的结果。"""

    started = time.perf_counter()
    try:
        result: JsonChatResult = await client.complete_json(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=("输入：\n" + json.dumps(input_payload, ensure_ascii=False, indent=2)),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except InductionFailure as error:
        return {
            "status": "provider_failure",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "error": str(error),
            "raw_response": None,
            "decisions": [],
            "summary": {},
        }
    decisions, validation_errors = validate_response(
        result.payload,
        expected_leaf_ids,
        str(input_payload["target_branch"]["node_id"]),
    )
    return {
        "status": "complete" if not validation_errors else "structural_failure",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "actual_model_id": result.actual_model_id,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_hit_tokens": result.cache_hit_tokens,
        "cache_miss_tokens": result.cache_miss_tokens,
        "raw_response": result.payload,
        "validation_errors": validation_errors,
        "decisions": decisions,
        "summary": summarize_decisions(decisions),
    }


async def main() -> None:
    """执行固定分支审计并保存可追溯产物。"""

    args = parse_args()
    forest_bytes = FOREST_PATH.read_bytes()
    if hashlib.sha256(forest_bytes).hexdigest() != FOREST_SHA256:
        raise ValueError("forest hash does not match the frozen REF-0 baseline")
    forest = json.loads(forest_bytes.decode("utf-8"))
    input_payload = build_input(forest, args.target_node_id)
    branch = input_payload["target_branch"]
    leaves = current_leaves(branch)
    expected_leaf_ids = [leaf["node_id"] for leaf in leaves]
    prompt_sha256 = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "branch_id": args.target_node_id,
                    "leaf_count": len(leaves),
                    "input_chars": len(json.dumps(input_payload, ensure_ascii=False)),
                    "prompt_sha256": prompt_sha256,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    client = DeepSeekJsonClient(
        load_chat_key(),
        model_id=MODEL_ID,
        base_url=DEFAULT_BASE_URL,
        timeout_seconds=TIMEOUT_SECONDS,
        thinking_enabled=args.thinking,
    )
    runs = [await run_once(client, input_payload, expected_leaf_ids) for _ in range(args.runs)]
    artifact = {
        "experiment": "leaf granularity audit and replacement subtree",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "forest_path": str(FOREST_PATH),
        "forest_sha256": FOREST_SHA256,
        "reference_root_id": REFERENCE_ROOT_ID,
        "target_node_id": args.target_node_id,
        "branch_id": args.target_node_id,
        "branch_sha256": TARGET_NODE_SHA256[args.target_node_id],
        "branch_leaf_count": len(leaves),
        "model_id": MODEL_ID,
        "thinking": args.thinking,
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "timeout_seconds": TIMEOUT_SECONDS,
        "runs": args.runs,
        "system_prompt": SYSTEM_PROMPT,
        "system_prompt_sha256": prompt_sha256,
        "input_payload": input_payload,
        "expected_leaf_ids": expected_leaf_ids,
        "results": runs,
    }
    result_path = run_dir / "results.json"
    result_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "prompt_sha256": prompt_sha256,
                "results": [
                    {
                        "status": run["status"],
                        "elapsed_seconds": run["elapsed_seconds"],
                        "input_tokens": run.get("input_tokens"),
                        "output_tokens": run.get("output_tokens"),
                        "validation_errors": run.get("validation_errors", []),
                        "summary": run["summary"],
                    }
                    for run in runs
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
