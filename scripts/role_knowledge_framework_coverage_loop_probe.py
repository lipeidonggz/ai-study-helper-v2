"""执行任务覆盖审查与知识框架修订的自动闭环探针。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import role_knowledge_framework_task_coverage_audit_probe as audit_probe
from app.dual_entry_index.induction import (
    DeepSeekJsonClient,
    InductionFailure,
    JsonChatResult,
)

EDITOR_PROMPT_VERSION = "role-knowledge-framework-revision-v1"
LOOP_VERSION = "role-knowledge-framework-coverage-loop-v1"
MAX_ROUNDS = 6
MAX_ATTEMPTS_PER_STAGE = 3
MAX_OUTPUT_TOKENS = 49_152
EXPERIMENT_DOCUMENT = (
    PROJECT_ROOT
    / "docs"
    / "development"
    / "experiments"
    / "2026-11-23-角色知识框架两阶段分层生成.md"
)
INITIAL_AUDIT_RESULT_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_task_coverage_audit_20261123T085513797690Z"
    / "results.json"
)
NODE_ID_PATTERN = re.compile(r"^K\d{2,}$")


def load_editor_prompt() -> str:
    """从实验文档读取框架修订器系统提示词。"""

    document = EXPERIMENT_DOCUMENT.read_text(encoding="utf-8")
    start = document.index("#### 10.16.1 框架修订器系统提示词")
    end = document.index("#### 10.16.2", start)
    section = document[start:end]
    match = re.search(r"```text\r?\n(.*?)\r?\n```", section, re.DOTALL)
    if match is None:
        raise RuntimeError("framework_editor_system_prompt_missing")
    return match.group(1)


def validate_editor_payload(
    payload: dict[str, Any],
    expected_node_ids: set[str],
) -> list[str]:
    """校验修订器输出保持原节点身份并满足一级框架结构。"""

    errors: list[str] = []
    expected_fields = {"domain", "role", "framework_scope", "first_level_nodes"}
    if set(payload) != expected_fields:
        errors.append("framework_fields_invalid")
    if payload.get("domain") != audit_probe.DOMAIN_NAME:
        errors.append("framework_domain_mismatch")
    if payload.get("role") != audit_probe.ROLE_NAME:
        errors.append("framework_role_mismatch")
    framework_scope = payload.get("framework_scope")
    if not isinstance(framework_scope, str) or not framework_scope.strip():
        errors.append("framework_scope_missing")

    raw_nodes = payload.get("first_level_nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        return errors + ["first_level_nodes_missing"]

    node_ids: list[str] = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            errors.append(f"node_not_object:{index}")
            continue
        if set(node) != {"node_id", "title", "description", "exclusions"}:
            errors.append(f"node_fields_invalid:{index}")
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or NODE_ID_PATTERN.fullmatch(node_id) is None:
            errors.append(f"node_id_invalid:{index}")
        else:
            node_ids.append(node_id)
        for field in ("title", "description"):
            value = node.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"node_{field}_missing:{index}")
        exclusions = node.get("exclusions")
        if (
            not isinstance(exclusions, list)
            or not exclusions
            or any(not isinstance(item, str) or not item.strip() for item in exclusions)
        ):
            errors.append(f"node_exclusions_invalid:{index}")

    duplicate_node_ids = sorted(
        node_id for node_id in set(node_ids) if node_ids.count(node_id) > 1
    )
    errors.extend(f"node_id_duplicate:{node_id}" for node_id in duplicate_node_ids)
    missing_node_ids = sorted(expected_node_ids - set(node_ids))
    errors.extend(f"existing_node_removed:{node_id}" for node_id in missing_node_ids)
    return errors


def extract_framework(payload: dict[str, Any]) -> dict[str, Any]:
    """从修订输出提取可再次送入覆盖审查的知识框架。"""

    return {
        "framework_scope": payload["framework_scope"],
        "first_level_nodes": payload["first_level_nodes"],
    }


def framework_diff(
    previous_framework: dict[str, Any],
    current_framework: dict[str, Any],
) -> dict[str, Any]:
    """汇总一次修订相对上一框架的新增、修改和未变节点。"""

    previous_nodes = {
        node["node_id"]: node for node in previous_framework["first_level_nodes"]
    }
    current_nodes = {
        node["node_id"]: node for node in current_framework["first_level_nodes"]
    }
    added = sorted(set(current_nodes) - set(previous_nodes))
    changed = sorted(
        node_id
        for node_id in set(previous_nodes) & set(current_nodes)
        if previous_nodes[node_id] != current_nodes[node_id]
    )
    unchanged = sorted(
        node_id
        for node_id in set(previous_nodes) & set(current_nodes)
        if previous_nodes[node_id] == current_nodes[node_id]
    )
    return {
        "added_node_ids": added,
        "changed_node_ids": changed,
        "unchanged_node_ids": unchanged,
        "changed": bool(added or changed)
        or previous_framework["framework_scope"]
        != current_framework["framework_scope"],
    }


def coverage_signature(payload: dict[str, Any]) -> str:
    """生成用于无进展检测的任务状态与缺口范围签名。"""

    gaps = {
        gap["gap_id"]: gap["scope"]
        for gap in payload.get("gap_register", [])
        if isinstance(gap, dict)
        and isinstance(gap.get("gap_id"), str)
        and isinstance(gap.get("scope"), str)
    }
    entries = []
    for task in payload.get("task_coverage", []):
        if not isinstance(task, dict):
            continue
        gap_scopes = sorted(
            gaps.get(gap_id, gap_id)
            for gap_id in task.get("gap_ids", [])
            if isinstance(gap_id, str)
        )
        entries.append(
            {
                "task_id": task.get("task_id"),
                "status": task.get("status"),
                "gap_scopes": gap_scopes,
            }
        )
    return json.dumps(sorted(entries, key=lambda item: str(item["task_id"])),
                      ensure_ascii=False,
                      sort_keys=True)


def is_fully_covered(payload: dict[str, Any], task_count: int) -> bool:
    """判断审查结果是否达到闭环成功条件。"""

    coverage = payload.get("task_coverage")
    gaps = payload.get("gap_register")
    return (
        isinstance(coverage, list)
        and len(coverage) == task_count
        and all(task.get("status") == "covered" for task in coverage)
        and gaps == []
    )


async def complete_validated_json(
    *,
    client: DeepSeekJsonClient,
    system_prompt: str,
    user_payload: dict[str, Any],
    validator: Callable[[dict[str, Any]], list[str]],
    diagnostic_scope: str,
) -> tuple[JsonChatResult, list[dict[str, Any]]]:
    """执行模型调用，并在结构无效时携带校验错误重试。"""

    base_prompt = json.dumps(user_payload, ensure_ascii=False, indent=2)
    current_prompt = base_prompt
    attempts: list[dict[str, Any]] = []
    last_errors = ["call_not_attempted"]
    for attempt_index in range(MAX_ATTEMPTS_PER_STAGE):
        started = time.perf_counter()
        result: JsonChatResult | None = None
        try:
            result = await client.complete_json(
                system_prompt=system_prompt,
                user_prompt=current_prompt,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            errors = validator(result.payload)
        except InductionFailure as error:
            errors = [str(error)]
        elapsed = round(time.perf_counter() - started, 3)
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "elapsed_seconds": elapsed,
                "validation_errors": errors,
                "input_tokens": result.input_tokens if result else 0,
                "output_tokens": result.output_tokens if result else 0,
            }
        )
        if result is not None and not errors:
            return result, attempts
        last_errors = errors
        feedback = (
            "\n\n上一次输出未通过宿主校验。必须修正后重新输出完整 JSON。错误："
            + json.dumps(last_errors, ensure_ascii=False)
        )
        current_prompt = base_prompt + feedback
    raise InductionFailure(
        f"{diagnostic_scope}_validation_retry_exhausted:{last_errors}"
    )


async def run_loop() -> dict[str, Any]:
    """从已有首轮审查开始，执行修订与复审闭环。"""

    base_input = audit_probe.load_candidate_input()
    audit_system_prompt = audit_probe.load_system_prompt()
    editor_system_prompt = load_editor_prompt()
    expected_tasks = {
        task["task_id"]: task["task"] for task in base_input["primary_tasks"]
    }
    framework = copy.deepcopy(base_input["knowledge_framework"])
    initial_audit_report = json.loads(
        INITIAL_AUDIT_RESULT_PATH.read_text(encoding="utf-8")
    )
    initial_audit = initial_audit_report["payload"]
    initial_errors, initial_summary = audit_probe.validate_payload(
        initial_audit,
        expected_tasks,
        {node["node_id"] for node in framework["first_level_nodes"]},
    )
    if initial_errors:
        raise RuntimeError(f"initial_audit_invalid:{initial_errors}")

    client = DeepSeekJsonClient(
        audit_probe.load_chat_key(),
        model_id=audit_probe.DEFAULT_MODEL_ID,
        base_url=audit_probe.DEFAULT_BASE_URL,
        timeout_seconds=600.0,
        thinking_enabled=True,
    )
    started = time.perf_counter()
    round_records: list[dict[str, Any]] = [
        {
            "round": 1,
            "source": "reused_initial_audit",
            "audit_path": str(INITIAL_AUDIT_RESULT_PATH),
            "framework": framework,
            "audit_summary": initial_summary,
            "audit_payload": initial_audit,
        }
    ]
    total_input_tokens = 0
    total_output_tokens = 0
    state = "running"
    current_audit = initial_audit
    previous_signature = coverage_signature(current_audit)
    round_number = 1

    if is_fully_covered(current_audit, len(expected_tasks)):
        state = "completed"
    else:
        while round_number < MAX_ROUNDS:
            editor_node_ids = {
                node["node_id"] for node in framework["first_level_nodes"]
            }
            editor_input = {
                "domain_name": audit_probe.DOMAIN_NAME,
                "role_name": audit_probe.ROLE_NAME,
                "primary_tasks": base_input["primary_tasks"],
                "current_framework": framework,
                "coverage_audit": current_audit,
            }
            editor_result, editor_attempts = await complete_validated_json(
                client=client,
                system_prompt=editor_system_prompt,
                user_payload=editor_input,
                validator=lambda payload, ids=editor_node_ids: validate_editor_payload(
                    payload,
                    ids,
                ),
                diagnostic_scope=f"framework_editor_round_{round_number}",
            )
            total_input_tokens += sum(
                attempt["input_tokens"] for attempt in editor_attempts
            )
            total_output_tokens += sum(
                attempt["output_tokens"] for attempt in editor_attempts
            )
            revised_framework = extract_framework(editor_result.payload)
            diff = framework_diff(framework, revised_framework)
            round_records[-1]["revision"] = {
                "attempts": editor_attempts,
                "prompt_sha256": hashlib.sha256(
                    editor_system_prompt.encode("utf-8")
                ).hexdigest(),
                "output": editor_result.payload,
                "diff": diff,
            }
            if not diff["changed"]:
                state = "no_framework_change"
                break

            framework = revised_framework
            round_number += 1
            audit_input = {
                **base_input,
                "knowledge_framework": framework,
            }
            current_node_ids = {
                node["node_id"] for node in framework["first_level_nodes"]
            }
            audit_result, audit_attempts = await complete_validated_json(
                client=client,
                system_prompt=audit_system_prompt,
                user_payload=audit_input,
                validator=lambda payload, ids=current_node_ids, tasks=expected_tasks: audit_probe.validate_payload(
                    payload,
                    tasks,
                    ids,
                )[0],
                diagnostic_scope=f"coverage_audit_round_{round_number}",
            )
            total_input_tokens += sum(
                attempt["input_tokens"] for attempt in audit_attempts
            )
            total_output_tokens += sum(
                attempt["output_tokens"] for attempt in audit_attempts
            )
            audit_errors, audit_summary = audit_probe.validate_payload(
                audit_result.payload,
                expected_tasks,
                current_node_ids,
            )
            if audit_errors:
                raise RuntimeError(f"audit_validation_failed:{audit_errors}")
            current_audit = audit_result.payload
            round_records.append(
                {
                    "round": round_number,
                    "source": "generated",
                    "framework": framework,
                    "audit_attempts": audit_attempts,
                    "audit_summary": audit_summary,
                    "audit_payload": current_audit,
                }
            )
            if is_fully_covered(current_audit, len(expected_tasks)):
                state = "completed"
                break
            current_signature = coverage_signature(current_audit)
            if current_signature == previous_signature:
                state = "no_coverage_progress"
                break
            previous_signature = current_signature
        else:
            state = "iteration_limit"

    return {
        "status": state,
        "loop_version": LOOP_VERSION,
        "editor_prompt_version": EDITOR_PROMPT_VERSION,
        "editor_prompt_sha256": hashlib.sha256(
            editor_system_prompt.encode("utf-8")
        ).hexdigest(),
        "audit_prompt_sha256": hashlib.sha256(
            audit_system_prompt.encode("utf-8")
        ).hexdigest(),
        "requested_model_id": audit_probe.DEFAULT_MODEL_ID,
        "thinking_enabled": True,
        "temperature": 0.0,
        "max_rounds": MAX_ROUNDS,
        "round_count": round_number,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "additional_input_tokens": total_input_tokens,
        "additional_output_tokens": total_output_tokens,
        "final_framework": framework,
        "final_audit_summary": (
            round_records[-1].get("audit_summary") if round_records else None
        ),
        "final_audit_payload": current_audit,
        "round_records": round_records,
    }


def main() -> None:
    """运行闭环探针并写入完整轮次产物。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run_loop())
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        BACKEND_ROOT
        / ".runtime"
        / f"role_knowledge_framework_coverage_loop_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "results.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    final_framework_path = output_dir / "final_framework.json"
    final_framework_path.write_text(
        json.dumps(report["final_framework"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    printable = {
        "status": report["status"],
        "round_count": report["round_count"],
        "elapsed_seconds": report["elapsed_seconds"],
        "additional_input_tokens": report["additional_input_tokens"],
        "additional_output_tokens": report["additional_output_tokens"],
        "final_audit_summary": report["final_audit_summary"],
        "output_path": str(output_path),
        "final_framework_path": str(final_framework_path),
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
