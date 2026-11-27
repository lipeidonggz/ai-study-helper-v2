"""验证用户问题能否稳定映射为带作用域的关系分面表达式树。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
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
)
from wiki_axis_selection_probe import AXES as ALL_AXES
from wiki_axis_selection_probe import SECONDARY_LABELS

PROMPT_VERSION = "stage51-query-plan-v1-expression-tree"
OUTPUT_PATH = BACKEND_ROOT / ".runtime" / "query_plan_latest" / "results.json"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"
SCOPES = {"relation_instance", "block", "document"}


@dataclass(frozen=True, slots=True)
class QueryQuestion:
    """一个查询规划探针问题及其最低人工预期。"""

    question_id: str
    text: str
    expected_labels: tuple[tuple[str, tuple[str, ...]], ...]


QUESTIONS = (
    QueryQuestion(
        "c13-003",
        "在当前知识库中，除了沙箱、虚拟机和出口控制，AI Agent 还有哪些隔离或控制边界？"
        "请分别说明各限制什么。",
        (("C22", ("分离／隔断", "阻断／不可达")),),
    ),
    QueryQuestion(
        "isolation",
        "AI Agent 中的隔离都有哪些？",
        (("C22", ("分离／隔断", "阻断／不可达")),),
    ),
)


SYSTEM_PROMPT = """你是知识库问答系统的查询规划器。

输入包含：
1. 当前候选关系观察轴；
2. 每个轴可选择的二级关系标签；
3. 一个用户问题。

你的任务是生成关系分面表达式树，说明为了完整回答该问题，需要检索哪些关系实例。

叶节点格式：
{ "type": "label", "axis_id": "轴编号", "label": "该轴下的二级标签" }

运算符节点格式：
{
  "type": "operator",
  "operator": "AND" 或 "OR",
  "scope": "relation_instance"、"block" 或 "document",
  "children": [至少两个子节点]
}

判断规则：

1. 每个叶节点只能使用输入中给出的轴编号、标签和该标签所属的轴。
2. OR 表示任意子表达式提供所需证据即可，用于同一语义的备选关系或不同机制路径。
3. AND 表示同一求值作用域内必须同时满足全部子树。只有问题明确要求多个方面在同一关系实例、同一 block 或同一文档中同时成立时才使用。
4. 每个 AND 必须声明求值作用域。不能把多个相关轴无依据地求交。
5. “有哪些”不等于必须做类型归类；只有问题需要原文的分类或归属证据时才选择 C01。
6. “限制什么”不自动等于用途、一般因果或必要条件；如果所选关系实例自身已经包含被限制对象，不要再为了形式上的 AND 添加其他轴。
7. 只选择回答所必需的关系语义，不选择仅与主题相关的领域。
8. 如果一个关系叶节点已经足够，可以直接返回叶节点；否则返回运算符树。
9. 如果问题不需要任何候选关系语义，expression 返回 null。
10. 只返回 JSON 对象，不要 Markdown 或额外说明。

返回结构：
{
  "expression": 叶节点、运算符节点或 null,
  "reason": "简短说明哪些查询要求和作用域决定该表达式"
}
"""


def _load_chat_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def _active_axes(axis_set: str) -> tuple[Any, ...]:
    if axis_set == "without-c22":
        return tuple(axis for axis in ALL_AXES if axis.axis_id != "C22")
    return ALL_AXES


def _axis_prompt(axes: tuple[Any, ...]) -> str:
    blocks: list[str] = []
    for axis in axes:
        labels = "\n".join(
            f"    - {name}：{definition}" for name, definition in SECONDARY_LABELS[axis.axis_id]
        )
        blocks.append(
            f"- {axis.axis_id} {axis.name}：{axis.definition}\n  可选二级关系标签：\n{labels}"
        )
    return "\n".join(blocks)


def _user_prompt(question: QueryQuestion, axes: tuple[Any, ...]) -> str:
    return f"""当前候选关系观察轴：

{_axis_prompt(axes)}

