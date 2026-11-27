"""并行验证角色知识框架一级目录生成提示词。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import keyring

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.induction import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
    InductionFailure,
    JsonChatResult,
)

PROMPT_VERSION = "role-knowledge-first-level-v2-effective-duty"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"
DOMAIN_NAME = "AI Agent"
ROLE_NAME = "系统架构师"
MAX_OUTPUT_TOKENS = 16_384
RUN_COUNT = 5

SYSTEM_PROMPT = """你是一位知识框架设计专家。

给定一个领域和一个岗位角色，请识别该岗位角色在该领域中有效履行职责所
必须掌握的知识范围，并为这些知识生成一级目录。

【任务】
识别该岗位角色在该领域中有效履行职责所必须掌握的知识，并且只生成一级
目录。一级目录用于界定整套框架的大类知识范围，不是最终叶节点，也不在
这一步继续展开。

【一级节点要求】
1. 每个一级节点必须是知识范围，不能是工作任务、实施步骤、项目安排、
   交付文档或产品方案。判断一项知识是否应纳入，依据是它是否是该岗位在
   该领域中有效履行职责所必须掌握的知识，不要求它属于狭义的领域核心知识。
2. 标题应清楚、稳定，脱离父路径后仍能独立识别所指的领域知识范围。
3. description 用一到三句话说明该范围是什么、主要包含哪些知识内容，以及
   这些内容为什么构成同一个知识范围。不要只复述标题，也不要用空泛的
   “基础知识”“相关内容”等措辞代替知识范围。
4. exclusions 只记录确实容易与该一级范围混淆、但不属于该范围的相邻知识；
   没有这种相邻知识时返回空数组。
5. 各一级节点应共同覆盖该岗位角色有效履行职责所必需的主要知识范围，
   不能只列出少量示例，也不能只覆盖该领域内部的技术构成。
6. 一级节点可以是有内部结构的知识范围，但必须具有统一的知识身份；不能把
   互不相干的知识仅因为同属该领域就合并成一个节点。
7. 各一级节点之间不能是同一知识身份的重复表述、同义改名或简单上下位重复。
8. 不预设一级节点数量。节点数量由该角色需要掌握的知识范围决定。

【输出要求】
只输出符合下方输出契约的 JSON，不输出 Markdown、解释或额外文字。
domain 和 role 必须与输入逐字一致。first_level_nodes 中每个对象只有
title、description 和 exclusions 三个字段，不得输出 children 或其他层级。

