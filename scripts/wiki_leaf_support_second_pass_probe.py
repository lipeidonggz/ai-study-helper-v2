"""对首轮判定为支持的单叶实例执行一次 thinking 二次校准。"""

# 探针必须先设置 backend 与 scripts 搜索路径，之后才能导入项目模块。
# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_ROOT.parent / "backend"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import (
    DEEPSEEK_BASE_URL,
    ChatMessage,
    ChatProviderError,
    ChatProviderProfile,
    DeepSeekChatProvider,
)
from app.storage.credentials import CHAT_API_KEY_USERNAME, WindowsCredentialStore
from wiki_leaf_support_probe import (
    MODEL_ID,
    OUTPUT_ROOT,
    SUPPORT_TYPES,
    parse_and_validate,
)

SOURCE_RESULTS_PATH = OUTPUT_ROOT / "20261126T061318098571Z" / "results.json"
GOLD_PATH = OUTPUT_ROOT / "full_253_three_level_gold_20261125.json"
EXPECTED_SOURCE_PROMPT_SHA256 = "9e7ad73a23091972c964e9e0ebe6b6233fc1143db565afaf307009dcbf326c1a"
EXPECTED_SOURCE_SUPPORTED_COUNTS = {
    "direct_support": 337,
    "weak_support": 169,
    "no_support": 87,
}
EXPECTED_SOURCE_TOTAL_COUNTS = {
    "direct_support": 340,
    "weak_support": 190,
    "no_support": 2000,
}
SECOND_PASS_MAX_OUTPUT_TOKENS = 8192
DEFAULT_MAX_CONCURRENCY = 16
SUPPORTED_GRADES = frozenset({"direct_support", "weak_support"})