用户问题：
{question.text}

请生成能够支持完整回答的关系分面表达式树。"""


def _validate_label(node: dict[str, Any], axes_by_id: dict[str, Any]) -> dict[str, Any]:
    if node.get("type") != "label":
        raise TypeError("leaf_type_must_be_label")
    axis_id = node.get("axis_id")
    label = node.get("label")
    if not isinstance(axis_id, str) or axis_id not in axes_by_id:
        raise ValueError("unknown_axis_id")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label_must_be_non_empty_string")
    label = label.strip()
    valid_labels = {name for name, _ in SECONDARY_LABELS[axis_id]}
    if label not in valid_labels:
        raise ValueError(f"label_not_in_axis:{axis_id}:{label}")
    return {"type": "label", "axis_id": axis_id, "label": label}


def _validate_expression(node: dict[str, Any], axes_by_id: dict[str, Any]) -> dict[str, Any]:
    node_type = node.get("type")
    if node_type == "label":
        return _validate_label(node, axes_by_id)
    if node_type != "operator":
        raise TypeError("expression_type_must_be_label_or_operator")

    operator = node.get("operator")
    if operator not in {"AND", "OR"}:
        raise ValueError("operator_must_be_AND_or_OR")
    scope = node.get("scope")
    if scope not in SCOPES:
        raise ValueError("operator_scope_invalid")

    raw_children = node.get("children")
    if not isinstance(raw_children, list) or len(raw_children) < 2:
        raise ValueError("operator_requires_at_least_two_children")
    children = [_validate_expression(child, axes_by_id) for child in raw_children]
    return {
        "type": "operator",
        "operator": operator,
        "scope": scope,
        "children": children,
    }


def _validate_payload(
    payload: dict[str, Any], axes: tuple[Any, ...]
) -> tuple[dict[str, Any] | None, str]:
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason_must_be_non_empty_string")
    expression = payload.get("expression")
    if expression is None:
        return None, reason.strip()
    if not isinstance(expression, dict):
        raise TypeError("expression_must_be_object_or_null")
    axes_by_id = {axis.axis_id: axis for axis in axes}
    return _validate_expression(expression, axes_by_id), reason.strip()


def _leaf_labels(expression: dict[str, Any] | None) -> list[tuple[str, str]]:
    if expression is None:
        return []
    if expression["type"] == "label":
        return [(expression["axis_id"], expression["label"])]
    labels: list[tuple[str, str]] = []
    for child in expression["children"]:
        labels.extend(_leaf_labels(child))
    return labels


def _operator_counts(expression: dict[str, Any] | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    if expression is None:
        return counts
    if expression["type"] == "operator":
        counts[expression["operator"]] += 1
        for child in expression["children"]:
            counts.update(_operator_counts(child))
    return counts


def _scope_sequence(expression: dict[str, Any] | None) -> list[str]:
    if expression is None:
        return []
    if expression["type"] == "label":
        return ["relation_instance"]
    scopes = [expression["scope"]]
    for child in expression["children"]:
        scopes.extend(_scope_sequence(child))
    return scopes


def _expected_hit(question: QueryQuestion, labels: list[tuple[str, str]]) -> bool:
    selected = set(labels)
    return all(
        any((axis_id, label) in selected for label in expected_labels)
        for axis_id, expected_labels in question.expected_labels
    )


async def _run_once(
    client: DeepSeekJsonClient,
    question: QueryQuestion,
    axes: tuple[Any, ...],
    axis_set: str,
    run_index: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = await client.complete_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=_user_prompt(question, axes),
                    max_output_tokens=2_000,
                )
                expression, reason = _validate_payload(result.payload, axes)
                labels = _leaf_labels(expression)
                return {
                    "question_id": question.question_id,
                    "question": question.text,
                    "axis_set": axis_set,
                    "run": run_index,
                    "attempt": attempt,
                    "expression": expression,
                    "reason": reason,
                    "leaf_labels": [
                        {"axis_id": axis_id, "label": label} for axis_id, label in labels
                    ],
                    "selected_axis_ids": list(dict.fromkeys(axis_id for axis_id, _ in labels)),
                    "operator_counts": dict(_operator_counts(expression)),
                    "scope_sequence": _scope_sequence(expression),
                    "expected_hit": _expected_hit(question, labels),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "actual_model_id": result.actual_model_id,
                }
            except (InductionFailure, TypeError, ValueError) as error:
                last_error = error
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"query_plan_failed:{question.question_id}:{run_index}:{last_error}")


def _summarize(results: list[dict[str, Any]], runs_per_question: int) -> dict[str, Any]:
    by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_question[result["question_id"]].append(result)

    summary: dict[str, Any] = {
        "runs_per_question": runs_per_question,
        "questions": {},
        "total_input_tokens": sum(result["input_tokens"] for result in results),
        "total_output_tokens": sum(result["output_tokens"] for result in results),
    }
    for question in QUESTIONS:
        if question.question_id not in by_question:
            continue
        question_results = by_question[question.question_id]
        leaf_set_counts = Counter(
            tuple(f"{leaf['axis_id']}:{leaf['label']}" for leaf in result["leaf_labels"])
            for result in question_results
        )
        summary["questions"][question.question_id] = {
            "question": question.text,
            "expected_labels": {
                axis_id: list(labels) for axis_id, labels in question.expected_labels
            },
            "expected_hit_count": sum(1 for result in question_results if result["expected_hit"]),
            "operator_sequence_counts": dict(
                Counter(
                    ",".join(
                        f"{operator}:{count}"
                        for operator, count in sorted(result["operator_counts"].items())
                    )
                    or "<leaf>"
                    for result in question_results
                )
            ),
            "root_scope_counts": dict(
                Counter(
                    result["scope_sequence"][0] if result["scope_sequence"] else "<empty>"
                    for result in question_results
                )
            ),
            "selected_leaf_set_counts": {
                ",".join(sequence) if sequence else "<empty>": count
                for sequence, count in leaf_set_counts.items()
            },
            "runs": question_results,
        }
    return summary


async def run(args: argparse.Namespace) -> dict[str, Any]:
    axes = _active_axes(args.axis_set)
    selected_questions = (
        QUESTIONS
        if args.question_id == "all"
        else tuple(q for q in QUESTIONS if q.question_id == args.question_id)
    )
    client = DeepSeekJsonClient(
        _load_chat_key(),
        model_id=args.model,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        _run_once(client, question, axes, args.axis_set, run_index, semaphore)
        for question in selected_questions
        for run_index in range(1, args.runs + 1)
    ]
    results = await asyncio.gather(*tasks)
    summary = _summarize(results, args.runs)
    return {
        "prompt_version": f"{PROMPT_VERSION}-{args.axis_set}",
        "requested_model_id": args.model,
        "axis_set": args.axis_set,
        "runs_per_question": args.runs,
        "axis_input_fields": [
            "axis_id",
            "name",
            "canonical_question",
            "secondary_labels[name,definition]",
        ],
        "expression_contract": {
            "leaf": "axis_id + secondary label",
            "operators": ["AND", "OR"],
            "scopes": sorted(SCOPES),
            "and_requires_scope": True,
        },
        "axes": [
            {
                "axis_id": axis.axis_id,
                "name": axis.name,
                "canonical_question": axis.definition,
                "secondary_labels": [
                    {"name": name, "definition": definition}
                    for name, definition in SECONDARY_LABELS[axis.axis_id]
                ],
            }
            for axis in axes
        ],
        "system_prompt": SYSTEM_PROMPT,
        "summary": summary,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument(
        "--axis-set",
        choices=("current-21", "without-c22"),
        default="current-21",
    )
    parser.add_argument(
        "--question-id",
        choices=("c13-003", "isolation", "all"),
        default="c13-003",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run(args))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"\nraw_results={args.output}")


if __name__ == "__main__":
    main()
