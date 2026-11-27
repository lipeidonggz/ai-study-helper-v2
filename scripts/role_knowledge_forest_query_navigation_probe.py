"""验证主 Agent 能否基于完整知识目录完成跨分支求全类节点发现。"""

# 角色知识框架探针共享同一实验链；模块导入位于 sys.path 初始化之后。
# ruff: noqa: E402, E501

from __future__ import annotations

import asyncio
import hashlib
import json
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
)
from role_knowledge_framework_task_coverage_audit_probe import load_chat_key

FOREST_PATH = (
    BACKEND_ROOT
    / ".runtime"
    / "role_knowledge_framework_tree_expansion_batch_20261123T113731009302Z"
    / "role_knowledge_forest.json"
)
CONCURRENCY = 4
MAX_OUTPUT_TOKENS = 16_384

QUERIES: list[dict[str, Any]] = [
    {
        "query_id": "Q1_DATA_LINEAGE",
        "question": (
            "在 AI Agent 系统中，为了实现数据从来源、加工、进入知识库、被检索使用，"
            "以及后续更新和删除时全程可追溯，需要掌握哪些数据血缘相关知识？"
        ),
        "required_roots": ["K10", "K17", "K23"],
        "required_groups": [
            {"name": "technical_lineage", "any": ["K10.10.03"]},
            {"name": "knowledge_update", "any": ["K10.09.01", "K10.09.02"]},
            {"name": "deletion_traceability", "any": ["K10.13.02"]},
            {"name": "lineage_governance", "any": ["K17.06"]},
            {
                "name": "source_and_rights",
                "any": ["K17.07.01", "K17.07.05"],
            },
            {"name": "context_audit_event", "any": ["K23.01.03.04"]},
            {"name": "audit_event_association", "any": ["K23.02", "K23.02.02"]},
            {"name": "audit_replay", "any": ["K23.05.01"]},
        ],
    },
    {
        "query_id": "Q2_ISOLATION_BOUNDARIES",
        "question": "AI Agent 领域中都有哪些隔离和控制边界？",
        "required_roots": ["K01", "K04", "K07", "K08", "K10", "K16", "K18", "K24"],
        "required_groups": [
            {"name": "code_sandbox", "any": ["K08.05.01"]},
            {"name": "resource_isolation", "any": ["K08.05.03"]},
            {"name": "tool_runtime_isolation", "any": ["K08.05.04"]},
            {"name": "trust_boundary", "any": ["K16.01.02"]},
            {"name": "tool_capability_boundary", "any": ["K16.03.01", "K16.03.02"]},
            {"name": "runtime_sandbox_security", "any": ["K16.06.01"]},
            {"name": "guardrail_boundary", "any": ["K16.06.02"]},
            {
                "name": "autonomy_boundary",
                "any": ["K16.07.01", "K18.02", "K01.02.02"],
            },
            {"name": "data_isolation", "any": ["K10.12"]},
            {"name": "policy_execution_point", "any": ["K24.02.02"]},
            {"name": "gateway_tenant_boundary", "any": ["K24.03.03"]},
            {
                "name": "policy_runtime_boundary",
                "any": ["K24.07.01", "K24.07.02"],
            },
            {"name": "tool_side_effect_boundary", "any": ["K04.06.03"]},
            {"name": "hierarchical_control_boundary", "any": ["K07.06.01"]},
        ],
    },
    {
        "query_id": "Q3_FAILURE_RECOVERY_REGRESSION",
        "question": (
            "AI Agent 多步任务失败后，应如何分类、检测、评估影响、恢复，并通过测试和"
            "回归确认恢复有效？"
        ),
        "required_roots": ["K03", "K08", "K11", "K12", "K13"],
        "required_groups": [
            {"name": "planning_recovery", "any": ["K03.05.03"]},
            {"name": "runtime_timeout_cancel", "any": ["K08.04.02"]},
            {"name": "runtime_retry", "any": ["K08.04.03"]},
            {"name": "runtime_checkpoint", "any": ["K08.04.04"]},
            {"name": "human_interrupt_recovery", "any": ["K08.04.05"]},
            {"name": "reliability_acceptance", "any": ["K11.07.01"]},
            {"name": "regression_suite", "any": ["K11.08", "K11.08.01"]},
            {"name": "change_impact", "any": ["K11.08.02"]},
            {"name": "behavior_drift", "any": ["K11.08.03"]},
            {"name": "root_cause", "any": ["K12.08.03"]},
            {"name": "incident_review", "any": ["K12.09.03"]},
            {"name": "failure_taxonomy", "any": ["K13.02"]},
            {"name": "failure_detection", "any": ["K13.03.01"]},
            {"name": "failure_impact", "any": ["K13.03.02"]},
            {"name": "recoverability", "any": ["K13.03.03"]},
            {"name": "recovery_strategies", "any": ["K13.04"]},
            {"name": "state_recovery", "any": ["K13.04.02"]},
            {"name": "recovery_completion", "any": ["K13.05.04"]},
        ],
    },
    {
        "query_id": "Q4_PERMISSION_APPROVAL",
        "question": (
            "AI Agent 的权限、最小权限、策略执行、人工审批、接管和授权留痕涉及哪些"
            "知识？"
        ),
        "required_roots": [
            "K01",
            "K03",
            "K04",
            "K08",
            "K16",
            "K17",
            "K18",
            "K23",
            "K24",
        ],
        "required_groups": [
            {"name": "planning_policy_constraint", "any": ["K03.06.03"]},
            {"name": "tool_auth_contract", "any": ["K04.06.01"]},
            {"name": "tool_authorization_scope", "any": ["K04.06.02"]},
            {"name": "tool_side_effect_confirmation", "any": ["K04.06.03"]},
            {"name": "runtime_human_interrupt", "any": ["K08.04.05"]},
            {"name": "least_privilege_tool_check", "any": ["K16.03.02"]},
            {"name": "delegated_authorization", "any": ["K16.04.02"]},
            {"name": "human_safety_takeover", "any": ["K16.07.02"]},
            {"name": "data_access_governance", "any": ["K17.05"]},
            {"name": "human_decision_authority", "any": ["K18.01.02"]},
            {"name": "autonomy_level", "any": ["K18.02.01", "K01.02.02"]},
            {"name": "control_mode_switch", "any": ["K18.02.02"]},
            {"name": "approval_interaction", "any": ["K18.04.01"]},
            {"name": "takeover_emergency_stop", "any": ["K18.04.02"]},
            {"name": "escalation_handover", "any": ["K18.04.03"]},
            {"name": "permission_audit_event", "any": ["K23.01.03.05"]},
            {"name": "manual_takeover_audit", "any": ["K23.01.03.08"]},
            {"name": "policy_model", "any": ["K24.01"]},
            {"name": "pdp_pep", "any": ["K24.02"]},
            {"name": "approval_gate", "any": ["K24.04"]},
            {"name": "authorization_credential", "any": ["K24.06"]},
            {
                "name": "policy_interaction_boundary",
                "any": ["K24.07.01", "K24.07.02"],
            },
        ],
    },
]


