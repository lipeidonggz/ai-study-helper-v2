"""用叶节点完整路径对已路由的叶节点池做一次高召回排除实验。"""

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
    FOREST_PATH,
    MODEL_ID,
    load_block,
    load_json,
    load_leaves,
)
from wiki_root_tree_batch_probe import KNOWN_GOLD_BY_ROOT

DEFAULT_SOURCE_RESULT_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "routed_leaf_batch_stability_10x_probe"
    / "20261124T135712443966Z"
    / "results.json"
)
OUTPUT_ROOT = BACKEND_ROOT / ".runtime" / "title_prefilter_probe"
PREFILTER_MAX_OUTPUT_TOKENS = 32768

SYSTEM_PROMPT = """\
你是知识叶节点候选去除器。你会收到一个 block 和一份已经由上游保留的叶节点完整路径清单。

你的任务不是判断叶节点是否最终成立，也不是选择最相关叶节点。你只需要排除那些仅凭
当前完整路径就能确定不可能从 block 获得直接证据的叶节点。本步骤是最终证据判定的高召回
前置步骤，误排除会造成不可恢复的漏挂；存在合理疑问时必须保留。本步骤允许一个叶节点
仅因其邻近知识对象可能获得支持而被保留，但这不表示该叶节点已经成立。

【输入】
- block.section_path：原文所在章节路径；
- block.text：本次唯一可用的原文；
- leaves：叶节点数组。每项只有 leaf_id 和 path，不提供描述或排除项。path 是从根节点到
  当前叶节点的完整标题路径，最后一项是叶节点标题。

【判定规则】
只能排除同时满足以下条件的叶节点：
1. 路径所指的知识对象或关系与 block 讨论的内容没有合理对应；
2. block 中的具体陈述不可能作为该路径所指知识对象，或与该路径构成直接上下位、组成、
   过程、职责或边界关系的邻近知识对象的案例、实现、条件、组成、机制、方法、效果或
   评价线索；
3. 该判断不需要依赖未提供的叶节点描述、路径或下级定义。

邻近知识对象不要求在输入 leaves 中出现，可以是模型依据完整路径及其领域含义识别出的
直接相关概念。只允许直接上下位、组成、过程、职责或边界关系，不得把同一宽泛领域中的
任意相关概念、仅有词义相近的概念或列表中相邻的叶节点当作邻居。

以下情况必须保留：
1. block 提供的是具体案例、实现、操作、条件、风险或效果，但可能对应标题所指的上位知识；
2. block 只涉及标题范围中的一个可能分支；
3. 路径与 block 没有直接的词面重合，但知识身份仍可能对应；
4. 只能确认“大概率无关”，但不能确认“明确不可能”；
5. 需要叶节点描述或路径才能进一步判断；
6. 当前标题本身没有直接对应，但存在可明确识别的邻近叶节点可能从 block 获得支持。

不得因为 block 没有覆盖路径代表的全部范围、路径范围较宽、目录中存在更具体的其他
叶节点，或当前 block 的主要主题不是该路径，就排除叶节点。候选是否最终成立由后续
证据判定器负责。

【输出】
只输出一个 JSON 对象，不得输出 Markdown 或 JSON 之外的说明。

excluded 只包含准备排除的叶节点。没有可排除项时输出空数组。每项字段顺序固定为
leaf_id、reason，reason 用不超过 60 字说明为什么明确不可能获得支持。不得输出保留项，
不得输出输入中不存在的 leaf_id，不得重复 leaf_id，不得增加其他字段。

{
  "excluded": [
    {
      "leaf_id": "输入 leaves 中的 leaf_id",
      "reason": "明确排除理由"
    }
  ]
}
"""


def expected_supported(leaf_id: str) -> bool:
    """读取已人工审核的 b00008 叶节点结果；未列出的叶节点按负例处理。"""

    root_id = leaf_id.split(".", 1)[0]
    return KNOWN_GOLD_BY_ROOT.get(root_id, {}).get(leaf_id, False)


