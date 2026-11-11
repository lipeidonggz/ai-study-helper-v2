"""Build one incremental Knowledge Map version and optionally query it."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import DeepSeekChatProvider
from app.providers.embeddings import DashScopeEmbeddingProvider
from app.storage.credentials import (
    CHAT_API_KEY_USERNAME,
    EMBEDDING_API_KEY_USERNAME,
    WindowsCredentialStore,
)
from app.wiki.profile_knowledge_map import (
    ProfileKnowledgeMapError,
    ProfileKnowledgeMapService,
    SearchWikiTopicV2Service,
)


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Execute incremental map update and return map plus optional search result."""

    started_at = time.monotonic()

    def report_progress(event: dict[str, object]) -> None:
        elapsed = int(time.monotonic() - started_at)
        minutes, seconds = divmod(elapsed, 60)
        details = " ".join(
            f"{key}={value}" for key, value in event.items() if key not in {"stage", "message"}
        )
        print(
            (
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"+{minutes:02d}:{seconds:02d} "
                f"{event.get('stage')}: {event.get('message')}"
                + (f" | {details}" if details else "")
            ),
            file=sys.stderr,
            flush=True,
        )

    print(
        "开始构建 Profile Knowledge Map；详细阶段进度写入 stderr。",
        file=sys.stderr,
        flush=True,
    )
    credentials = WindowsCredentialStore()
    chat_key = credentials.get(CHAT_API_KEY_USERNAME)
    embedding_key = credentials.get(EMBEDDING_API_KEY_USERNAME)
    if not chat_key or not embedding_key:
        raise RuntimeError("chat and embedding API keys must be configured")
    chat_provider = DeepSeekChatProvider(chat_key)
    embedding_provider = DashScopeEmbeddingProvider(embedding_key)
    result = await ProfileKnowledgeMapService(
        args.data_root,
        progress_callback=report_progress,
    ).build_incremental_map_update(
        preview_run_ids=args.preview_run_id,
        provider=chat_provider,
        embedding_provider=embedding_provider,
        base_map_id=args.base_map_id,
        force_new=args.force_new,
        activate=args.activate,
        confirm_ready_partial=args.confirm_ready_partial,
    )
    payload: dict[str, object] = {
        "map": {
            "map_id": result.manifest.map_id,
            "map_hash": result.manifest.manifest_hash,
            "schema_version": result.manifest.schema_version,
            "base_map_id": result.manifest.base_map_id,
            "compilation_mode": result.manifest.compilation_mode,
            "topics": len(result.manifest.topics),
            "groups": len(result.manifest.groups),
            "reused": result.reused,
        }
    }
    if args.topic:
        execution = await SearchWikiTopicV2Service(args.data_root).search(
            snapshot_id=result.manifest.snapshot_id,
            map_id=result.manifest.map_id,
            topic=args.topic,
            embedding_provider=embedding_provider,
            provider=chat_provider,
        )
        payload["search"] = execution.model_dump(mode="json")
    return payload


def main() -> None:
    """Parse arguments and print the incremental build result."""

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--base-map-id")
    parser.add_argument("--preview-run-id", action="append", required=True)
    parser.add_argument("--force-new", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--confirm-ready-partial", action="store_true")
    parser.add_argument("--topic")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(run(args))
    except (ProfileKnowledgeMapError, RuntimeError) as error:
        payload = {
            "error": {
                "code": getattr(error, "code", "incremental_build_failed"),
                "message": getattr(error, "message", str(error)),
                "diagnostics": getattr(error, "diagnostics", {}),
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
