"""按连续块批次执行多块关系判定，并保存批次合并结果。"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import asdict
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
    DEFAULT_SNAPSHOT,
    DocumentBlock,
    DocumentInput,
    _build_prompt,
    _load_document,
    _run_once,
    _write_json,
)


def _split_contiguous(
    blocks: tuple[DocumentBlock, ...], batch_count: int
) -> list[tuple[DocumentBlock, ...]]:
    """把原文块按原顺序切成尽量均衡的连续批次。"""

    if batch_count < 1:
        raise ValueError("batch_count must be positive")
    if batch_count > len(blocks):
        raise ValueError("batch_count cannot exceed block count")
    base_size, larger_batch_count = divmod(len(blocks), batch_count)
    batches: list[tuple[DocumentBlock, ...]] = []
    start = 0
    for batch_index in range(batch_count):
        size = base_size + (1 if batch_index < larger_batch_count else 0)
        end = start + size
        batches.append(blocks[start:end])
        start = end
    return batches


def _batch_document(
    document: DocumentInput,
    blocks: tuple[DocumentBlock, ...],
) -> DocumentInput:
    """构造只包含一个连续原文批次的文档输入。"""

    return DocumentInput(
        label=document.label,
        doc_id=document.doc_id,
        title=document.title,
        blocks=blocks,
    )


def _collect_relations(payload: dict[str, Any], batch_index: int) -> list[dict[str, Any]]:
    """从批次结果中收集带批次定位信息的全部关系证据。"""

    parsed = payload["run"].get("parsed")
    if parsed is None:
        return []
    relations: list[dict[str, Any]] = []
    for block_record in parsed["blocks"]:
        for relation in block_record["relations"]:
            relations.append(
                {
                    "batch_index": batch_index,
                    "block_id": block_record["block_id"],
                    "quote": relation["quote"],
                    "reason": relation["reason"],
                }
            )
    return relations


async def _run_batch(
    client: AsyncOpenAI,
    *,
    relation_card: Any,
    document: DocumentInput,
    blocks: tuple[DocumentBlock, ...],
    batch_count: int,
    batch_index: int,
    model: str,
    max_tokens: int,
    include_theme_independent_candidate_rule: bool,
    output_root: Path,
) -> dict[str, Any]:
    """执行一个连续块批次，并返回落盘后的请求结果。"""

    batch_document = _batch_document(document, blocks)
    batch_dir = output_root / f"split-{batch_count:02d}" / f"batch-{batch_index:02d}"
    batch_dir.mkdir(parents=True, exist_ok=False)
    scope_label = f"{document.label} 第{batch_index}/{batch_count}批"
    prompt = _build_prompt(
        relation_card,
        batch_document,
        input_scope_label=scope_label,
        source_description="当前批次原文",
        include_theme_independent_candidate_rule=include_theme_independent_candidate_rule,
    )
    (batch_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    return await _run_once(
        client,
        relation_card=relation_card,
        document=batch_document,
        model=model,
        max_tokens=max_tokens,
        prompt=prompt,
        run_index=1,
        output_root=batch_dir,
    )


def _summarize_split(
    *,
    batch_count: int,
    batches: list[tuple[DocumentBlock, ...]],
    payloads: list[dict[str, Any]],
    elapsed_seconds: float,
) -> dict[str, Any]:
    """汇总一个批次方案的召回、证据、调用和 token 使用。"""

    present_blocks: list[str] = []
    reviewed_blocks: list[str] = []
    relations: list[dict[str, Any]] = []
    closest_candidate_count = 0
    input_tokens = 0
    output_tokens = 0
    cache_hit_tokens = 0
    cache_miss_tokens = 0
    request_elapsed_seconds = 0.0
    failed_batches: list[int] = []
    batch_summaries: list[dict[str, Any]] = []
    prompt_char_count = 0

    for batch_index, payload in enumerate(payloads, 1):
        run = payload["run"]
        parsed = run.get("parsed")
        prompt_char_count += len(payload["prompt"])
        request_elapsed_seconds += float(run["elapsed_seconds"])
        input_tokens += int(run.get("input_tokens") or 0)
        output_tokens += int(run.get("output_tokens") or 0)
        cache_hit_tokens += int(run.get("cache_hit_tokens") or 0)
        cache_miss_tokens += int(run.get("cache_miss_tokens") or 0)
        if parsed is None:
            failed_batches.append(batch_index)
            batch_summaries.append(
                {
                    "batch_index": batch_index,
                    "block_ids": [block.suffix for block in batches[batch_index - 1]],
                    "response_class": run["response_class"],
                    "validation_error": run["validation_error"],
                }
            )
            continue
        batch_present = [
            block_record["block_id"]
            for block_record in parsed["blocks"]
            if block_record["has_constraint"]
        ]
        present_blocks.extend(batch_present)
        reviewed_blocks.extend(block_record["block_id"] for block_record in parsed["blocks"])
        closest_candidate_count += sum(
            len(block_record["closest_candidates"]) for block_record in parsed["blocks"]
        )
        relations.extend(_collect_relations(payload, batch_index))
        batch_summaries.append(
            {
                "batch_index": batch_index,
                "block_ids": [block.suffix for block in batches[batch_index - 1]],
                "response_class": run["response_class"],
                "present_block_ids": batch_present,
                "evidence_span_count": sum(
                    len(block_record["relations"]) for block_record in parsed["blocks"]
                ),
                "closest_candidate_count": sum(
                    len(block_record["closest_candidates"]) for block_record in parsed["blocks"]
                ),
                "input_tokens": run.get("input_tokens"),
                "output_tokens": run.get("output_tokens"),
                "elapsed_seconds": run["elapsed_seconds"],
            }
        )

    return {
        "batch_count": batch_count,
        "batch_sizes": [len(batch) for batch in batches],
        "call_count": len(payloads),
        "batch_elapsed_seconds": elapsed_seconds,
        "request_elapsed_seconds": request_elapsed_seconds,
        "reviewed_block_count": len(reviewed_blocks),
        "present_block_count": len(present_blocks),
        "present_block_ids": sorted(present_blocks),
        "evidence_span_count": len(relations),
        "relation_spans": relations,
        "closest_candidate_count": closest_candidate_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit_tokens,
        "cache_miss_tokens": cache_miss_tokens,
        "cache_hit_rate": (
            cache_hit_tokens / (cache_hit_tokens + cache_miss_tokens)
            if cache_hit_tokens + cache_miss_tokens
            else None
        ),
        "prompt_char_count": prompt_char_count,
        "failed_batches": failed_batches,
        "batches": batch_summaries,
    }


async def _run_split(
    client: AsyncOpenAI,
    *,
    relation_card: Any,
    document: DocumentInput,
    batch_count: int,
    model: str,
    max_tokens: int,
    include_theme_independent_candidate_rule: bool,
    output_root: Path,
) -> dict[str, Any]:
    """并发执行一个批次方案内的全部连续批次。"""

    batches = _split_contiguous(document.blocks, batch_count)
    started = time.perf_counter()
    payloads = await asyncio.gather(
        *(
            _run_batch(
                client,
                relation_card=relation_card,
                document=document,
                blocks=batch,
                batch_count=batch_count,
                batch_index=batch_index,
                model=model,
                max_tokens=max_tokens,
                include_theme_independent_candidate_rule=(include_theme_independent_candidate_rule),
                output_root=output_root,
            )
            for batch_index, batch in enumerate(batches, 1)
        )
    )
    elapsed_seconds = time.perf_counter() - started
    summary = _summarize_split(
        batch_count=batch_count,
        batches=batches,
        payloads=payloads,
        elapsed_seconds=elapsed_seconds,
    )
    _write_json(output_root / f"split-{batch_count:02d}" / "summary.json", summary)
    return summary


def _parse_args() -> argparse.Namespace:
    """读取批次切分探针参数。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--document", default="D14")
    parser.add_argument("--relation", default="constrain", choices=RELATION_CARDS)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-counts", nargs="+", type=int, default=[2, 4])
    parser.add_argument("--max-tokens", type=int, default=6000)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--theme-independent-candidates", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