【输出契约】
{
  "domain": "领域名称",
  "role": "角色名称",
  "framework_scope": "该岗位角色在该领域中有效履行职责所必须掌握的知识范围概述",
  "first_level_nodes": [
    {
      "title": "一级知识范围标题",
      "description": "该知识范围是什么、主要包含哪些知识内容，以及这些内容为什么构成同一个知识范围",
      "exclusions": []
    }
  ]
}
"""

USER_PROMPT = f"领域名称：{DOMAIN_NAME}\n角色名称：{ROLE_NAME}"


def load_chat_key() -> str:
    """从环境变量或 Windows Credential Manager 读取 DeepSeek Key。"""

    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def validate_payload(payload: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """校验一级目录输出契约，并生成结构摘要。"""

    errors: list[str] = []
    if payload.get("domain") != DOMAIN_NAME:
        errors.append("domain_mismatch")
    if payload.get("role") != ROLE_NAME:
        errors.append("role_mismatch")
    framework_scope = payload.get("framework_scope")
    if not isinstance(framework_scope, str) or not framework_scope.strip():
        errors.append("framework_scope_missing")

    raw_nodes = payload.get("first_level_nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("first_level_nodes_missing")
        raw_nodes = []

    normalized_titles: list[str] = []
    titles: list[str] = []
    description_lengths: list[int] = []
    exclusion_item_count = 0
    empty_exclusion_count = 0
    unexpected_key_nodes = 0
    children_present_count = 0

    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            errors.append(f"node_not_object:{index}")
            continue
        expected_keys = {"title", "description", "exclusions"}
        if set(raw_node) != expected_keys:
            unexpected_key_nodes += 1
            errors.append(f"node_keys_invalid:{index}:{sorted(raw_node)}")
        if "children" in raw_node:
            children_present_count += 1

        title = raw_node.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"title_missing:{index}")
            continue
        normalized_title = " ".join(title.split()).casefold()
        normalized_titles.append(normalized_title)
        titles.append(title.strip())

        description = raw_node.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"description_missing:{index}")
        else:
            description_lengths.append(len(description.strip()))

        exclusions = raw_node.get("exclusions")
        if not isinstance(exclusions, list):
            errors.append(f"exclusions_not_array:{index}")
        else:
            if not exclusions:
                empty_exclusion_count += 1
            for exclusion_index, exclusion in enumerate(exclusions):
                if not isinstance(exclusion, str) or not exclusion.strip():
                    errors.append(f"exclusion_invalid:{index}:{exclusion_index}")
            exclusion_item_count += len(exclusions)

    duplicate_counter = Counter(normalized_titles)
    duplicates = sorted(title for title, count in duplicate_counter.items() if count > 1)
    if duplicates:
        errors.extend(f"duplicate_title:{title}" for title in duplicates)

    summary = {
        "first_level_count": len(raw_nodes),
        "titles": titles,
        "duplicate_titles": duplicates,
        "description_lengths": description_lengths,
        "description_length_median": _median(description_lengths),
        "empty_exclusion_count": empty_exclusion_count,
        "exclusion_item_count": exclusion_item_count,
        "unexpected_key_nodes": unexpected_key_nodes,
        "children_present_count": children_present_count,
    }
    return errors, summary


def _median(values: list[int]) -> float | None:
    """返回整数列表的中位数。"""

    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


async def run_one(
    client: DeepSeekJsonClient,
    run_index: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    """执行一次一级目录生成，并保留原始输出或显式失败。"""

    async with semaphore:
        started = time.perf_counter()
        try:
            result: JsonChatResult = await client.complete_json(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=USER_PROMPT,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            elapsed_seconds = round(time.perf_counter() - started, 3)
            errors, summary = validate_payload(result.payload)
            return {
                "run": run_index,
                "status": "valid" if not errors else "invalid",
                "elapsed_seconds": elapsed_seconds,
                "actual_model_id": result.actual_model_id,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cache_hit_tokens": result.cache_hit_tokens,
                "cache_miss_tokens": result.cache_miss_tokens,
                "validation_errors": errors,
                "summary": summary,
                "payload": result.payload,
            }
        except (InductionFailure, TypeError, ValueError) as error:
            return {
                "run": run_index,
                "status": "failed",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "error": str(error),
            }


async def run() -> dict[str, Any]:
    """并行执行三次固定配置的一级目录生成。"""

    client = DeepSeekJsonClient(
        load_chat_key(),
        model_id=DEFAULT_MODEL_ID,
        base_url=DEFAULT_BASE_URL,
        timeout_seconds=300.0,
        thinking_enabled=True,
    )
    semaphore = asyncio.Semaphore(RUN_COUNT)
    results = await asyncio.gather(
        *(
            run_one(client, run_index, semaphore)
            for run_index in range(1, RUN_COUNT + 1)
        )
    )
    return {
        "status": "completed",
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "prompt": SYSTEM_PROMPT,
        "user_prompt": USER_PROMPT,
        "input": {"domain_name": DOMAIN_NAME, "role_name": ROLE_NAME},
        "requested_model_id": DEFAULT_MODEL_ID,
        "thinking_enabled": True,
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "concurrency": RUN_COUNT,
        "results": results,
    }


def main() -> None:
    """运行探针并写入带时间戳的原始结果目录。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run())
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        BACKEND_ROOT / ".runtime" / f"role_knowledge_framework_first_level_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "results.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        result["run"]: {
            "status": result["status"],
            "elapsed_seconds": result["elapsed_seconds"],
            "input_tokens": result.get("input_tokens"),
            "output_tokens": result.get("output_tokens"),
            "validation_error_count": len(result.get("validation_errors", [])),
            "first_level_count": result.get("summary", {}).get("first_level_count"),
            "titles": result.get("summary", {}).get("titles"),
            "error": result.get("error"),
        }
        for result in report["results"]
    }
    print(json.dumps({"results": summary, "output_path": str(output_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
