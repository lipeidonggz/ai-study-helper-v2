from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.citation_source_meta import (
    DEFAULT_CITATION_SOURCE_META_PROMPT,
    CitationSourceMetaClassifier,
    CitationSourceMetaPrompt,
)
from app.agent.citations import CitationSpan, split_answer_spans
from app.agent.token_counter import ConservativeObservationTokenCounter
from app.evaluation.database import EvaluationDatabase
from app.evaluation.repository import EvaluationRepository
from app.providers.chat import DeepSeekChatProvider
from app.storage.credentials import CHAT_API_KEY_USERNAME, WindowsCredentialStore

PRODUCT_ROOT = Path.home() / "AppData" / "Local" / "com.lipeidong.aistudyhelper"
EVAL_ROOT = PRODUCT_ROOT / "eval-data"
DEFAULT_CONCURRENCY = 3


class GoldOverride(BaseModel):
    """按正文片段覆盖默认来源元期望。"""

    model_config = ConfigDict(extra="forbid")

    text_contains: str = Field(min_length=1)
    source_meta: bool


class GoldCase(BaseModel):
    """一个冻结用例的默认标签与局部反例覆盖。"""

    model_config = ConfigDict(extra="forbid")

    default_source_meta: bool
    overrides: list[GoldOverride] = Field(default_factory=list)


class SourceMetaGold(BaseModel):
    """来源元分类实验使用的可版本化金标准。"""

    model_config = ConfigDict(extra="forbid")

    version: str
    review_status: str
    cases: dict[str, GoldCase]


def load_source_meta_gold(path: Path) -> SourceMetaGold:
    """读取并校验来源元实验金标准。"""

    return SourceMetaGold.model_validate_json(path.read_text(encoding="utf-8"))


def resolve_expected_labels(
    *,
    case_id: str,
    spans: list[CitationSpan],
    gold: SourceMetaGold,
) -> dict[str, bool]:
    """把用例级默认值和唯一文本覆盖解析成逐 span 标签。"""

    case = gold.cases.get(case_id)
    if case is None:
        raise ValueError(f"source meta gold is missing case: {case_id}")
    expected = {span.span_id: case.default_source_meta for span in spans}
    for override in case.overrides:
        matches = [span for span in spans if override.text_contains in span.text]
        if len(matches) > 1:
            raise ValueError(
                f"source meta gold override is ambiguous: {case_id}: {override.text_contains}"
            )
        if matches:
            expected[matches[0].span_id] = override.source_meta
    return expected


def summarize_records(
    *,
    records: list[dict[str, Any]],
    spans_by_attempt: dict[str, list[CitationSpan]],
    expected_by_attempt: dict[str, dict[str, bool]],
    repeats: int,
) -> dict[str, Any]:
    """按 span 和答案运行统计正确性、K=9、错杀与漏放。"""

    decision_counts: dict[str, Counter[bool | None]] = defaultdict(Counter)
    category_counts: dict[str, Counter[str]] = defaultdict(Counter)
    reason_counts: dict[str, Counter[str]] = defaultdict(Counter)
    false_exclusion_count = 0
    false_pass_count = 0
    false_exclusion_runs = 0
    false_pass_runs = 0
    protocol_errors = 0
    records_by_attempt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        records_by_attempt[record["attempt_id"]].append(record)
        if record["status"] != "complete":
            protocol_errors += 1
    for attempt_id, attempt_records in records_by_attempt.items():
        expected = expected_by_attempt[attempt_id]
        for repeat_index in range(repeats):
            record = next(
                (item for item in attempt_records if item["repeat"] == repeat_index + 1),
                None,
            )
            has_false_exclusion = False
            has_false_pass = False
            for span in spans_by_attempt[attempt_id]:
                key = f"{attempt_id}:{span.span_id}"
                decision = None if record is None else record["decisions"].get(span.span_id)
                if decision is None:
                    decision_counts[key][None] += 1
                    category_counts[key]["missing"] += 1
                    reason_counts[key]["missing"] += 1
                    actual = None
                else:
                    actual = decision["source_meta"]
                    decision_counts[key][actual] += 1
                    category_counts[key][decision["category"] or "body"] += 1
                    reason_counts[key][decision["reason"] or "empty"] += 1
                expected_value = expected[span.span_id]
                if actual is True and expected_value is False:
                    false_exclusion_count += 1
                    has_false_exclusion = True
                elif actual is False and expected_value is True:
                    false_pass_count += 1
                    has_false_pass = True
            false_exclusion_runs += int(has_false_exclusion)
            false_pass_runs += int(has_false_pass)

    correctness: dict[str, int] = {}
    for key, distribution in decision_counts.items():
        attempt_id, span_id = key.split(":", 1)
        expected_value = expected_by_attempt[attempt_id][span_id]
        correctness[key] = distribution[expected_value]
    unstable = [
        {
            "span": key,
            "distribution": {
                "body": distribution[False],
                "source_meta": distribution[True],
                "missing": distribution[None],
            },
            "category_distribution": dict(category_counts[key]),
            "reason_distribution": dict(reason_counts[key]),
            "correct_count": correctness[key],
        }
        for key, distribution in decision_counts.items()
        if correctness[key] < repeats
    ]
    return {
        "span_count": len(decision_counts),
        "correct_all": sum(value == repeats for value in correctness.values()),
        "correct_at_least_9": sum(value >= min(9, repeats) for value in correctness.values()),
        "protocol_errors": protocol_errors,
        "false_exclusion_decisions": false_exclusion_count,
        "false_pass_decisions": false_pass_count,
        "false_exclusion_runs": false_exclusion_runs,
        "false_pass_runs": false_pass_runs,
        "answer_run_count": len(records_by_attempt) * repeats,
        "unstable_spans": unstable,
    }


