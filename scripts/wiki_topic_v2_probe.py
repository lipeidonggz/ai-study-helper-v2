"""Build a Profile-driven knowledge map and run one topic evidence search."""

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
from app.wiki.profile_knowledge_map import (  # noqa: E402
    ProfileKnowledgeMapError,
    ProfileKnowledgeMapService,
    SearchWikiTopicV2Service,
)


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Build or load a map, then return one bounded evidence result."""

    credentials = WindowsCredentialStore()
    chat_key = credentials.get(CHAT_API_KEY_USERNAME)
    embedding_key = credentials.get(EMBEDDING_API_KEY_USERNAME)
    if not chat_key or not embedding_key:
        raise RuntimeError("chat and embedding API keys must be configured")
    chat_provider = DeepSeekChatProvider(chat_key)
    embedding_provider = DashScopeEmbeddingProvider(embedding_key)
    map_service = ProfileKnowledgeMapService(args.data_root)
    if args.build_run_id:
        built = await map_service.build(
            preview_run_ids=args.build_run_id,
            provider=chat_provider,
            embedding_provider=embedding_provider,
            force_new=args.force_new,
            activate=args.activate,
            confirm_ready_partial=args.confirm_ready_partial,
        )
        map_id = built.manifest.map_id
        map_summary = {
            "map_id": map_id,
            "map_hash": built.manifest.manifest_hash,
            "topics": len(built.manifest.topics),
            "groups": len(built.manifest.groups),
            "reused": built.reused,
        }
    elif args.map_id:
        map_id = args.map_id
        loaded = map_service.load(args.snapshot_id, map_id)
        map_summary = {
            "map_id": map_id,
            "map_hash": loaded.manifest.manifest_hash,
            "topics": len(loaded.manifest.topics),
            "groups": len(loaded.manifest.groups),
        }
    else:
        raise RuntimeError("provide --build-run-id or --map-id")
    execution = await SearchWikiTopicV2Service(args.data_root).search(
        snapshot_id=args.snapshot_id,
        map_id=map_id,
        topic=args.topic,
        embedding_provider=embedding_provider,
        provider=chat_provider,
        forced_group_ids=args.force_group_id or None,
        enable_group_coverage_audit=args.enable_group_coverage_audit,
    )
    return {
        "map": map_summary,
        "search": execution.model_dump(mode="json"),
    }


def main() -> None:
    """Parse command line arguments and print the experiment result."""

    # 实验产物固定以 UTF-8 写出；Windows 控制台默认可能使用 GBK，
    # 遇到零宽字符等内容时不能因为终端编码让已完成的实验以失败状态退出。
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--build-run-id", action="append", default=[])
    parser.add_argument("--map-id")
    parser.add_argument("--force-new", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--confirm-ready-partial", action="store_true")
    parser.add_argument("--topic", required=True)
    parser.add_argument(
        "--force-group-id",
        action="append",
        default=[],
        help="diagnostic only: bypass the LLM router for explicit candidate group IDs",
    )
    parser.add_argument(
        "--enable-group-coverage-audit",
        action="store_true",
        help="diagnostic only: evaluate unselected candidate groups for coverage gaps",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(run(args))
    except ProfileKnowledgeMapError as error:
        payload = {
            "error": {
                "code": error.code,
                "message": error.message,
                "diagnostics": error.diagnostics,
            }
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        raise SystemExit(1) from error
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