def load_selected_leaf_ids(path: Path) -> tuple[list[str], dict[str, Any]]:
    """从既有分批产物中读取第一轮叶节点及其跨树混排顺序。"""

    artifact = load_json(path)
    batches = artifact.get("base_batches")
    if not isinstance(batches, list):
        raise TypeError("source result artifact has no base_batches array")
    first_pass = [
        batch for batch in batches if isinstance(batch, dict) and batch.get("coverage_pass") == 1
    ]
    if not first_pass:
        raise RuntimeError("source result artifact has no first coverage pass")

    leaf_ids: list[str] = []
    for batch in first_pass:
        batch_leaf_ids = batch.get("leaf_ids")
        if not isinstance(batch_leaf_ids, list) or not all(
            isinstance(leaf_id, str) for leaf_id in batch_leaf_ids
        ):
            raise TypeError("source batch leaf_ids is invalid")
        leaf_ids.extend(batch_leaf_ids)
    if len(leaf_ids) != len(set(leaf_ids)):
        raise RuntimeError("source result contains duplicate leaf ids in first pass")
    return leaf_ids, {
        "path": str(path),
        "run_id": artifact.get("run_id"),
        "base_batches": len(first_pass),
        "order": "first coverage pass batch order",
    }


def build_messages(
    block: dict[str, Any], leaves: dict[str, dict[str, Any]], leaf_ids: list[str]
) -> tuple[ChatMessage, ...]:
    """构造只包含 block 原文和叶节点完整路径的候选去除请求。"""

    payload = {
        "block": {
            "section_path": block["section_path"],
            "text": block["text"],
        },
        "leaves": [
            {
                "leaf_id": leaf_id,
                "path": leaves[leaf_id]["path"],
            }
            for leaf_id in leaf_ids
        ],
    }
    return (
        ChatMessage(role="system", content=SYSTEM_PROMPT),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False, indent=2)),
    )


def validate_exclusions(
    content: str, *, leaf_ids: list[str], positive_ids: frozenset[str]
) -> dict[str, Any]:
    """校验排除决定，并把未排除项按高召回语义恢复为保留集合。"""

    allowed_ids = frozenset(leaf_ids)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        return {
            "parse_ok": False,
            "valid": False,
            "error": f"JSONDecodeError: {error}",
            "excluded_ids": [],
            "retained_ids": leaf_ids,
            "fallback_all_retained": True,
        }

    structure_errors: list[str] = []
    if not isinstance(parsed, dict) or list(parsed) != ["excluded"]:
        structure_errors.append("top-level object must contain only excluded")
        items = []
    else:
        items = parsed["excluded"]
        if not isinstance(items, list):
            structure_errors.append("excluded must be an array")
            items = []

    excluded_ids: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or list(item) != ["leaf_id", "reason"]:
            structure_errors.append(f"excluded[{index}] has invalid fields")
            continue
        leaf_id = item["leaf_id"]
        reason = item["reason"]
        if not isinstance(leaf_id, str) or leaf_id not in allowed_ids:
            structure_errors.append(f"excluded[{index}].leaf_id is not in input")
            continue
        if not isinstance(reason, str) or not reason.strip():
            structure_errors.append(f"excluded[{index}].reason is empty")
            continue
        excluded_ids.append(leaf_id)

    if len(excluded_ids) != len(set(excluded_ids)):
        structure_errors.append("excluded leaf ids contain duplicates")
    excluded_set = frozenset(excluded_ids)
    retained_ids = [leaf_id for leaf_id in leaf_ids if leaf_id not in excluded_set]
    valid = not structure_errors
    return {
        "parse_ok": True,
        "valid": valid,
        "structure_errors": structure_errors,
        "parsed": parsed,
        "excluded_ids": sorted(excluded_set) if valid else [],
        "retained_ids": retained_ids if valid else leaf_ids,
        "fallback_all_retained": not valid,
        "excluded_positive_ids": sorted(excluded_set & positive_ids) if valid else [],
    }


