"""验证基于已审核一级任务的知识框架覆盖审查提示词。"""

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

PROMPT_VERSION = "role-knowledge-framework-task-coverage-audit-v1"
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
PRIMARY_TASK_RESULT_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_primary_task_merge_v2_reviewed_20261122T075745558Z"
    / "results.json"
)
KNOWLEDGE_FRAMEWORK_RESULT_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_merge_20261123T084452663722Z"
    / "results.json"
)
VALID_STATUSES = {"covered", "partial", "missing"}
VALID_PLACEMENTS = {"existing", "new_scope"}


def load_chat_key() -> str:
    """从环境变量或 Windows Credential Manager 读取 DeepSeek Key。"""

    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def load_system_prompt() -> str:
    """从实验文档读取已经审核的覆盖审查系统提示词。"""

    document = EXPERIMENT_DOCUMENT.read_text(encoding="utf-8")
    start = document.index("#### 10.15.1 系统提示词")
    end = document.index("#### 10.15.2", start)
    section = document[start:end]
    match = re.search(r"```text\r?\n(.*?)\r?\n```", section, re.DOTALL)
    if match is None:
        raise RuntimeError("coverage_audit_system_prompt_missing")
    return match.group(1)


def load_candidate_input() -> dict[str, Any]:
    """加载已审核主要任务和当前归并后的知识框架。"""

    task_report = json.loads(PRIMARY_TASK_RESULT_PATH.read_text(encoding="utf-8"))
    framework_report = json.loads(
        KNOWLEDGE_FRAMEWORK_RESULT_PATH.read_text(encoding="utf-8")
    )
    tasks = []
    for task in task_report["payload"]["primary_tasks"]:
        tasks.append(
            {
                "task_id": task["task_key"],
                "task": task["task"],
                "completed_when": task["completed_when"],
                "inclusions": task["inclusions"],
                "exclusions": task["exclusions"],
            }
        )
    framework = framework_report["payload"]
    nodes = [
        {
            "node_id": node["node_id"],
            "title": node["title"],
            "description": node["description"],
            "exclusions": node["exclusions"],
        }
        for node in framework["first_level_nodes"]
    ]
    return {
        "domain_name": DOMAIN_NAME,
        "role_name": ROLE_NAME,
        "primary_tasks": tasks,
        "knowledge_framework": {
            "framework_scope": framework["framework_scope"],
            "first_level_nodes": nodes,
        },
    }