def load_forest() -> dict[str, Any]:
    """读取待导航的完整知识森林。"""

    return json.loads(FOREST_PATH.read_text(encoding="utf-8"))


def flatten_nodes(forest: dict[str, Any]) -> list[dict[str, Any]]:
    """按先序展开森林，并保留父路径。"""

    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], parent_id: str | None) -> None:
        nodes.append({**node, "parent_id": parent_id})
        for child in node["children"]:
            visit(child, node["node_id"])

    for root in forest["roots"]:
        visit(root, None)
    return nodes


def build_catalog(nodes: list[dict[str, Any]], include_description: bool) -> str:
    """生成紧凑目录，描述模式只增加 description，不加入排除项。"""

    lines: list[str] = []
    depth_by_id: dict[str, int] = {}
    for node in nodes:
        parent_id = node["parent_id"]
        depth = 0 if parent_id is None else depth_by_id[parent_id] + 1
        depth_by_id[node["node_id"]] = depth
        line = f"{'  ' * depth}{node['node_id']} | {node['title']}"
        if include_description:
            line += f" | {node['description']}"
        lines.append(line)
    return "\n".join(lines)


def build_system_prompt(catalog: str, mode: str) -> str:
    """构造跨分支目录导航提示词。"""

    return f"""你是一个知识目录导航 Agent。你只能从给定目录中选择节点，不直接回答领域问题。

目录模式：{mode}
目录包含完整的 24 个一级知识树。每个节点前面的编号是唯一 node_id。

【任务】
针对用户给出的求全类问题，找出完整回答所需的所有知识节点。必须遍历多个一级分支，
不能看到第一个相关分支就停止。

【选择规则】
1. 只返回目录中逐字存在的 node_id，不得编造编号。
2. 优先选择最具体的相关节点。
3. 如果选择父节点表示整棵相关子树，视为相关子节点均已覆盖；不要同时机械返回全部后代。
4. 如果只有父节点中的部分子节点相关，应返回这些具体子节点。
5. 为求全，可以接受边界相邻但确有回答价值的节点；明显无关的节点不要加入。
6. 根据问题语义跨分支组合节点，不要求目录中预先存在统一的跨分支主题。

【输出】
只输出 JSON：
{{
  "node_ids": ["K..."],
  "topic_groups": [
    {{"topic": "主题", "node_ids": ["K..."]}}
  ],
  "brief_reason": "不超过100字"
}}

【完整知识目录】
{catalog}
"""