async def main() -> None:
    """依次比较多个连续块批次方案，并写入统一结果。"""

    args = _parse_args()
    batch_counts = sorted(set(args.batch_counts))
    if any(count < 1 for count in batch_counts):
        raise SystemExit("batch counts must be positive")

    relation_card = RELATION_CARDS[args.relation]
    document, snapshot = _load_document(args.snapshot, args.document)
    if any(count > len(document.blocks) for count in batch_counts):
        raise SystemExit("batch count cannot exceed document block count")

    api_key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not api_key:
        raise SystemExit("chat api key is missing")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = args.output_dir or (
        BACKEND_ROOT / ".runtime" / f"constrain_batched_document_probe_{stamp}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    experiment = {
        "snapshot": str(args.snapshot.resolve()),
        "snapshot_id": snapshot.snapshot_id,
        "document": args.document,
        "doc_id": document.doc_id,
        "doc_title": document.title,
        "block_count": len(document.blocks),
        "relation_card": asdict(relation_card),
        "model": args.model,
        "temperature": 0.0,
        "thinking": "disabled",
        "max_tokens": args.max_tokens,
        "batch_counts": batch_counts,
        "runs_per_batch": 1,
        "include_theme_independent_candidate_rule": (args.theme_independent_candidates),
    }
    _write_json(output_root / "experiment.json", experiment)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=DEFAULT_BASE_URL,
        timeout=args.timeout_seconds,
        max_retries=2,
    )
    splits: dict[str, Any] = {}
    for batch_count in batch_counts:
        splits[str(batch_count)] = await _run_split(
            client,
            relation_card=relation_card,
            document=document,
            batch_count=batch_count,
            model=args.model,
            max_tokens=args.max_tokens,
            include_theme_independent_candidate_rule=(args.theme_independent_candidates),
            output_root=output_root,
        )

    result = {
        "experiment": experiment,
        "splits": splits,
    }
    _write_json(output_root / "result.json", result)
    print(
        str(
            {
                "output_root": str(output_root),
                "splits": {
                    count: {
                        "batch_sizes": summary["batch_sizes"],
                        "present_block_ids": summary["present_block_ids"],
                        "evidence_span_count": summary["evidence_span_count"],
                        "input_tokens": summary["input_tokens"],
                        "output_tokens": summary["output_tokens"],
                        "elapsed_seconds": summary["batch_elapsed_seconds"],
                        "failed_batches": summary["failed_batches"],
                    }
                    for count, summary in splits.items()
                },
            }
        ),
        flush=True,
    )
    if any(summary["failed_batches"] for summary in splits.values()):
        raise SystemExit("probe completed with failed batches")


if __name__ == "__main__":
    asyncio.run(main())
