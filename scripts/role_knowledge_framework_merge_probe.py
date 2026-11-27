"""验证角色知识框架归并提示词，并检查来源账本是否完整。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import keyring

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.induction import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
    InductionFailure,
    JsonChatResult,
)

PROMPT_VERSION = "role-knowledge-framework-merge-v2-identity-contract"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"
DOMAIN_NAME = "AI Agent"
ROLE_NAME = "系统架构师"
MAX_OUTPUT_TOKENS = 49_152
EXPERIMENT_DOCUMENT = (
    PROJECT_ROOT
    / "docs"
    / "development"
    / "experiments"
    / "2026-11-23-角色知识框架两阶段分层生成.md"
)
SOURCE_RESULT_PATHS = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_first_level_20261123T081803766453Z"
    / "results.json",
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_first_level_20261123T082207902294Z"
    / "results.json",
)
VALID_RELATIONS = {"equivalent", "contained", "split", "needs_review"}


def load_chat_key() -> str:
    """从环境变量或 Windows Credential Manager 读取 DeepSeek Key。"""

    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def load_system_prompt() -> str:
    """从实验文档读取已经审核的归并系统提示词。"""

    document = EXPERIMENT_DOCUMENT.read_text(encoding="utf-8")
    start = document.index("#### 10.11.1 系统提示词")
    end = document.index("#### 10.11.2", start)
    section = document[start:end]
    match = re.search(r"```text\r?\n(.*?)\r?\n```", section, re.DOTALL)
    if match is None:
        raise RuntimeError("merge_system_prompt_missing")
    return match.group(1)


def load_candidate_input() -> dict[str, Any]:
    """加载八份候选结果，并为每个候选节点分配稳定来源 ID。"""

    candidate_frameworks: list[dict[str, Any]] = []
    run_number = 0
    for result_path in SOURCE_RESULT_PATHS:
        report = json.loads(result_path.read_text(encoding="utf-8"))
        for result in report["results"]:
            if result.get("status") != "valid":
                raise RuntimeError(f"source_run_invalid:{result_path}:{result.get('run')}")
            run_number += 1
            run_id = f"r{run_number:02d}"
            nodes: list[dict[str, Any]] = []
            for node_number, node in enumerate(
                result["payload"]["first_level_nodes"],
                start=1,
            ):
                nodes.append(
                    {
                        "source_node_id": f"{run_id}-n{node_number:02d}",
                        "title": node["title"],
                        "description": node["description"],
                        "exclusions": node["exclusions"],
                    }
                )
            candidate_frameworks.append(
                {
                    "run_id": run_id,
                    "source_output_path": str(result_path),
                    "source_run": result["run"],
                    "framework_scope": result["payload"]["framework_scope"],
                    "first_level_nodes": nodes,
                }
            )
    return {
        "domain_name": DOMAIN_NAME,
        "role_name": ROLE_NAME,
        "candidate_frameworks": candidate_frameworks,
    }


def validate_payload(
    payload: dict[str, Any],
    expected_source_ids: set[str],
) -> tuple[list[str], dict[str, Any]]:
    """校验规范框架、来源账本和待审项之间的引用完整性。"""

    errors: list[str] = []
    if payload.get("domain") != DOMAIN_NAME:
        errors.append("domain_mismatch")
    if payload.get("role") != ROLE_NAME:
        errors.append("role_mismatch")
    if not isinstance(payload.get("framework_scope"), str) or not payload[
        "framework_scope"
    ].strip():
        errors.append("framework_scope_missing")

    raw_nodes = payload.get("first_level_nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("first_level_nodes_missing")
        raw_nodes = []

    node_ids: list[str] = []
    titles: list[str] = []
    description_lengths: list[int] = []
    for node in raw_nodes:
        if not isinstance(node, dict):
            errors.append("first_level_node_not_object")
            continue
        if set(node) != {"node_id", "title", "description", "exclusions"}:
            errors.append(f"first_level_node_fields_invalid:{node.get('node_id')}")
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append("first_level_node_id_missing")
            continue
        node_ids.append(node_id)
        title = node.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"title_missing:{node_id}")
        else:
            titles.append(title.strip())
        description = node.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"description_missing:{node_id}")
        else:
            description_lengths.append(len(description))
        if not isinstance(node.get("exclusions"), list):
            errors.append(f"exclusions_not_list:{node_id}")

    duplicate_node_ids = sorted(
        node_id for node_id in set(node_ids) if node_ids.count(node_id) > 1
    )
    duplicate_titles = sorted(
        title for title in set(titles) if titles.count(title) > 1
    )
    errors.extend(f"duplicate_node_id:{node_id}" for node_id in duplicate_node_ids)
    errors.extend(f"duplicate_title:{title}" for title in duplicate_titles)
    node_id_set = set(node_ids)

    ledger = payload.get("merge_ledger")
    if not isinstance(ledger, list):
        errors.append("merge_ledger_missing")
        ledger = []

    ledger_source_ids: list[str] = []
    target_node_ids: set[str] = set()
    relation_counts = {relation: 0 for relation in sorted(VALID_RELATIONS)}
    needs_review_source_ids: set[str] = set()
    for entry in ledger:
        if not isinstance(entry, dict):
            errors.append("merge_ledger_entry_not_object")
            continue
        if set(entry) != {
            "source_node_id",
            "relation",
            "target_node_ids",
            "reason",
        }:
            errors.append(f"merge_ledger_fields_invalid:{entry.get('source_node_id')}")
        source_node_id = entry.get("source_node_id")
        if isinstance(source_node_id, str):
            ledger_source_ids.append(source_node_id)
        else:
            errors.append("merge_ledger_source_id_missing")
            continue
        relation = entry.get("relation")
        if relation not in VALID_RELATIONS:
            errors.append(f"merge_ledger_relation_invalid:{source_node_id}")
        else:
            relation_counts[relation] += 1
        raw_target_ids = entry.get("target_node_ids")
        if not isinstance(raw_target_ids, list):
            errors.append(f"merge_ledger_target_ids_invalid:{source_node_id}")
        else:
            for target_node_id in raw_target_ids:
                if not isinstance(target_node_id, str):
                    errors.append(f"merge_ledger_target_id_not_string:{source_node_id}")
                    continue
                target_node_ids.add(target_node_id)
                if target_node_id not in node_id_set:
                    errors.append(
                        f"merge_ledger_target_missing:{source_node_id}:{target_node_id}"
                    )
            if relation == "split" and len(raw_target_ids) < 2:
                errors.append(f"split_target_count_invalid:{source_node_id}")
            if relation in {"equivalent", "contained"} and len(raw_target_ids) != 1:
                errors.append(f"single_target_count_invalid:{source_node_id}")
            if relation == "needs_review":
                needs_review_source_ids.add(source_node_id)
                if raw_target_ids:
                    errors.append(f"needs_review_has_target:{source_node_id}")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            errors.append(f"merge_ledger_reason_missing:{source_node_id}")

    duplicate_ledger_source_ids = sorted(
        source_id
        for source_id in set(ledger_source_ids)
        if ledger_source_ids.count(source_id) > 1
    )
    errors.extend(
        f"duplicate_ledger_source_id:{source_id}"
        for source_id in duplicate_ledger_source_ids
    )
    missing_source_ids = sorted(expected_source_ids - set(ledger_source_ids))
    unexpected_source_ids = sorted(set(ledger_source_ids) - expected_source_ids)
    errors.extend(f"source_missing:{source_id}" for source_id in missing_source_ids)
    errors.extend(
        f"source_unexpected:{source_id}" for source_id in unexpected_source_ids
    )
    unreferenced_node_ids = sorted(node_id_set - target_node_ids)
    errors.extend(f"node_unreferenced:{node_id}" for node_id in unreferenced_node_ids)

    review_items = payload.get("review_items")
    if not isinstance(review_items, list):
        errors.append("review_items_missing")
        review_items = []
    review_source_ids: set[str] = set()
    for item in review_items:
        if not isinstance(item, dict):
            errors.append("review_item_not_object")
            continue
        raw_source_ids = item.get("source_node_ids")
        if not isinstance(raw_source_ids, list):
            errors.append("review_item_source_ids_invalid")
            continue
        for source_node_id in raw_source_ids:
            if not isinstance(source_node_id, str):
                errors.append("review_item_source_id_not_string")
                continue
            review_source_ids.add(source_node_id)
            if source_node_id not in expected_source_ids:
                errors.append(f"review_item_source_unexpected:{source_node_id}")
            if source_node_id not in needs_review_source_ids:
                errors.append(f"review_item_source_not_marked:{source_node_id}")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            errors.append("review_item_reason_missing")
    unlisted_needs_review_ids = sorted(needs_review_source_ids - review_source_ids)
    errors.extend(
        f"needs_review_source_unlisted:{source_id}"
        for source_id in unlisted_needs_review_ids
    )

    summary = {
        "canonical_node_count": len(raw_nodes),
        "canonical_titles": titles,
        "description_lengths": description_lengths,
        "description_length_median": _median(description_lengths),
        "expected_source_count": len(expected_source_ids),
        "ledger_source_count": len(ledger_source_ids),
        "relation_counts": relation_counts,
        "needs_review_source_count": len(needs_review_source_ids),
        "unreferenced_node_count": len(unreferenced_node_ids),
        "missing_source_count": len(missing_source_ids),
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


async def run() -> dict[str, Any]:
    """执行一次完整归并，并保留输入、原始输出和校验结果。"""

    system_prompt = load_system_prompt()
    candidate_input = load_candidate_input()
    expected_source_ids = {
        node["source_node_id"]
        for framework in candidate_input["candidate_frameworks"]
        for node in framework["first_level_nodes"]
    }
    user_prompt = json.dumps(candidate_input, ensure_ascii=False, indent=2)
    client = DeepSeekJsonClient(
        load_chat_key(),
        model_id=DEFAULT_MODEL_ID,
        base_url=DEFAULT_BASE_URL,
        timeout_seconds=600.0,
        thinking_enabled=True,
    )
    started = time.perf_counter()
    try:
        result: JsonChatResult = await client.complete_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
    except InductionFailure as error:
        return {
            "status": "failed",
            "prompt_version": PROMPT_VERSION,
            "error": str(error),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }

    errors, summary = validate_payload(result.payload, expected_source_ids)
    return {
        "status": "valid" if not errors else "invalid",
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "source_result_paths": [str(path) for path in SOURCE_RESULT_PATHS],
        "candidate_framework_count": len(
            candidate_input["candidate_frameworks"]
        ),
        "candidate_node_count": len(expected_source_ids),
        "requested_model_id": DEFAULT_MODEL_ID,
        "thinking_enabled": True,
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "actual_model_id": result.actual_model_id,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_hit_tokens": result.cache_hit_tokens,
        "cache_miss_tokens": result.cache_miss_tokens,
        "validation_errors": errors,
        "summary": summary,
        "payload": result.payload,
    }


def main() -> None:
    """运行归并探针并写入带时间戳的原始结果。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run())
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        BACKEND_ROOT / ".runtime" / f"role_knowledge_framework_merge_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "results.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    printable = {
        key: report.get(key)
        for key in (
            "status",
            "elapsed_seconds",
            "input_tokens",
            "output_tokens",
            "candidate_framework_count",
            "candidate_node_count",
            "validation_errors",
            "summary",
            "error",
        )
        if key in report
    }
    printable["output_path"] = str(output_path)
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