def validate_payload(
    payload: dict[str, Any],
    expected_tasks: dict[str, str],
    expected_node_ids: set[str],
) -> tuple[list[str], dict[str, Any]]:
    """校验任务覆盖、节点引用和缺口账本的一致性。"""

    errors: list[str] = []
    if payload.get("domain") != DOMAIN_NAME:
        errors.append("domain_mismatch")
    if payload.get("role") != ROLE_NAME:
        errors.append("role_mismatch")

    task_coverage = payload.get("task_coverage")
    if not isinstance(task_coverage, list):
        errors.append("task_coverage_missing")
        task_coverage = []
    gap_register = payload.get("gap_register")
    if not isinstance(gap_register, list):
        errors.append("gap_register_missing")
        gap_register = []

    coverage_task_ids: list[str] = []
    status_counts = {status: 0 for status in sorted(VALID_STATUSES)}
    task_gap_ids: dict[str, set[str]] = {}
    covered_node_ids: set[str] = set()
    for task_entry in task_coverage:
        if not isinstance(task_entry, dict):
            errors.append("task_coverage_entry_not_object")
            continue
        expected_fields = {
            "task_id",
            "task",
            "required_knowledge",
            "status",
            "covered_by_node_ids",
            "gap_ids",
            "reason",
        }
        if set(task_entry) != expected_fields:
            errors.append(f"task_coverage_fields_invalid:{task_entry.get('task_id')}")
        task_id = task_entry.get("task_id")
        if not isinstance(task_id, str):
            errors.append("task_id_missing")
            continue
        coverage_task_ids.append(task_id)
        if task_id not in expected_tasks:
            errors.append(f"task_id_unexpected:{task_id}")
            continue
        if task_entry.get("task") != expected_tasks[task_id]:
            errors.append(f"task_text_mismatch:{task_id}")

        required_knowledge = task_entry.get("required_knowledge")
        if not isinstance(required_knowledge, list) or not required_knowledge:
            errors.append(f"required_knowledge_missing:{task_id}")
        elif any(
            not isinstance(item, str) or not item.strip()
            for item in required_knowledge
        ):
            errors.append(f"required_knowledge_invalid:{task_id}")

        status = task_entry.get("status")
        if status not in VALID_STATUSES:
            errors.append(f"task_status_invalid:{task_id}")
        else:
            status_counts[status] += 1

        node_ids = task_entry.get("covered_by_node_ids")
        if not isinstance(node_ids, list):
            errors.append(f"covered_by_node_ids_invalid:{task_id}")
        else:
            for node_id in node_ids:
                if not isinstance(node_id, str):
                    errors.append(f"covered_by_node_id_not_string:{task_id}")
                    continue
                covered_node_ids.add(node_id)
                if node_id not in expected_node_ids:
                    errors.append(f"covered_by_node_missing:{task_id}:{node_id}")

        gap_ids = task_entry.get("gap_ids")
        if not isinstance(gap_ids, list):
            errors.append(f"task_gap_ids_invalid:{task_id}")
            gap_ids = []
        elif any(not isinstance(gap_id, str) for gap_id in gap_ids):
            errors.append(f"task_gap_id_not_string:{task_id}")
            gap_ids = [gap_id for gap_id in gap_ids if isinstance(gap_id, str)]
        if status == "covered" and gap_ids:
            errors.append(f"covered_task_has_gaps:{task_id}")
        if status in {"partial", "missing"} and not gap_ids:
            errors.append(f"uncovered_task_without_gap:{task_id}")
        task_gap_ids[task_id] = set(gap_ids)

        if not isinstance(task_entry.get("reason"), str) or not task_entry[
            "reason"
        ].strip():
            errors.append(f"task_reason_missing:{task_id}")

    duplicate_task_ids = sorted(
        task_id
        for task_id in set(coverage_task_ids)
        if coverage_task_ids.count(task_id) > 1
    )
    errors.extend(f"task_id_duplicate:{task_id}" for task_id in duplicate_task_ids)
    missing_task_ids = sorted(set(expected_tasks) - set(coverage_task_ids))
    errors.extend(f"task_id_missing:{task_id}" for task_id in missing_task_ids)

    gap_ids: list[str] = []
    placement_counts = {placement: 0 for placement in sorted(VALID_PLACEMENTS)}
    gap_required_tasks: dict[str, set[str]] = {}
    suggested_node_ids: set[str] = set()
    for gap in gap_register:
        if not isinstance(gap, dict):
            errors.append("gap_entry_not_object")
            continue
        expected_fields = {
            "gap_id",
            "scope",
            "description",
            "why_required",
            "required_for_task_ids",
            "suggested_parent_node_ids",
            "placement",
        }
        if set(gap) != expected_fields:
            errors.append(f"gap_fields_invalid:{gap.get('gap_id')}")
        gap_id = gap.get("gap_id")
        if not isinstance(gap_id, str) or not gap_id.strip():
            errors.append("gap_id_missing")
            continue
        gap_ids.append(gap_id)
        for field_name in ("scope", "description", "why_required"):
            field_value = gap.get(field_name)
            if not isinstance(field_value, str) or not field_value.strip():
                errors.append(f"gap_{field_name}_missing:{gap_id}")

        required_for_task_ids = gap.get("required_for_task_ids")
        if not isinstance(required_for_task_ids, list) or not required_for_task_ids:
            errors.append(f"gap_task_ids_missing:{gap_id}")
            required_for_task_ids = []
        valid_required_task_ids: set[str] = set()
        for task_id in required_for_task_ids:
            if not isinstance(task_id, str):
                errors.append(f"gap_task_id_not_string:{gap_id}")
                continue
            if task_id not in expected_tasks:
                errors.append(f"gap_task_id_unexpected:{gap_id}:{task_id}")
                continue
            valid_required_task_ids.add(task_id)
        gap_required_tasks[gap_id] = valid_required_task_ids

        raw_parent_ids = gap.get("suggested_parent_node_ids")
        if not isinstance(raw_parent_ids, list):
            errors.append(f"gap_parent_ids_invalid:{gap_id}")
        else:
            for node_id in raw_parent_ids:
                if not isinstance(node_id, str):
                    errors.append(f"gap_parent_id_not_string:{gap_id}")
                    continue
                suggested_node_ids.add(node_id)
                if node_id not in expected_node_ids:
                    errors.append(f"gap_parent_missing:{gap_id}:{node_id}")

        placement = gap.get("placement")
        if placement not in VALID_PLACEMENTS:
            errors.append(f"gap_placement_invalid:{gap_id}")
        else:
            placement_counts[placement] += 1

    duplicate_gap_ids = sorted(
        gap_id for gap_id in set(gap_ids) if gap_ids.count(gap_id) > 1
    )
    errors.extend(f"gap_id_duplicate:{gap_id}" for gap_id in duplicate_gap_ids)

    referenced_gap_ids = {
        gap_id for task_ids in task_gap_ids.values() for gap_id in task_ids
    }
    errors.extend(
        f"task_gap_id_unregistered:{gap_id}"
        for gap_id in sorted(referenced_gap_ids - set(gap_ids))
    )
    errors.extend(
        f"gap_unreferenced_by_task:{gap_id}"
        for gap_id in sorted(set(gap_ids) - referenced_gap_ids)
    )
    for task_id, referenced_ids in task_gap_ids.items():
        for gap_id in referenced_ids:
            if task_id not in gap_required_tasks.get(gap_id, set()):
                errors.append(f"gap_task_reference_forward_missing:{gap_id}:{task_id}")
    for gap_id, required_task_ids in gap_required_tasks.items():
        for task_id in required_task_ids:
            if gap_id not in task_gap_ids.get(task_id, set()):
                errors.append(f"gap_task_reference_reverse_missing:{gap_id}:{task_id}")

    summary = {
        "task_count": len(task_coverage),
        "status_counts": status_counts,
        "covered_node_count": len(covered_node_ids),
        "gap_count": len(gap_register),
        "placement_counts": placement_counts,
        "suggested_parent_node_count": len(suggested_node_ids),
        "gap_scopes": [
            gap.get("scope") for gap in gap_register if isinstance(gap, dict)
        ],
    }
    return errors, summary


async def run() -> dict[str, Any]:
    """执行一次覆盖审查，并保留输入、原始输出和校验结果。"""

    system_prompt = load_system_prompt()
    candidate_input = load_candidate_input()
    expected_tasks = {
        task["task_id"]: task["task"] for task in candidate_input["primary_tasks"]
    }
    expected_node_ids = {
        node["node_id"]
        for node in candidate_input["knowledge_framework"]["first_level_nodes"]
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

    errors, summary = validate_payload(
        result.payload,
        expected_tasks,
        expected_node_ids,
    )
    return {
        "status": "valid" if not errors else "invalid",
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "primary_task_result_path": str(PRIMARY_TASK_RESULT_PATH),
        "knowledge_framework_result_path": str(KNOWLEDGE_FRAMEWORK_RESULT_PATH),
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
    """运行覆盖审查探针并写入带时间戳的原始结果。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run())
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        BACKEND_ROOT
        / ".runtime"
        / f"role_knowledge_framework_task_coverage_audit_{timestamp}"
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
