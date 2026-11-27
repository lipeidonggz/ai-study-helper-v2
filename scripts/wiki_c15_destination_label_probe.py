"""验证 C15 去向二级标签的赋值稳定性与证据边界。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
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

PROMPT_VERSION = "stage51-c15-destination-labels-v1"
OUTPUT_PATH = BACKEND_ROOT / ".runtime" / "c15_destination_label_latest" / "results.json"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"
ALLOWED_LABELS = frozenset({"direct_destination", "indirect_destination", "final_destination"})


@dataclass(frozen=True, slots=True)
class DestinationCase:
    """一个带人工预期去向标签的探针输入。"""

    case_id: str
    text: str
    target: str
    expected: tuple[tuple[str, str], ...]


CASES = (
    DestinationCase(
        "D1",
        "A 将一份数据交给 B。",
        "A 发出的这份数据",
        (("direct_destination", "B"),),
    ),
    DestinationCase(
        "D2",
        "A 将一份数据交给 B；B 把这同一份数据原封不动地交给 C。",
        "A 发出的这份数据",
        (("direct_destination", "B"), ("indirect_destination", "C")),
    ),
    DestinationCase(
        "D3",
        "A 将一份数据交给 B；B 把这同一份数据原封不动地交给 C。C 收下后不再继续向下游传递。",
        "A 发出的这份数据",
        (
            ("direct_destination", "B"),
            ("indirect_destination", "C"),
            ("final_destination", "C"),
        ),
    ),
    DestinationCase(
        "D4",
        "A 将同一份数据分别交给 B 和 C。",
        "A 发出的这份数据",
        (("direct_destination", "B"), ("direct_destination", "C")),
    ),
    DestinationCase(
        "D5",
        "C 采集了一份数据并交给 A；A 随后把这同一份数据原封不动地交给 B。",
        "C 采集的这份数据",
        (("direct_destination", "A"), ("indirect_destination", "B")),
    ),
    DestinationCase(
        "D6",
        "A 自己采集了一份数据。",
        "A 采集的这份数据",
        (),
    ),
    DestinationCase(
        "D7",
        "A 将一份数据交给 B，原文明确说 B 是最终接收者。",
        "A 发出的这份数据",
        (("direct_destination", "B"), ("final_destination", "B")),
    ),
)


SYSTEM_PROMPT = """你是关系二级标签判定器。

输入包含：
1. 一段原文；
2. 一个目标对象；
3. C15“来源、去向与流传承继”的去向候选二级标签。

你的任务是列出原文能够明确证明的目标对象去向标签。同一去向对象可以同时具有多个标签。

候选标签：

- direct_destination（直接去向）：目标对象直接流向、交给、发送给或传递给该去向对象，二者之间没有原文给出的中间对象。
- indirect_destination（间接去向）：目标对象经过至少一个原文明确给出的中间对象后到达该去向对象，因此它是更下游、但非直接的去向。
- final_destination（最终去向）：原文明确把该去向对象认定为传递终点、最终接收者或不再继续向下游传递的位置。不能仅因为当前可见链路到该对象为止就推断它是最终去向。

证据规则：

1. 只能使用当前原文明确表达的内容，不得引入外部知识或补写链外对象。
2. 多个原文子句只有在明确表达“同一份数据”、原样复制、继续转发或派生连续性时，才可以组成去向链。
3. 可以返回零个、一个或多个标签；同一个去向对象可以同时具有 direct_destination 和 final_destination 等标签。
4. 每个结果必须给出原文中实际出现的去向对象 surface。
5. reason 只简短说明原文证据，不写内部推理。
6. 只返回 JSON 对象，不要 Markdown 或额外说明。

返回结构：

{
  "labels": [
    {
      "label": "候选标签标识符",
      "object": "原文中实际出现的去向对象",
      "reason": "短理由"
    }
  ]
}
"""


def _load_chat_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def _user_prompt(case: DestinationCase) -> str:
    return f"""原文：
{case.text}

目标对象：
{case.target}

请判断目标对象的去向二级标签。"""


def _validate_labels(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw_labels = payload.get("labels")
    if not isinstance(raw_labels, list):
        raise TypeError("labels_missing")
    labels: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_labels):
        if not isinstance(raw, dict):
            raise TypeError(f"label_not_object:{index}")
        label = raw.get("label")
        surface = raw.get("object")
        reason = raw.get("reason")
        if label not in ALLOWED_LABELS:
            raise TypeError(f"label_invalid:{label}")
        if not isinstance(surface, str) or not surface.strip():
            raise TypeError(f"label_object_missing:{index}")
        if not isinstance(reason, str) or not reason.strip():
            raise TypeError(f"label_reason_missing:{index}")
        key = (label, surface.strip())
        if key in seen:
            raise TypeError(f"label_duplicate:{label}:{surface}")
        seen.add(key)
        labels.append({"label": label, "object": surface.strip(), "reason": reason.strip()})
    return labels


async def _run_once(
    client: DeepSeekJsonClient,
    case: DestinationCase,
    run_index: int,
    semaphore: asyncio.Semaphore,
) -> dict[str, Any]:
    async with semaphore:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                result = await client.complete_json(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=_user_prompt(case),
                    max_output_tokens=1_500,
                )
                labels = _validate_labels(result.payload)
                predicted = {(item["label"], item["object"]) for item in labels}
                expected = set(case.expected)
                return {
                    "case_id": case.case_id,
                    "text": case.text,
                    "target": case.target,
                    "run": run_index,
                    "attempt": attempt,
                    "expected": [
                        {"label": label, "object": surface} for label, surface in case.expected
                    ],
                    "labels": labels,
                    "missing": [
                        {"label": label, "object": surface}
                        for label, surface in sorted(expected - predicted)
                    ],
                    "extra": [
                        {"label": label, "object": surface}
                        for label, surface in sorted(predicted - expected)
                    ],
                    "exact_match": predicted == expected,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "actual_model_id": result.actual_model_id,
                }
            except (InductionFailure, TypeError, ValueError) as error:
                last_error = error
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"label_probe_failed:{case.case_id}:{run_index}:{last_error}")


def _summarize(results: list[dict[str, Any]], runs_per_case: int) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "runs_per_case": runs_per_case,
        "cases": {},
        "total_input_tokens": sum(result["input_tokens"] for result in results),
        "total_output_tokens": sum(result["output_tokens"] for result in results),
    }
    for case in CASES:
        case_results = [result for result in results if result["case_id"] == case.case_id]
        predicted_label_counts = Counter(
            item["label"] for result in case_results for item in result["labels"]
        )
        summary["cases"][case.case_id] = {
            "text": case.text,
            "target": case.target,
            "expected": [{"label": label, "object": surface} for label, surface in case.expected],
            "exact_match_count": sum(1 for result in case_results if result["exact_match"]),
            "predicted_label_counts": dict(predicted_label_counts),
            "runs": case_results,
        }
    return summary


async def run(args: argparse.Namespace) -> dict[str, Any]:
    client = DeepSeekJsonClient(
        _load_chat_key(),
        model_id=args.model,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        _run_once(client, case, run_index, semaphore)
        for case in CASES
        for run_index in range(1, args.runs + 1)
    ]
    results = await asyncio.gather(*tasks)
    return {
        "prompt_version": PROMPT_VERSION,
        "requested_model_id": args.model,
        "runs_per_case": args.runs,
        "system_prompt": SYSTEM_PROMPT,
        "summary": _summarize(results, args.runs),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run(args))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"\nraw_results={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
