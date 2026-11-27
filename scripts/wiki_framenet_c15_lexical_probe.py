"""用 FrameNet 框架族构造 C15 跨谓词抽取探针。"""

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

PROMPT_VERSION = "framenet-c15-lexical-v1"
METRIC_VERSION = "strict-and-normalized-participants-v1"
OUTPUT_PATH = BACKEND_ROOT / ".runtime" / "framenet_c15_lexical_latest" / "results.json"
CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"


@dataclass(frozen=True, slots=True)
class ExpectedRelation:
    """一个关系实例的人工预期端点。"""

    content: str
    source: str
    destination: str


@dataclass(frozen=True, slots=True)
class RelationCase:
    """一个来自 FrameNet 框架族或负例边界的探针输入。"""

    case_id: str
    frame: str
    text: str
    expected: tuple[ExpectedRelation, ...]


CASES = (
    RelationCase(
        "P1",
        "Giving",
        "The collector gave the filtered records to the indexing worker.",
        (ExpectedRelation("the filtered records", "The collector", "the indexing worker"),),
    ),
    RelationCase(
        "P2",
        "Transfer",
        "The gateway transferred the session transcript to the audit store.",
        (ExpectedRelation("the session transcript", "The gateway", "the audit store"),),
    ),
    RelationCase(
        "P3",
        "Sending",
        "The agent sent the generated summary to the memory module.",
        (ExpectedRelation("the generated summary", "The agent", "the memory module"),),
    ),
    RelationCase(
        "P4",
        "Communication",
        "The reviewer communicated the updated policy to the deployment agent.",
        (ExpectedRelation("the updated policy", "The reviewer", "the deployment agent"),),
    ),
    RelationCase(
        "P5",
        "Receiving",
        "The retrieval service received the query text from the user.",
        (ExpectedRelation("the query text", "the user", "The retrieval service"),),
    ),
    RelationCase(
        "P6",
        "Giving",
        "The configuration snapshot was handed over to the evaluation service by the release manager.",
        (
            ExpectedRelation(
                "The configuration snapshot",
                "the release manager",
                "the evaluation service",
            ),
        ),
    ),
    RelationCase(
        "P7",
        "Giving",
        "The collector passed a copy of the access logs along to the monitoring service.",
        (ExpectedRelation("a copy of the access logs", "The collector", "the monitoring service"),),
    ),
    RelationCase(
        "P8",
        "Sending",
        "The collector sent the access logs to the gateway. "
        "The gateway forwarded the same access logs to the audit store.",
        (
            ExpectedRelation("the access logs", "The collector", "the gateway"),
            ExpectedRelation("the same access logs", "The gateway", "the audit store"),
        ),
    ),
    RelationCase(
        "N1",
        "Using",
        "The retrieval service used the cached document to draft a summary.",
        (),
    ),
    RelationCase(
        "N2",
        "Creating",
        "The scheduler created an empty task record.",
        (),
    ),
    RelationCase(
        "N3",
        "Locative_relation",
        "The audit log is stored in the encrypted volume.",
        (),
    ),
    RelationCase(
        "N4",
        "Possession",
        "The operator owns the signing key.",
        (),
    ),
    RelationCase(
        "N5",
        "Success",
        "The batch job passed after the review.",
        (),
    ),
    RelationCase(
        "N6",
        "Constraint",
        "The policy restricts the agent to read-only access.",
        (),
    ),
)