def _parse_args() -> argparse.Namespace:
    """读取实验 run、金标准、候选提示词和输出路径。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--prompt-version", default="candidate")
    parser.add_argument("--list-spans", action="store_true")
    return parser.parse_args()


def _load_prompt(path: Path | None, version: str) -> CitationSourceMetaPrompt:
    """读取候选提示词；未提供时使用产品默认提示词。"""

    if path is None:
        return DEFAULT_CITATION_SOURCE_META_PROMPT
    return CitationSourceMetaPrompt(
        version=version,
        system_prompt=path.read_text(encoding="utf-8"),
    )


async def _run(args: argparse.Namespace) -> None:
    """执行冻结样本上的重复分类实验。"""

    repository = EvaluationRepository(EvaluationDatabase(EVAL_ROOT))
    run = repository.get_run(args.run_id)
    case_revisions = {
        revision.revision_id: revision
        for revision in repository.get_case_revisions(
            list({attempt.case_revision_id for attempt in run.attempts})
        )
    }
    attempts = [
        attempt
        for attempt in run.attempts
        if attempt.outcome_class == "product"
        and (not args.case_id or case_revisions[attempt.case_revision_id].case_id in args.case_id)
    ]
    gold = load_source_meta_gold(args.gold)
    if args.list_spans:
        listed_attempts = []
        for attempt in attempts:
            case_revision = case_revisions[attempt.case_revision_id]
            spans = split_answer_spans(attempt.answer or "")
            expected = resolve_expected_labels(
                case_id=case_revision.case_id,
                spans=spans,
                gold=gold,
            )
            listed_attempts.append(
                {
                    "attempt_id": attempt.attempt_id,
                    "case_id": case_revision.case_id,
                    "spans": [
                        {
                            "span_id": span.span_id,
                            "structure_role": span.structure_role,
                            "text": span.text,
                            "expected_source_meta": expected[span.span_id],
                        }
                        for span in spans
                    ],
                }
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(listed_attempts, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"wrote {len(listed_attempts)} attempts to {args.output}")
        return
    api_key = WindowsCredentialStore().get(CHAT_API_KEY_USERNAME)
    if not api_key:
        raise RuntimeError("chat API key is not configured")
    prompt = _load_prompt(args.prompt_file, args.prompt_version)
    classifier = CitationSourceMetaClassifier(
        DeepSeekChatProvider(api_key),
        token_counter=ConservativeObservationTokenCounter(),
        prompt=prompt,
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    spans_by_attempt: dict[str, list[CitationSpan]] = {}
    expected_by_attempt: dict[str, dict[str, bool]] = {}
    records: list[dict[str, Any]] = []

    async def classify_attempt(attempt: Any) -> None:
        case_revision = case_revisions[attempt.case_revision_id]
        spans = split_answer_spans(attempt.answer or "")
        spans_by_attempt[attempt.attempt_id] = spans
        expected_by_attempt[attempt.attempt_id] = resolve_expected_labels(
            case_id=case_revision.case_id,
            spans=spans,
            gold=gold,
        )
        for repeat in range(1, args.repeats + 1):
            async with semaphore:
                outcome = await classifier.classify(
                    question=case_revision.question,
                    answer=attempt.answer or "",
                    spans=spans,
                    spent_input_tokens=0,
                    spent_output_tokens=0,
                    max_context_tokens=100_000,
                    max_request_tokens=400_000,
                )
            records.append(
                {
                    "attempt_id": attempt.attempt_id,
                    "case_id": case_revision.case_id,
                    "repeat": repeat,
                    "status": outcome.status,
                    "error_code": outcome.error_code,
                    "error_message": outcome.error_message,
                    "model_calls": outcome.model_calls,
                    "input_tokens": outcome.input_tokens,
                    "output_tokens": outcome.output_tokens,
                    "decisions": {
                        item.span_id: {
                            "source_meta": item.source_meta,
                            "category": item.category,
                            "reason": item.reason,
                        }
                        for item in outcome.decisions
                    },
                    "raw_outputs": list(outcome.raw_outputs),
                }
            )
        print(
            json.dumps(
                {"case_id": case_revision.case_id, "span_count": len(spans)},
                ensure_ascii=False,
            ),
            flush=True,
        )

    await asyncio.gather(*(classify_attempt(attempt) for attempt in attempts))
    summary = summarize_records(
        records=records,
        spans_by_attempt=spans_by_attempt,
        expected_by_attempt=expected_by_attempt,
        repeats=args.repeats,
    )
    result = {
        "run_id": args.run_id,
        "gold_version": gold.version,
        "gold_review_status": gold.review_status,
        "prompt_version": prompt.version,
        "prompt_hash": prompt.content_hash,
        "repeats": args.repeats,
        "attempt_count": len(attempts),
        **summary,
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "records"},
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


def main() -> None:
    """命令行入口。"""

    asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    main()