def load_json(path: Path) -> Any:
    """读取实验 JSON，并对缺失文件给出可操作错误。"""

    if not path.is_file():
        raise RuntimeError(f"file does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_text(text: str) -> str:
    """返回 UTF-8 文本的 SHA-256。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    """返回文件字节的 SHA-256。"""

    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_key(row: dict[str, Any]) -> str:
    """构造首轮叶子实例的稳定唯一键。"""

    return f"{row['leaf_id']}@{int(row['run_index']):02d}"


def parse_block_text(messages: list[dict[str, Any]]) -> str:
    """从首轮保存的完整用户消息中恢复唯一可用的 block 原文。"""

    if len(messages) != 2 or [item.get("role") for item in messages] != [
        "system",
        "user",
    ]:
        raise RuntimeError("source result must contain exactly one system and one user message")
    payload = json.loads(messages[1]["content"])
    block_text = payload.get("block", {}).get("text")
    if not isinstance(block_text, str) or not block_text:
        raise RuntimeError("source user message does not contain block.text")
    return block_text


def load_inputs(
    source_results_path: Path,
    gold_path: Path,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """加载并冻结首轮产物、三级金标准和支持实例集合。"""

    source_results = load_json(source_results_path)
    gold_rows = load_json(gold_path)
    if not isinstance(source_results, dict) or not isinstance(gold_rows, list):
        raise TypeError("source results or gold standard has an unexpected top-level type")
    if source_results.get("system_prompt_sha256") != EXPECTED_SOURCE_PROMPT_SHA256:
        raise RuntimeError(
            "source prompt mismatch: "
            f"expected={EXPECTED_SOURCE_PROMPT_SHA256} "
            f"actual={source_results.get('system_prompt_sha256')}"
        )
    if source_results.get("thinking") != "disabled":
        raise RuntimeError("source results must be the thinking-disabled first pass")

    gold_by_id: dict[str, dict[str, Any]] = {}
    for gold_row in gold_rows:
        leaf_id = gold_row.get("leaf_id")
        grade = gold_row.get("support_grade")
        if not isinstance(leaf_id, str) or grade not in {
            "direct_support",
            "weak_support",
            "no_support",
        }:
            raise RuntimeError(f"invalid gold row: {gold_row}")
        if leaf_id in gold_by_id:
            raise RuntimeError(f"duplicate gold leaf_id: {leaf_id}")
        gold_by_id[leaf_id] = gold_row

    source_rows = source_results.get("results")
    if not isinstance(source_rows, list):
        raise TypeError("source results does not contain a results array")
    seen_keys: set[str] = set()
    total_counts: Counter[str] = Counter()
    supported_counts: Counter[str] = Counter()
    supported_rows: list[dict[str, Any]] = []
    for row in source_rows:
        key = source_key(row)
        if key in seen_keys:
            raise RuntimeError(f"duplicate source instance: {key}")
        seen_keys.add(key)
        leaf_id = row.get("leaf_id")
        gold_row = gold_by_id.get(leaf_id)
        if gold_row is None:
            raise RuntimeError(f"source leaf missing from gold standard: {leaf_id}")
        grade = gold_row["support_grade"]
        total_counts[grade] += 1
        is_supported = row.get("validation", {}).get("derived_supported")
        if not isinstance(is_supported, bool):
            raise TypeError(f"source instance has no valid derived_support decision: {key}")
        if is_supported:
            supported_counts[grade] += 1
            supported_rows.append(row)

    if dict(total_counts) != EXPECTED_SOURCE_TOTAL_COUNTS:
        raise RuntimeError(
            f"source grade totals mismatch: expected={EXPECTED_SOURCE_TOTAL_COUNTS} "
            f"actual={dict(total_counts)}"
        )
    if dict(supported_counts) != EXPECTED_SOURCE_SUPPORTED_COUNTS:
        raise RuntimeError(
            "source supported counts mismatch: "
            f"expected={EXPECTED_SOURCE_SUPPORTED_COUNTS} actual={dict(supported_counts)}"
        )
    if len(supported_rows) != 593:
        raise RuntimeError(f"expected 593 supported source instances, got {len(supported_rows)}")

    supported_rows.sort(key=lambda row: (int(row["run_index"]), str(row["leaf_id"])))
    return source_results, gold_by_id, supported_rows


async def run_one(
    provider: DeepSeekChatProvider,
    semaphore: asyncio.Semaphore,
    source_row: dict[str, Any],
    gold_row: dict[str, Any],
    ordinal: int,
) -> dict[str, Any]:
    """原样重放一个首轮支持实例，开启 thinking，失败时不自动重试。"""

    messages = tuple(
        ChatMessage(role=message["role"], content=message["content"])
        for message in source_row["messages"]
    )
    started_at = datetime.now(UTC).isoformat()
    async with semaphore:
        try:
            completion = await provider.complete(
                messages,
                max_output_tokens=SECOND_PASS_MAX_OUTPUT_TOKENS,
            )
        except ChatProviderError as error:
            return {
                "ordinal": ordinal,
                "source_key": source_key(source_row),
                "leaf_id": source_row["leaf_id"],
                "source_run_index": source_row["run_index"],
                "gold_grade": gold_row["support_grade"],
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "error": f"{type(error).__name__}: {error}",
                "error_code": error.code,
                "retryable": error.retryable,
            }

    validation = parse_and_validate(
        completion.content,
        block_text=parse_block_text(source_row["messages"]),
        expected_supported=gold_row["support_grade"] in SUPPORTED_GRADES,
        required_types=(),
        allowed_extra_types=SUPPORT_TYPES,
        allowed_support_types=SUPPORT_TYPES,
        require_quote_grounding=False,
    )
    return {
        "ordinal": ordinal,
        "source_key": source_key(source_row),
        "leaf_id": source_row["leaf_id"],
        "source_run_index": source_row["run_index"],
        "gold_grade": gold_row["support_grade"],
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "messages": source_row["messages"],
        "raw_content": completion.content,
        "request_id": completion.request_id,
        "actual_model_id": completion.actual_model_id,
        "finish_reason": completion.finish_reason,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
        "validation": validation,
    }


def ratio(numerator: int, denominator: int) -> float | None:
    """返回百分数并保留四位小数；分母为零时返回空值。"""

    return round(numerator * 100 / denominator, 4) if denominator else None


def compute_metrics(final_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """在全部最终结果已知时计算三级金标准指标。"""

    if any(row["final_supported"] is None for row in final_rows):
        return None
    counts = Counter((row["gold_grade"], bool(row["final_supported"])) for row in final_rows)
    direct_supported = counts[("direct_support", True)]
    direct_total = sum(counts[("direct_support", value)] for value in (True, False))
    weak_supported = counts[("weak_support", True)]
    weak_total = sum(counts[("weak_support", value)] for value in (True, False))
    hard_negative_supported = counts[("no_support", True)]
    hard_negative_total = sum(counts[("no_support", value)] for value in (True, False))

    binary_true_positive = direct_supported + weak_supported
    binary_positive_predictions = binary_true_positive + hard_negative_supported
    binary_actual_positive = direct_total + weak_total
    binary_precision = (
        binary_true_positive / binary_positive_predictions if binary_positive_predictions else None
    )
    binary_recall = (
        binary_true_positive / binary_actual_positive if binary_actual_positive else None
    )
    binary_f1 = (
        2 * binary_precision * binary_recall / (binary_precision + binary_recall)
        if binary_precision is not None and binary_recall not in {None, 0.0}
        else None
    )

    hard_positive_predictions = direct_supported + hard_negative_supported
    hard_precision = (
        direct_supported / hard_positive_predictions if hard_positive_predictions else None
    )
    hard_recall = direct_supported / direct_total if direct_total else None
    hard_f1 = (
        2 * hard_precision * hard_recall / (hard_precision + hard_recall)
        if hard_precision is not None and hard_recall not in {None, 0.0}
        else None
    )
    return {
        "direct_support": {
            "supported": direct_supported,
            "total": direct_total,
            "recall_pct": ratio(direct_supported, direct_total),
        },
        "weak_support": {
            "supported": weak_supported,
            "total": weak_total,
            "hit_pct": ratio(weak_supported, weak_total),
        },
        "hard_negative": {
            "supported": hard_negative_supported,
            "total": hard_negative_total,
            "false_support_pct": ratio(hard_negative_supported, hard_negative_total),
        },
        "binary": {
            "true_positive": binary_true_positive,
            "predicted_positive": binary_positive_predictions,
            "actual_positive": binary_actual_positive,
            "precision_pct": ratio(binary_true_positive, binary_positive_predictions),
            "recall_pct": ratio(binary_true_positive, binary_actual_positive),
            "f1_pct": round(binary_f1 * 100, 4) if binary_f1 is not None else None,
        },
        "hard": {
            "precision_pct": ratio(direct_supported, hard_positive_predictions),
            "recall_pct": ratio(direct_supported, direct_total),
            "f1_pct": round(hard_f1 * 100, 4) if hard_f1 is not None else None,
        },
    }


def compute_metric_bounds(final_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """把未知最终结果分别按不支持和支持处理，给出指标上下界。"""

    lower_rows = [
        {
            **row,
            "final_supported": (
                row["final_supported"] if row["final_supported"] is not None else False
            ),
        }
        for row in final_rows
    ]
    upper_rows = [
        {
            **row,
            "final_supported": (
                row["final_supported"] if row["final_supported"] is not None else True
            ),
        }
        for row in final_rows
    ]
    return {
        "lower_unknown_as_unsupported": compute_metrics(lower_rows),
        "upper_unknown_as_supported": compute_metrics(upper_rows),
    }


def build_summary(
    source_results: dict[str, Any],
    gold_by_id: dict[str, dict[str, Any]],
    second_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """合并首轮其余判定和 think 重判结果，形成 2,530 实例最终口径。"""

    second_by_key = {row["source_key"]: row for row in second_rows}
    if len(second_by_key) != len(second_rows):
        raise RuntimeError("duplicate second-pass source_key")

    final_rows: list[dict[str, Any]] = []
    transition_counts: dict[str, Counter[str]] = {
        grade: Counter() for grade in EXPECTED_SOURCE_TOTAL_COUNTS
    }
    for source_row in source_results["results"]:
        key = source_key(source_row)
        grade = gold_by_id[source_row["leaf_id"]]["support_grade"]
        first_supported = source_row["validation"]["derived_supported"]
        if first_supported:
            second_row = second_by_key.get(key)
            if second_row is None:
                raise RuntimeError(f"missing second-pass result: {key}")
            if "validation" in second_row and second_row["validation"].get("valid") is True:
                second_supported = second_row["validation"].get("derived_supported")
                if not isinstance(second_supported, bool):
                    raise RuntimeError(f"invalid second-pass support decision: {key}")
                final_supported: bool | None = second_supported
                transition_counts[grade][
                    "retained_support" if second_supported else "corrected_to_unsupported"
                ] += 1
            else:
                final_supported = None
                transition_counts[grade]["invalid_or_error"] += 1
        else:
            final_supported = False
            transition_counts[grade]["first_pass_unsupported"] += 1
        final_rows.append(
            {
                "source_key": key,
                "leaf_id": source_row["leaf_id"],
                "source_run_index": source_row["run_index"],
                "gold_grade": grade,
                "first_pass_supported": first_supported,
                "final_supported": final_supported,
            }
        )

    first_pass_rows = [
        {**row, "final_supported": bool(row["first_pass_supported"])} for row in final_rows
    ]
    finish_reasons = Counter(
        str(row.get("finish_reason"))
        if "validation" in row
        else f"error:{row.get('error_code', 'unknown')}"
        for row in second_rows
    )
    invalid_count = sum(
        "validation" not in row or row["validation"].get("valid") is not True for row in second_rows
    )
    output_tokens = [int(row.get("output_tokens", 0)) for row in second_rows if "validation" in row]
    return {
        "source_total_instances": len(final_rows),
        "source_supported_instances": len(second_rows),
        "source_unsupported_instances": len(final_rows) - len(second_rows),
        "second_pass_completed": sum("validation" in row for row in second_rows),
        "second_pass_valid": len(second_rows) - invalid_count,
        "second_pass_invalid_or_error": invalid_count,
        "finish_reasons": dict(sorted(finish_reasons.items())),
        "input_tokens": sum(int(row.get("input_tokens", 0)) for row in second_rows),
        "output_tokens": sum(output_tokens),
        "max_output_tokens_used": max(output_tokens, default=0),
        "transitions": {
            grade: dict(sorted(counts.items())) for grade, counts in transition_counts.items()
        },
        "first_pass_metrics": compute_metrics(first_pass_rows),
        "two_stage_metrics": compute_metrics(final_rows),
        "two_stage_metric_bounds": compute_metric_bounds(final_rows),
        "final_rows": final_rows,
    }


def parse_args() -> argparse.Namespace:
    """解析输入、并发和可选恢复目录。"""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-results", type=Path, default=SOURCE_RESULTS_PATH)
    parser.add_argument("--gold", type=Path, default=GOLD_PATH)
    parser.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    parser.add_argument(
        "--resume-run-dir",
        type=Path,
        help="仅补齐该运行目录中缺失的实例；不会重试已有成功或失败项",
    )
    args = parser.parse_args()
    if args.max_concurrency <= 0:
        parser.error("--max-concurrency must be positive")
    return args


def prepare_run_dir(args: argparse.Namespace, manifest: dict[str, Any]) -> Path:
    """创建新的运行目录，或验证并复用显式指定的目录。"""

    if args.resume_run_dir is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        run_dir = OUTPUT_ROOT / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "items").mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return run_dir

    run_dir = args.resume_run_dir.resolve()
    existing_manifest = load_json(run_dir / "manifest.json")
    identity_fields = (
        "source_results_path",
        "source_results_sha256",
        "gold_path",
        "gold_sha256",
        "source_system_prompt_sha256",
        "max_output_tokens",
        "thinking",
    )
    mismatches = {
        field: {"expected": manifest[field], "actual": existing_manifest.get(field)}
        for field in identity_fields
        if manifest[field] != existing_manifest.get(field)
    }
    if mismatches:
        raise RuntimeError(f"resume manifest mismatch: {mismatches}")
    (run_dir / "items").mkdir(exist_ok=True)
    return run_dir


async def main() -> None:
    """执行一次 593 实例二次判定，并保存逐项与汇总产物。"""

    args = parse_args()
    source_results, gold_by_id, supported_rows = load_inputs(
        args.source_results.resolve(),
        args.gold.resolve(),
    )
    manifest = {
        "experiment": "E-1 first-pass supported occurrence thinking second pass",
        "created_at": datetime.now(UTC).isoformat(),
        "source_results_path": str(args.source_results.resolve()),
        "source_results_sha256": sha256_file(args.source_results.resolve()),
        "gold_path": str(args.gold.resolve()),
        "gold_sha256": sha256_file(args.gold.resolve()),
        "source_run_id": source_results["run_id"],
        "source_system_prompt_sha256": source_results["system_prompt_sha256"],
        "source_total_instances": len(source_results["results"]),
        "source_supported_instances": len(supported_rows),
        "model": MODEL_ID,
        "temperature": 0.0,
        "thinking": "enabled",
        "quote_grounding_enabled": False,
        "max_output_tokens": SECOND_PASS_MAX_OUTPUT_TOKENS,
        "max_concurrency": args.max_concurrency,
    }
    run_dir = prepare_run_dir(args, manifest)
    items_dir = run_dir / "items"
    existing_rows = [
        load_json(path) for path in sorted(items_dir.glob("*.json"), key=lambda path: path.name)
    ]
    existing_by_key = {row["source_key"]: row for row in existing_rows}
    expected_keys = {source_key(row) for row in supported_rows}
    extra_keys = sorted(set(existing_by_key) - expected_keys)
    if extra_keys:
        raise RuntimeError(f"unexpected resume items: {extra_keys}")

    ordinal_by_key = {
        source_key(row): ordinal for ordinal, row in enumerate(supported_rows, start=1)
    }
    missing_rows = [row for row in supported_rows if source_key(row) not in existing_by_key]
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "expected_calls": len(supported_rows),
                "already_present": len(existing_rows),
                "remaining_calls": len(missing_rows),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if missing_rows:
        api_key = WindowsCredentialStore().get(CHAT_API_KEY_USERNAME)
        if not api_key:
            raise RuntimeError("chat API key is not configured in Windows Credential Manager")
        provider = DeepSeekChatProvider(
            api_key,
            profile=ChatProviderProfile(
                provider="deepseek",
                model_id=MODEL_ID,
                base_url=DEEPSEEK_BASE_URL,
                temperature=0.0,
                max_output_tokens=SECOND_PASS_MAX_OUTPUT_TOKENS,
                thinking_enabled=True,
            ),
        )
        semaphore = asyncio.Semaphore(args.max_concurrency)
        tasks = [
            asyncio.create_task(
                run_one(
                    provider,
                    semaphore,
                    source_row,
                    gold_by_id[source_row["leaf_id"]],
                    ordinal_by_key[source_key(source_row)],
                )
            )
            for source_row in missing_rows
        ]
        for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
            row = await task
            item_path = (
                items_dir
                / f"{row['ordinal']:04d}_{row['leaf_id']}_{row['source_run_index']:02d}.json"
            )
            item_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
            existing_by_key[row["source_key"]] = row
            if completed % 10 == 0 or completed == len(tasks):
                print(
                    json.dumps(
                        {
                            "newly_completed": completed,
                            "total_present": len(existing_by_key),
                            "expected": len(supported_rows),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    second_rows = [existing_by_key[key] for key in sorted(expected_keys)]
    summary = build_summary(source_results, gold_by_id, second_rows)
    artifact = {
        "manifest": load_json(run_dir / "manifest.json"),
        "run_dir": str(run_dir),
        "summary": summary,
        "results": sorted(second_rows, key=lambda row: row["ordinal"]),
    }
    result_path = run_dir / "results.json"
    result_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "result_path": str(result_path),
                "summary": {key: value for key, value in summary.items() if key != "final_rows"},
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