SYSTEM_PROMPT = """你是原文关系判定器。

判断目标：

C15 来源、去向与流转：某个对象、内容、材料、数据、知识、主张或信息来自哪个上游对象、过程或主体，
或经过复制、转发、引用、传递等关系流向哪个下游对象、过程或主体。

任务：

1. 找出原文明确表达 C15 关系的句子或连续子句。
2. 每个关系必须同时给出内容、上游来源和下游去向；三者都必须是原文实际出现的词面。
3. 只记录原文明确表达的传递、提供、发送、接收、复制、转发或引用关系。仅说明使用、生成、存放、
   拥有、成功或约束，不能据此补写 C15。
4. evidence 必须逐字复制支持该关系的原文句子或连续子句。
5. 没有明确关系时返回空数组。不要为了覆盖原文而补充。
6. 只返回 JSON 对象，不要 Markdown 或额外说明。

返回结构：

{
  "relations": [
    {
      "content": "原文中的内容词面",
      "source": "原文中的上游词面",
      "destination": "原文中的下游词面",
      "evidence": "逐字原文证据"
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


def _user_prompt(case: RelationCase) -> str:
    return f"原文：\n{case.text}\n\n请抽取原文明确表达的 C15 关系。"


def _validate_relations(payload: dict[str, Any], text: str) -> list[dict[str, str]]:
    raw_relations = payload.get("relations")
    if not isinstance(raw_relations, list):
        raise TypeError("relations_must_be_array")

    relations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in raw_relations:
        if not isinstance(item, dict):
            raise TypeError("relation_must_be_object")

        content = item.get("content")
        source = item.get("source")
        destination = item.get("destination")
        evidence = item.get("evidence")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                content,
                source,
                destination,
                evidence,
            )
        ):
            raise ValueError("relation_fields_must_be_non_empty_strings")

        normalized = {
            "content": content.strip(),
            "source": source.strip(),
            "destination": destination.strip(),
            "evidence": evidence.strip(),
        }
        for field in ("content", "source", "destination"):
            if normalized[field] not in text:
                raise ValueError(f"participant_not_in_source:{field}")
        if normalized["evidence"] not in text:
            raise ValueError("evidence_not_in_source")

        key = (
            normalized["content"],
            normalized["source"],
            normalized["destination"],
        )
        if key not in seen:
            seen.add(key)
            relations.append(normalized)
    return relations


def _normalize_participant(surface: str) -> str:
    """只消除冠词、大小写和空白差异，保留其余语义词面。"""
    normalized = " ".join(surface.split()).casefold()
    for article in ("the ", "a ", "an "):
        if normalized.startswith(article):
            return normalized[len(article) :]
    return normalized


def _normalized_relation_set(
    relations: set[tuple[str, str, str]],
) -> set[tuple[str, str, str]]:
    """返回忽略冠词、大小写和空白后的关系端点集合。"""
    return {tuple(_normalize_participant(value) for value in relation) for relation in relations}


async def _run_once(
    client: DeepSeekJsonClient,
    case: RelationCase,
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
                relations = _validate_relations(result.payload, case.text)
                predicted = {
                    (relation["content"], relation["source"], relation["destination"])
                    for relation in relations
                }
                expected = {
                    (relation.content, relation.source, relation.destination)
                    for relation in case.expected
                }
                normalized_predicted = _normalized_relation_set(predicted)
                normalized_expected = _normalized_relation_set(expected)
                return {
                    "case_id": case.case_id,
                    "frame": case.frame,
                    "run": run_index,
                    "attempt": attempt,
                    "relations": relations,
                    "expected": [
                        {
                            "content": relation.content,
                            "source": relation.source,
                            "destination": relation.destination,
                        }
                        for relation in case.expected
                    ],
                    "missing": [list(item) for item in sorted(expected - predicted)],
                    "extra": [list(item) for item in sorted(predicted - expected)],
                    "exact_match": predicted == expected,
                    "expected_hit": expected.issubset(predicted),
                    "normalized_missing": [
                        list(item) for item in sorted(normalized_expected - normalized_predicted)
                    ],
                    "normalized_extra": [
                        list(item) for item in sorted(normalized_predicted - normalized_expected)
                    ],
                    "normalized_exact_match": normalized_predicted == normalized_expected,
                    "normalized_expected_hit": normalized_expected.issubset(normalized_predicted),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cache_hit_tokens": result.cache_hit_tokens,
                    "cache_miss_tokens": result.cache_miss_tokens,
                    "actual_model_id": result.actual_model_id,
                }
            except (InductionFailure, TypeError, ValueError) as error:
                last_error = error
                if attempt < 3:
                    await asyncio.sleep(0.5 * attempt)
        raise RuntimeError(f"framenet_c15_probe_failed:{case.case_id}:{run_index}:{last_error}")


def _summarize(results: list[dict[str, Any]], runs_per_case: int) -> dict[str, Any]:
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        by_case[result["case_id"]].append(result)

    summary: dict[str, Any] = {
        "runs_per_case": runs_per_case,
        "cases": {},
        "total_exact_match": sum(1 for result in results if result["exact_match"]),
        "total_expected_hit": sum(1 for result in results if result["expected_hit"]),
        "total_extras": sum(len(result["extra"]) for result in results),
        "total_normalized_exact_match": sum(
            1 for result in results if result["normalized_exact_match"]
        ),
        "total_normalized_expected_hit": sum(
            1 for result in results if result["normalized_expected_hit"]
        ),
        "total_normalized_extras": sum(len(result["normalized_extra"]) for result in results),
        "total_input_tokens": sum(result["input_tokens"] for result in results),
        "total_output_tokens": sum(result["output_tokens"] for result in results),
    }
    for case in CASES:
        case_results = by_case[case.case_id]
        relation_counts = Counter(
            (relation["content"], relation["source"], relation["destination"])
            for result in case_results
            for relation in result["relations"]
        )
        summary["cases"][case.case_id] = {
            "frame": case.frame,
            "text": case.text,
            "expected": [
                {
                    "content": relation.content,
                    "source": relation.source,
                    "destination": relation.destination,
                }
                for relation in case.expected
            ],
            "exact_match_count": sum(1 for result in case_results if result["exact_match"]),
            "expected_hit_count": sum(1 for result in case_results if result["expected_hit"]),
            "normalized_exact_match_count": sum(
                1 for result in case_results if result["normalized_exact_match"]
            ),
            "normalized_expected_hit_count": sum(
                1 for result in case_results if result["normalized_expected_hit"]
            ),
            "relation_counts": [
                {
                    "content": key[0],
                    "source": key[1],
                    "destination": key[2],
                    "count": count,
                }
                for key, count in relation_counts.items()
            ],
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
    results = await asyncio.gather(
        *(
            _run_once(client, case, run_index, semaphore)
            for case in CASES
            for run_index in range(1, args.runs + 1)
        )
    )
    return {
        "prompt_version": PROMPT_VERSION,
        "metric_version": METRIC_VERSION,
        "requested_model_id": args.model,
        "runs_per_case": args.runs,
        "framenet_frames": sorted({case.frame for case in CASES}),
        "system_prompt": SYSTEM_PROMPT,
        "summary": _summarize(results, args.runs),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    report = asyncio.run(run(args))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"\nraw_results={OUTPUT_PATH}")


if __name__ == "__main__":
    main()
