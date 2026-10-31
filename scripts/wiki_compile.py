"""Compile a snapshot-bound LLM Wiki build with configured cloud providers."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import DeepSeekChatProvider  # noqa: E402
from app.providers.embeddings import DashScopeEmbeddingProvider  # noqa: E402
from app.storage.credentials import (  # noqa: E402
    CHAT_API_KEY_USERNAME,
    EMBEDDING_API_KEY_USERNAME,
    WindowsCredentialStore,
)
from app.wiki.compilation_service import WikiCompilationService  # noqa: E402


async def run(args: argparse.Namespace) -> dict[str, object]:
    """执行全量或增量编译并返回稳定摘要。"""

    credentials = WindowsCredentialStore()
    chat_key = credentials.get(CHAT_API_KEY_USERNAME)
    embedding_key = credentials.get(EMBEDDING_API_KEY_USERNAME)
    if not chat_key:
        raise RuntimeError("chat API key is required")
    if not embedding_key:
        raise RuntimeError("embedding API key is required")
    service = WikiCompilationService(args.data_root)
    if args.base_build_id:
        result = await service.compile_incremental(
            args.snapshot_id,
            chat_provider=DeepSeekChatProvider(chat_key),
            embedding_provider=DashScopeEmbeddingProvider(embedding_key),
            base_build_id=args.base_build_id,
            granularity=args.granularity,
            materialize_pages=not args.skip_narrative,
            map_parallel=args.map_parallel,
        )
    else:
        result = await service.compile_full(
            args.snapshot_id,
            chat_provider=DeepSeekChatProvider(chat_key),
            embedding_provider=DashScopeEmbeddingProvider(embedding_key),
            granularity=args.granularity,
            materialize_pages=not args.skip_narrative,
            map_parallel=args.map_parallel,
        )
    return result.model_dump(mode="json")


def main() -> None:
    """解析 CLI 参数并输出编译结果。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--base-build-id")
    parser.add_argument(
        "--granularity",
        choices=("focused", "standard", "exhaustive"),
        default="standard",
    )
    parser.add_argument("--skip-narrative", action="store_true")
    parser.add_argument("--map-parallel", type=int, choices=range(1, 9), default=1)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
