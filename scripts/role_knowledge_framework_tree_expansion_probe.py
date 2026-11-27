"""验证单个一级知识节点的完整子树展开提示词。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.induction import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
    InductionFailure,
    JsonChatResult,
)
from role_knowledge_framework_task_coverage_audit_probe import load_chat_key

PROMPT_VERSION = "role-knowledge-framework-tree-expansion-v1"
EXPERIMENT_DOCUMENT = (
    PROJECT_ROOT
    / "docs"
    / "development"
    / "experiments"
    / "2026-11-23-角色知识框架两阶段分层生成.md"
)
FRAMEWORK_RESULT_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_coverage_loop_20261123T105116215557Z"
    / "final_framework.json"
)
DOMAIN_NAME = "AI Agent"
ROLE_NAME = "系统架构师"
TARGET_NODE_ID = "K08"
MAX_OUTPUT_TOKENS = 49_152
MAX_ATTEMPTS = 3


def load_system_prompt() -> str:
    """从实验文档读取已经审阅的子树展开系统提示词。"""

    document = EXPERIMENT_DOCUMENT.read_text(encoding="utf-8")
    start = document.index("#### 10.17.2 展开器系统提示词")
    end = document.index("#### 10.17.3", start)
    section = document[start:end]
    match = re.search(r"```text\r?\n(.*?)\r?\n```", section, re.DOTALL)
    if match is None:
        raise RuntimeError("tree_expansion_system_prompt_missing")
    return match.group(1)


def load_input(target_node_id: str = TARGET_NODE_ID) -> dict[str, Any]:
    """构造单节点展开调用的五个顶层输入字段。"""

    framework = json.loads(FRAMEWORK_RESULT_PATH.read_text(encoding="utf-8"))
    target_node = next(
        node
        for node in framework["first_level_nodes"]
        if node["node_id"] == target_node_id
    )
    return {
        "domain_name": DOMAIN_NAME,
        "role_name": ROLE_NAME,
        "framework_scope": framework["framework_scope"],
        "first_level_directory": framework["first_level_nodes"],
        "target_node": target_node,
    }


def normalized_title(title: str) -> str:
    """生成用于重复标题检测的保守规范化形式。"""

    return re.sub(r"\s+", "", title).casefold()


def validate_tree_payload(
    payload: dict[str, Any],
    target_node_id: str = TARGET_NODE_ID,
) -> tuple[list[str], dict[str, Any]]:
    """校验单节点子树结构，并汇总深度、分支和叶节点指标。"""

    errors: list[str] = []
    if set(payload) != {"parent_node_id", "children"}:
        errors.append("root_fields_invalid")
    if payload.get("parent_node_id") != target_node_id:
        errors.append("parent_node_id_mismatch")
    raw_children = payload.get("children")
    if not isinstance(raw_children, list) or not raw_children:
        return errors + ["children_missing"], {}

    titles: list[str] = []
    node_count = 0
    leaf_count = 0
    branch_node_count = 0
    max_depth = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal node_count, leaf_count, branch_node_count, max_depth
        node_count += 1
        max_depth = max(max_depth, depth)
        if not isinstance(node, dict):
            errors.append(f"node_not_object:depth={depth}")
            return
        if set(node) != {"title", "description", "exclusions", "children"}:
            errors.append(f"node_fields_invalid:depth={depth}")
        title = node.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"node_title_missing:depth={depth}")
        else:
            titles.append(normalized_title(title))
        description = node.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"node_description_missing:depth={depth}")
        exclusions = node.get("exclusions")
        if (
            not isinstance(exclusions, list)
            or any(not isinstance(item, str) or not item.strip() for item in exclusions)
        ):
            errors.append(f"node_exclusions_invalid:depth={depth}")
        children = node.get("children")
        if not isinstance(children, list):
            errors.append(f"node_children_invalid:depth={depth}")
            return
        if len(children) == 1:
            errors.append(f"single_child_container:depth={depth}")
        if children:
            branch_node_count += 1
            for child in children:
                visit(child, depth + 1)
        else:
            leaf_count += 1

    for child in raw_children:
        visit(child, 1)

    duplicate_titles = sorted(
        title for title in set(titles) if titles.count(title) > 1
    )
    errors.extend(f"node_title_duplicate:{title}" for title in duplicate_titles)
    summary = {
        "direct_child_count": len(raw_children),
        "descendant_node_count": node_count,
        "leaf_node_count": leaf_count,
        "branch_node_count": branch_node_count,
        "max_depth": max_depth,
        "duplicate_title_count": len(duplicate_titles),
    }
    return errors, summary


def assign_path_ids(children: list[dict[str, Any]], parent_path: str) -> list[dict[str, Any]]:
    """按先序位置为子树节点分配宿主路径标识。"""

    assigned: list[dict[str, Any]] = []
    for index, child in enumerate(children, start=1):
        path_id = f"{parent_path}.{index:02d}"
        assigned.append(
            {
                "node_id": path_id,
                "title": child["title"],
                "description": child["description"],
                "exclusions": child["exclusions"],
                "children": assign_path_ids(child["children"], path_id),
            }
        )
    return assigned


async def run(target_node_id: str = TARGET_NODE_ID) -> dict[str, Any]:
    """调用展开器，校验并以显式失败语义保存原始结果。"""

    system_prompt = load_system_prompt()
    user_input = load_input(target_node_id)
    base_user_prompt = json.dumps(user_input, ensure_ascii=False, indent=2)
    current_user_prompt = base_user_prompt
    client = DeepSeekJsonClient(
        load_chat_key(),
        model_id=DEFAULT_MODEL_ID,
        base_url=DEFAULT_BASE_URL,
        timeout_seconds=600.0,
        thinking_enabled=True,
    )
    attempts: list[dict[str, Any]] = []
    started = time.perf_counter()

    for attempt_index in range(MAX_ATTEMPTS):
        attempt_started = time.perf_counter()
        result: JsonChatResult | None = None
        try:
            result = await client.complete_json(
                system_prompt=system_prompt,
                user_prompt=current_user_prompt,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            errors, summary = validate_tree_payload(result.payload, target_node_id)
        except InductionFailure as error:
            errors = [str(error)]
            summary = {}
        attempts.append(
            {
                "attempt": attempt_index + 1,
                "elapsed_seconds": round(time.perf_counter() - attempt_started, 3),
                "validation_errors": errors,
                "input_tokens": result.input_tokens if result else 0,
                "output_tokens": result.output_tokens if result else 0,
                "cache_hit_tokens": result.cache_hit_tokens if result else 0,
                "cache_miss_tokens": result.cache_miss_tokens if result else 0,
            }
        )
        if result is not None and not errors:
            expanded_children = assign_path_ids(result.payload["children"], target_node_id)
            return {
                "status": "valid",
                "prompt_version": PROMPT_VERSION,
                "prompt_sha256": hashlib.sha256(
                    system_prompt.encode("utf-8")
                ).hexdigest(),
                "system_prompt": system_prompt,
                "input": user_input,
                "requested_model_id": DEFAULT_MODEL_ID,
                "actual_model_id": result.actual_model_id,
                "thinking_enabled": True,
                "temperature": 0.0,
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "attempts": attempts,
                "summary": summary,
                "raw_children": result.payload["children"],
                "expanded_children": expanded_children,
            }
        feedback = (
            "\n\n上一次输出未通过宿主校验。必须修正后重新输出完整 JSON。错误："
            + json.dumps(errors, ensure_ascii=False)
        )
        current_user_prompt = base_user_prompt + feedback

    return {
        "status": "failed",
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest(),
        "system_prompt": system_prompt,
        "input": user_input,
        "requested_model_id": DEFAULT_MODEL_ID,
        "thinking_enabled": True,
        "temperature": 0.0,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "attempts": attempts,
    }


def main() -> None:
    """执行展开探针并写入原始输出。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run())
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        BACKEND_ROOT
        / ".runtime"
        / f"role_knowledge_framework_tree_expansion_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "results.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if report["status"] == "valid":
        tree_path = output_dir / "expanded_tree.json"
        tree_path.write_text(
            json.dumps(
                {
                    "parent_node_id": TARGET_NODE_ID,
                    "children": report["expanded_children"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    else:
        tree_path = None
    printable = {
        "status": report["status"],
        "elapsed_seconds": report["elapsed_seconds"],
        "attempt_count": len(report["attempts"]),
        "summary": report.get("summary"),
        "last_validation_errors": report["attempts"][-1]["validation_errors"],
        "output_path": str(output_path),
        "tree_path": str(tree_path) if tree_path else None,
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