def is_covered(selected_ids: set[str], acceptable_ids: list[str]) -> bool:
    """选择一个节点或其祖先，视为覆盖目标知识组。"""

    for target in acceptable_ids:
        parts = target.split(".")
        ancestors = {".".join(parts[:index]) for index in range(1, len(parts) + 1)}
        if selected_ids & ancestors:
            return True
    return False


def validate_selection(
    payload: dict[str, Any],
    known_ids: set[str],
    query: dict[str, Any],
) -> dict[str, Any]:
    """校验模型选择并计算一级分支与关键知识组覆盖。"""

    errors: list[str] = []
    raw_ids = payload.get("node_ids")
    if not isinstance(raw_ids, list) or any(not isinstance(item, str) for item in raw_ids):
        return {"errors": ["node_ids_invalid"], "selected_ids": []}
    selected_ids = list(dict.fromkeys(raw_ids))
    unknown_ids = sorted(set(selected_ids) - known_ids)
    if unknown_ids:
        errors.append("unknown_node_ids")
    selected_roots = {node_id.split(".")[0] for node_id in selected_ids if node_id in known_ids}
    missing_roots = sorted(set(query["required_roots"]) - selected_roots)
    selected_set = set(selected_ids)
    missing_groups = [
        group["name"]
        for group in query["required_groups"]
        if not is_covered(selected_set, group["any"])
    ]
    return {
        "errors": errors,
        "unknown_node_ids": unknown_ids,
        "selected_count": len(selected_ids),
        "selected_roots": sorted(selected_roots),
        "missing_roots": missing_roots,
        "missing_groups": missing_groups,
        "group_recall": round(
            (len(query["required_groups"]) - len(missing_groups))
            / len(query["required_groups"]),
            4,
        ),
    }


