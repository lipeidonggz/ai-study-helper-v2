"""并发执行 15 篇整篇约束抽取，并汇总句子级、块级和整篇级结果。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import keyring
from openai import AsyncOpenAI

SCRIPT_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = SCRIPT_ROOT.parent / "backend"
sys.path.insert(0, str(SCRIPT_ROOT))
sys.path.insert(0, str(BACKEND_ROOT))

from wiki_constrain_block_level_probe import RELATION_CARDS
from wiki_constrain_whole_document_probe import (
    CHAT_API_KEY_USERNAME,
    CREDENTIAL_SERVICE_NAME,
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    DEFAULT_RELATION_ID,
    DEFAULT_SNAPSHOT,
    _build_prompt,
    _load_document,
    _run_once,
    _write_json,
)

DEFAULT_CONCURRENCY = 12
DEFAULT_MAX_TOKENS = 20_000
DEFAULT_SENTENCE_BASELINE = (
    BACKEND_ROOT / ".runtime" / "family_representatives_global_once_20261115_140049"
)
DEFAULT_BLOCK_BASELINE = (
    BACKEND_ROOT / ".runtime" / "constrain_block_level_guarded_full_20261115_162222"
)


def _document_labels(snapshot_path: Path) -> list[str]:
    """按 snapshot 文档顺序生成与既有实验一致的 DNN 标签。"""

    from app.dual_entry_index.snapshot import SnapshotLoader

    snapshot = SnapshotLoader().load(snapshot_path)
    return [f"D{index:02d}" for index in range(1, len(snapshot.documents) + 1)]


async def _run_document(
    *,
    client: AsyncOpenAI,
    semaphore: asyncio.Semaphore,
    snapshot_path: Path,
    document_label: str,
    relation_card: Any,
    model: str,
    max_tokens: int,
    format_retries: int,
    output_root: Path,
) -> dict[str, Any]:
    """在全局并发槽内完成整篇抽取，并对格式校验失败做有限次同提示重跑。"""

    async with semaphore:
        document, _ = _load_document(snapshot_path, document_label)
        prompt = _build_prompt(relation_card, document)
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        document_output = output_root / document.label
        document_output.mkdir(parents=True, exist_ok=False)
        (document_output / "prompt.txt").write_text(prompt, encoding="utf-8")
        _write_json(
            document_output / "experiment.json",
            {
                "document": document.label,
                "doc_id": document.doc_id,
                "doc_title": document.title,
                "block_count": len(document.blocks),
                "document_character_count": sum(len(block.text) for block in document.blocks),
                "relation_card": {
                    "relation_id": relation_card.relation_id,
                    "display_name": relation_card.display_name,
                    "definition": relation_card.definition,
                    "evidence_boundary": relation_card.evidence_boundary,
                },
                "model": model,
                "temperature": 0.0,
                "thinking": "disabled",
                "max_tokens": max_tokens,
                "prompt_sha256": prompt_sha256,
            },
        )
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        run: dict[str, Any] = {}
        for attempt_index in range(1, format_retries + 2):
            payload = await _run_once(
                client,
                relation_card=relation_card,
                document=document,
                model=model,
                max_tokens=max_tokens,
                prompt=prompt,
                run_index=attempt_index,
                output_root=document_output,
            )
            run = payload["run"]
            attempts.append(
                {
                    "attempt": attempt_index,
                    "finish_reason": run.get("finish_reason"),
                    "response_class": run.get("response_class"),
                    "validation_error": run.get("validation_error"),
                    "input_tokens": run.get("input_tokens"),
                    "output_tokens": run.get("output_tokens"),
                    "elapsed_seconds": run.get("elapsed_seconds"),
                }
            )
            if run.get("response_class") in {"present", "absent"}:
                break
        summary = run.get("summary") or {}
        return {
            "doc_label": document.label,
            "doc_id": document.doc_id,
            "doc_title": document.title,
            "block_count": len(document.blocks),
            "document_character_count": sum(len(block.text) for block in document.blocks),
            "prompt_sha256": prompt_sha256,
            "finish_reason": run.get("finish_reason"),
            "response_class": run.get("response_class"),
            "validation_error": run.get("validation_error"),
            "input_tokens": sum(int(attempt["input_tokens"] or 0) for attempt in attempts),
            "output_tokens": sum(int(attempt["output_tokens"] or 0) for attempt in attempts),
            "request_elapsed_seconds": run.get("elapsed_seconds"),
            "wall_elapsed_seconds": time.perf_counter() - started,
            "attempt_count": len(attempts),
            "format_retry_count": len(attempts) - 1,
            "attempts": attempts,
            "reviewed_block_count": summary.get("reviewed_block_count", 0),
            "evidence_span_count": summary.get("evidence_span_count", 0),
            "closest_candidate_count": summary.get("closest_candidate_count", 0),
            "constraint_block_ids": summary.get("reviewed_constraint_block_ids", []),
        }


def _sum_tokens(documents: list[dict[str, Any]], field: str) -> int:
    """对可空 token 字段求和；缺失值不伪装成零成本。"""

    return sum(int(document[field]) for document in documents if document[field] is not None)


def _load_sentence_baseline(path: Path) -> dict[str, Any]:
    """读取句子级逐块输出，汇总每个 block 是否返回过证据句。"""

    documents: list[dict[str, Any]] = []
    for document_dir in sorted(path.glob("D*")):
        block_files = sorted((document_dir / "constrain").glob("b*.json"))
        hit_blocks: list[str] = []
        sentence_count = 0
        input_tokens = 0
        output_tokens = 0
        for block_file in block_files:
            payload = json.loads(block_file.read_text(encoding="utf-8"))
            runs = payload.get("runs", [])
            block_sentences = [sentence for run in runs for sentence in run.get("sentences", [])]
            if block_sentences:
                hit_blocks.append(block_file.stem)
            sentence_count += len(block_sentences)
            input_tokens += sum(int(run.get("input_tokens") or 0) for run in runs)
            output_tokens += sum(int(run.get("output_tokens") or 0) for run in runs)
        documents.append(
            {
                "doc_label": document_dir.name,
                "block_count": len(block_files),
                "hit_block_ids": hit_blocks,
                "hit_block_count": len(hit_blocks),
                "evidence_sentence_count": sentence_count,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )
    return {
        "path": str(path.resolve()),
        "documents": documents,
        "block_count": sum(item["block_count"] for item in documents),
        "hit_block_count": sum(item["hit_block_count"] for item in documents),
        "evidence_sentence_count": sum(item["evidence_sentence_count"] for item in documents),
        "input_tokens": sum(item["input_tokens"] for item in documents),
        "output_tokens": sum(item["output_tokens"] for item in documents),
    }


def _load_block_baseline(path: Path) -> dict[str, Any]:
    """读取块级整库输出，汇总每个 block 的最终 present 判断。"""

    documents: list[dict[str, Any]] = []
    for document_dir in sorted(path.glob("D*")):
        block_files = sorted(document_dir.glob("b*.json"))
        hit_blocks: list[str] = []
        input_tokens = 0
        output_tokens = 0
        errors = 0
        for block_file in block_files:
            payload = json.loads(block_file.read_text(encoding="utf-8"))
            run = payload.get("run", {})
            response_class = run.get("response_class")
            if response_class == "present":
                hit_blocks.append(block_file.stem)
            if response_class in {"error", "call_error"}:
                errors += 1
            input_tokens += int(run.get("input_tokens") or 0)
            output_tokens += int(run.get("output_tokens") or 0)
        documents.append(
            {
                "doc_label": document_dir.name,
                "block_count": len(block_files),
                "hit_block_ids": hit_blocks,
                "hit_block_count": len(hit_blocks),
                "validation_or_call_errors": errors,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )
    return {
        "path": str(path.resolve()),
        "documents": documents,
        "block_count": sum(item["block_count"] for item in documents),
        "hit_block_count": sum(item["hit_block_count"] for item in documents),
        "validation_or_call_errors": sum(item["validation_or_call_errors"] for item in documents),
        "input_tokens": sum(item["input_tokens"] for item in documents),
        "output_tokens": sum(item["output_tokens"] for item in documents),
    }


def _build_comparison(
    *,
    whole_documents: list[dict[str, Any]],
    sentence_baseline: dict[str, Any],
    block_baseline: dict[str, Any],
) -> dict[str, Any]:
    """按 block 标识比较三种判断单位的模型命中集合。"""

    sentence_by_doc = {
        item["doc_label"]: set(item["hit_block_ids"]) for item in sentence_baseline["documents"]
    }
    block_by_doc = {
        item["doc_label"]: set(item["hit_block_ids"]) for item in block_baseline["documents"]
    }
    comparisons: list[dict[str, Any]] = []
    for document in sorted(whole_documents, key=lambda item: item["doc_label"]):
        label = document["doc_label"]
        whole = set(document["constraint_block_ids"])
        sentence = sentence_by_doc.get(label, set())
        block = block_by_doc.get(label, set())
        comparisons.append(
            {
                "doc_label": label,
                "block_count": document["block_count"],
                "sentence_hit_block_count": len(sentence),
                "block_hit_block_count": len(block),
                "whole_hit_block_count": len(whole),
                "all_three_hit_block_ids": sorted(whole & sentence & block),
                "whole_only_vs_sentence_block_ids": sorted(whole - sentence),
                "sentence_only_vs_whole_block_ids": sorted(sentence - whole),
                "whole_only_vs_block_block_ids": sorted(whole - block),
                "block_only_vs_whole_block_ids": sorted(block - whole),
            }
        )
    return {
        "comparison_semantics": (
            "只比较模型返回约束证据的 block 集合；句子级、块级和整篇级定义版本不同，"
            "且没有全库人工精度标注，因此这些数字不是 precision 或正确率。"
        ),
        "sentence_baseline": {
            key: value for key, value in sentence_baseline.items() if key != "documents"
        },
        "block_baseline": {
            key: value for key, value in block_baseline.items() if key != "documents"
        },
        "whole_document": {
            "document_count": len(whole_documents),
            "block_count": sum(item["block_count"] for item in whole_documents),
            "hit_block_count": sum(len(item["constraint_block_ids"]) for item in whole_documents),
            "evidence_span_count": sum(item["evidence_span_count"] for item in whole_documents),
            "closest_candidate_count": sum(
                item["closest_candidate_count"] for item in whole_documents
            ),
            "input_tokens": _sum_tokens(whole_documents, "input_tokens"),
            "output_tokens": _sum_tokens(whole_documents, "output_tokens"),
            "format_or_validation_errors": sum(
                item["response_class"] == "error" for item in whole_documents
            ),
            "call_errors": sum(item["response_class"] == "call_error" for item in whole_documents),
        },
        "documents": comparisons,
    }


def _parse_args() -> argparse.Namespace:
    """读取批量整篇实验参数。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--document",
        action="append",
        default=[],
        help="只运行指定 DNN 文档；可重复传入",
    )
    parser.add_argument("--relation", default=DEFAULT_RELATION_ID, choices=RELATION_CARDS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--format-retries", type=int, default=2)
    parser.add_argument("--timeout-seconds", type=float, default=600.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--sentence-baseline", type=Path, default=DEFAULT_SENTENCE_BASELINE)
    parser.add_argument("--block-baseline", type=Path, default=DEFAULT_BLOCK_BASELINE)
    return parser.parse_args()


async def main() -> None:
    """并发执行所有文档，并落盘整篇结果和三路线对比。"""

    args = _parse_args()
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive")
    if args.max_tokens < 1:
        raise SystemExit("--max-tokens must be positive")
    if args.format_retries < 0:
        raise SystemExit("--format-retries must be non-negative")
    relation_card = RELATION_CARDS[args.relation]
    labels = _document_labels(args.snapshot)
    if args.document:
        unknown_labels = sorted(set(args.document) - set(labels))
        if unknown_labels:
            raise SystemExit(f"unknown document labels: {unknown_labels}")
        selected_labels = set(args.document)
        labels = [label for label in labels if label in selected_labels]

    api_key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not api_key:
        raise SystemExit("chat api key is missing")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = args.output_dir or (
        BACKEND_ROOT / ".runtime" / f"constrain_whole_document_all15_{stamp}"
    )
    output_root.mkdir(parents=True, exist_ok=False)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=args.timeout_seconds,
        max_retries=2,
    )
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()
    documents = await asyncio.gather(
        *(
            _run_document(
                client=client,
                semaphore=semaphore,
                snapshot_path=args.snapshot,
                document_label=label,
                relation_card=relation_card,
                model=args.model,
                max_tokens=args.max_tokens,
                format_retries=args.format_retries,
                output_root=output_root,
            )
            for label in labels
        )
    )
    wall_elapsed_seconds = time.perf_counter() - started
    documents = sorted(documents, key=lambda item: item["doc_label"])

    sentence_baseline = _load_sentence_baseline(args.sentence_baseline)
    block_baseline = _load_block_baseline(args.block_baseline)
    comparison = _build_comparison(
        whole_documents=documents,
        sentence_baseline=sentence_baseline,
        block_baseline=block_baseline,
    )
    response_classes = Counter(item["response_class"] for item in documents)
    result = {
        "output_root": str(output_root),
        "snapshot": str(args.snapshot.resolve()),
        "relation": args.relation,
        "model": args.model,
        "concurrency": args.concurrency,
        "max_tokens": args.max_tokens,
        "format_retries": args.format_retries,
        "temperature": 0.0,
        "thinking": "disabled",
        "document_count": len(documents),
        "block_count": sum(item["block_count"] for item in documents),
        "wall_elapsed_seconds": wall_elapsed_seconds,
        "response_classes": dict(sorted(response_classes.items())),
        "input_tokens": _sum_tokens(documents, "input_tokens"),
        "output_tokens": _sum_tokens(documents, "output_tokens"),
        "format_retried_document_count": sum(item["format_retry_count"] > 0 for item in documents),
        "format_retry_count": sum(item["format_retry_count"] for item in documents),
        "documents": documents,
        "comparison": comparison,
    }
    _write_json(output_root / "result.json", result)
    print(
        json.dumps(
            {
                "output_root": str(output_root),
                "document_count": result["document_count"],
                "block_count": result["block_count"],
                "wall_elapsed_seconds": result["wall_elapsed_seconds"],
                "response_classes": result["response_classes"],
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
                "whole_hit_block_count": comparison["whole_document"]["hit_block_count"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())
