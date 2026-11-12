"""独立运行 14 号设计 5.1 的候选扫描与概念义项编译。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import keyring

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.dual_entry_index.artifacts import (
    load_stage51_artifact,
    publish_scan_result,
    publish_stage51_artifact,
)
from app.dual_entry_index.compiler import Stage51Compiler
from app.dual_entry_index.induction import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL_ID,
    DeepSeekJsonClient,
    LLMTermSenseInducer,
)
from app.dual_entry_index.scanner import CandidateScanner
from app.dual_entry_index.snapshot import (
    SnapshotLoader,
    find_active_snapshot,
    select_snapshot_documents,
)

CREDENTIAL_SERVICE_NAME = "ai-study-helper"
CHAT_API_KEY_USERNAME = "chat-api-key"


def _load_chat_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key is None:
        key = keyring.get_password(CREDENTIAL_SERVICE_NAME, CHAT_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("chat_api_key_missing")
    return key


def _snapshot_path(args: argparse.Namespace) -> Path:
    if args.snapshot is not None:
        return args.snapshot
    if args.data_root is None:
        raise RuntimeError("snapshot_or_data_root_required")
    return find_active_snapshot(args.data_root)


def _progress_printer() -> Callable[[str], None]:
    started = time.monotonic()

    def print_progress(message: str) -> None:
        elapsed = int(time.monotonic() - started)
        minutes, seconds = divmod(elapsed, 60)
        print(f"[+{minutes:02d}:{seconds:02d}] {message}", flush=True)

    return print_progress


async def run(args: argparse.Namespace) -> dict[str, object]:
    """执行 scan-only 或完整 5.1 编译并返回产物位置摘要。"""

    snapshot = SnapshotLoader().load(_snapshot_path(args))
    snapshot = select_snapshot_documents(snapshot, args.doc_id)
    scanner = CandidateScanner()
    progress = _progress_printer()
    progress(f"已加载快照：{snapshot.snapshot_id} blocks={len(snapshot.blocks)}")
    if args.scan_only:
        scan_result = scanner.scan(snapshot.snapshot_id, snapshot.blocks)
        progress(
            "候选扫描完成："
            f"candidates={len(scan_result.occurrences)} "
            f"hash={scan_result.candidate_set_hash[:16]}"
        )
        output_path = publish_scan_result(args.output_dir, scan_result)
        return {
            "mode": "scan_only",
            "snapshot_id": snapshot.snapshot_id,
            "candidate_occurrences": len(scan_result.occurrences),
            "candidate_set_hash": scan_result.candidate_set_hash,
            "output": str(output_path),
        }

    base_artifact = (
        load_stage51_artifact(args.base_map) if args.base_map is not None else None
    )
    client = DeepSeekJsonClient(
        _load_chat_key(),
        model_id=args.model,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )
    inducer = LLMTermSenseInducer(
        client,
        max_input_chars=args.max_input_chars,
        max_output_tokens=args.max_output_tokens,
        max_retries=args.max_retries,
    )
    compiler = Stage51Compiler(
        inducer,
        max_input_chars=args.max_input_chars,
        max_output_tokens=args.max_output_tokens,
    )
    artifact, diagnostics = await compiler.compile(
        snapshot=snapshot,
        scanner=scanner,
        base_artifact=base_artifact,
        concurrency=args.concurrency,
        progress=progress,
    )
    for metric in diagnostics.stage_metrics:
        progress(
            "阶段指标："
            f"stage={metric.stage} wall={metric.wall_seconds:.2f}s "
            f"requests={metric.requests} attempts={metric.attempts} "
            f"failed={metric.failed_attempts} retries={metric.retry_attempts} "
            f"input={metric.input_tokens} output={metric.output_tokens} "
            f"cache_hit={metric.cache_hit_tokens} cache_miss={metric.cache_miss_tokens}"
        )
    output_path = publish_stage51_artifact(
        args.output_dir,
        artifact,
        diagnostics,
        snapshot,
    )
    return {
        "mode": artifact.compilation_mode,
        "map_id": artifact.map_id,
        "map_hash": artifact.map_hash,
        "snapshot_id": snapshot.snapshot_id,
        "terms": len(artifact.terms),
        "concept_senses": len(artifact.concept_senses),
        "term_sense_links": len(artifact.term_sense_links),
        "llm_requests": diagnostics.llm_requests,
        "llm_attempts": diagnostics.llm_attempts,
        "failed_llm_attempts": diagnostics.failed_llm_attempts,
        "retry_attempts": diagnostics.retry_attempts,
        "input_tokens": diagnostics.input_tokens,
        "output_tokens": diagnostics.output_tokens,
        "cache_hit_tokens": diagnostics.cache_hit_tokens,
        "cache_miss_tokens": diagnostics.cache_miss_tokens,
        "actual_model_ids": list(diagnostics.actual_model_ids),
        "stage_metrics": [
            metric.model_dump(mode="json") for metric in diagnostics.stage_metrics
        ],
        "output": str(output_path),
    }


def main() -> None:
    """解析命令行参数并运行独立探针。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--doc-id", action="append", default=[])
    parser.add_argument("--base-map", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--concurrency", type=int, choices=range(1, 17), default=5)
    parser.add_argument("--max-input-chars", type=int, default=60_000)
    parser.add_argument("--max-output-tokens", type=int, default=4_000)
    parser.add_argument("--max-retries", type=int, choices=range(5), default=2)
    parser.add_argument("--timeout-seconds", type=float, default=180.0)
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