async def run_query(
    query: dict[str, Any],
    system_prompt: str,
    known_ids: set[str],
    client: DeepSeekJsonClient,
    semaphore: asyncio.Semaphore,
    mode: str,
    catalog_sha256: str,
) -> dict[str, Any]:
    """执行一次目录导航调用并保存原始选择结果。"""

    async with semaphore:
        print(f"START {mode} {query['query_id']}", flush=True)
        started = time.perf_counter()
        try:
            result = await client.complete_json(
                system_prompt=system_prompt,
                user_prompt=query["question"],
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            validation = validate_selection(result.payload, known_ids, query)
            status = "valid" if not validation["errors"] else "invalid"
            payload = result.payload
        except InductionFailure as error:
            result = None
            validation = {"errors": [str(error)], "selected_ids": []}
            status = "failed"
            payload = None
        elapsed = round(time.perf_counter() - started, 3)
        print(
            f"DONE  {mode} {query['query_id']} status={status} "
            f"recall={validation.get('group_recall')} elapsed={elapsed}",
            flush=True,
        )
        return {
            "query_id": query["query_id"],
            "question": query["question"],
            "mode": mode,
            "status": status,
            "catalog_sha256": catalog_sha256,
            "selected_ids": validation.get("selected_ids", payload.get("node_ids", []) if payload else []),
            "payload": payload,
            "validation": validation,
            "elapsed_seconds": elapsed,
            "input_tokens": result.input_tokens if result else 0,
            "output_tokens": result.output_tokens if result else 0,
            "cache_hit_tokens": result.cache_hit_tokens if result else 0,
            "cache_miss_tokens": result.cache_miss_tokens if result else 0,
        }


async def run_mode(
    nodes: list[dict[str, Any]],
    client: DeepSeekJsonClient,
    mode: str,
    include_description: bool,
) -> dict[str, Any]:
    """运行一种目录模式下的全部问题。"""

    catalog = build_catalog(nodes, include_description)
    system_prompt = build_system_prompt(catalog, mode)
    known_ids = {node["node_id"] for node in nodes}
    catalog_sha256 = hashlib.sha256(catalog.encode("utf-8")).hexdigest()
    semaphore = asyncio.Semaphore(CONCURRENCY)
    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            run_query(
                query,
                system_prompt,
                known_ids,
                client,
                semaphore,
                mode,
                catalog_sha256,
            )
            for query in QUERIES
        )
    )
    return {
        "mode": mode,
        "catalog_chars": len(catalog),
        "catalog_sha256": catalog_sha256,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "results": sorted(results, key=lambda item: item["query_id"]),
    }


async def run() -> dict[str, Any]:
    """运行标题目录与描述目录两个对照。"""

    forest = load_forest()
    nodes = flatten_nodes(forest)
    client = DeepSeekJsonClient(
        load_chat_key(),
        model_id=DEFAULT_MODEL_ID,
        base_url=DEFAULT_BASE_URL,
        timeout_seconds=600.0,
        thinking_enabled=True,
    )
    started = time.perf_counter()
    title_mode = await run_mode(nodes, client, "titles_only", False)
    description_mode = await run_mode(nodes, client, "title_and_description", True)
    return {
        "requested_model_id": DEFAULT_MODEL_ID,
        "thinking_enabled": True,
        "forest_path": str(FOREST_PATH),
        "node_count": len(nodes),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "title_mode": title_mode,
        "description_mode": description_mode,
    }


def main() -> None:
    """执行两组目录导航回放并写入结果。"""

    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run())
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output_dir = (
        BACKEND_ROOT
        / ".runtime"
        / f"role_knowledge_forest_query_navigation_{timestamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    output_path = output_dir / "results.json"
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    printable = {
        "elapsed_seconds": report["elapsed_seconds"],
        "title_mode": [
            {
                "query_id": result["query_id"],
                "status": result["status"],
                "selected_count": result["validation"].get("selected_count", 0),
                "missing_roots": result["validation"].get("missing_roots", []),
                "missing_groups": result["validation"].get("missing_groups", []),
                "group_recall": result["validation"].get("group_recall"),
            }
            for result in report["title_mode"]["results"]
        ],
        "description_mode": [
            {
                "query_id": result["query_id"],
                "status": result["status"],
                "selected_count": result["validation"].get("selected_count", 0),
                "missing_roots": result["validation"].get("missing_roots", []),
                "missing_groups": result["validation"].get("missing_groups", []),
                "group_recall": result["validation"].get("group_recall"),
            }
            for result in report["description_mode"]["results"]
        ],
        "output_path": str(output_path),
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