async def run_once(
    messages: tuple[ChatMessage, ...],
    *,
    leaf_ids: list[str],
    thinking_enabled: bool,
) -> dict[str, Any]:
    """执行一次标题去除调用并保存失败或校验结果，不自动重试。"""

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
            max_output_tokens=PREFILTER_MAX_OUTPUT_TOKENS,
            thinking_enabled=thinking_enabled,
        ),
    )
    started_at = datetime.now(UTC).isoformat()
    try:
        result = await provider.complete(messages, max_output_tokens=PREFILTER_MAX_OUTPUT_TOKENS)
    except ChatProviderError as error:
        return {
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "messages": [
                {"role": message.role, "content": message.content} for message in messages
            ],
            "error": f"{type(error).__name__}: {error}",
            "validation": {
                "parse_ok": False,
                "valid": False,
                "error": f"{type(error).__name__}: {error}",
                "excluded_ids": [],
                "retained_ids": leaf_ids,
                "fallback_all_retained": True,
            },
        }

    positive_ids = frozenset(leaf_id for leaf_id in leaf_ids if expected_supported(leaf_id))
    validation = validate_exclusions(
        result.content,
        leaf_ids=leaf_ids,
        positive_ids=positive_ids,
    )
    return {
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


def parse_args() -> argparse.Namespace:
    """解析源产物与输出目录；默认只运行一次。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-result-path", type=Path, default=DEFAULT_SOURCE_RESULT_PATH)
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="启用模型 thinking；默认关闭。",
    )
    return parser.parse_args()


def main() -> None:
    """加载固定输入、执行一次标题去除并写出完整实验产物。"""

    args = parse_args()
    leaf_ids, source = load_selected_leaf_ids(args.source_result_path)
    forest = load_json(FOREST_PATH)
    leaves = load_leaves(forest)
    missing_leaf_ids = [leaf_id for leaf_id in leaf_ids if leaf_id not in leaves]
    if missing_leaf_ids:
        raise RuntimeError(f"leaves missing from forest: {missing_leaf_ids}")
    block = load_block()
    messages = build_messages(block, leaves, leaf_ids)
    positive_ids = frozenset(leaf_id for leaf_id in leaf_ids if expected_supported(leaf_id))

    async def execute() -> dict[str, Any]:
        return await run_once(
            messages,
            leaf_ids=leaf_ids,
            thinking_enabled=args.thinking,
        )

    result = asyncio.run(execute())
    validation = result["validation"]
    excluded_ids = validation.get("excluded_ids") or []
    retained_ids = validation.get("retained_ids") or leaf_ids
    retained_positive_ids = sorted(positive_ids & frozenset(retained_ids))
    excluded_negative_ids = sorted(frozenset(excluded_ids) - positive_ids)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    artifact = {
        "experiment": "title-only leaf prefilter",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "forest_path": str(FOREST_PATH),
        "block_id": block["block_id"],
        "model": MODEL_ID,
        "thinking": "enabled" if args.thinking else "disabled",
        "temperature": 0.0,
        "max_output_tokens": PREFILTER_MAX_OUTPUT_TOKENS,
        "source": source,
        "leaf_input_count": len(leaf_ids),
        "positive_count": len(positive_ids),
        "negative_count": len(leaf_ids) - len(positive_ids),
        "leaf_input_fields": ["leaf_id", "path"],
        "system_prompt": SYSTEM_PROMPT,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "result": result,
        "summary": {
            "parse_ok": validation.get("parse_ok"),
            "valid": validation.get("valid"),
            "fallback_all_retained": validation.get("fallback_all_retained"),
            "excluded_count": len(excluded_ids),
            "retained_count": len(retained_ids),
            "excluded_negative_count": len(excluded_negative_ids),
            "excluded_positive_count": len(validation.get("excluded_positive_ids") or []),
            "excluded_positive_ids": validation.get("excluded_positive_ids") or [],
            "retained_positive_count": len(retained_positive_ids),
            "retained_positive_ids": retained_positive_ids,
            "negatives_removed_ratio": (
                len(excluded_negative_ids) / (len(leaf_ids) - len(positive_ids))
                if len(leaf_ids) > len(positive_ids)
                else 0.0
            ),
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
        },
    }
    result_path = run_dir / "results.json"
    result_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(result_path)


if __name__ == "__main__":
    main()
