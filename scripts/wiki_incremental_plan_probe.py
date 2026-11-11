"""Plan one incremental Knowledge Map update without mutating the active map."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.embeddings import DashScopeEmbeddingProvider
from app.storage.credentials import (
    EMBEDDING_API_KEY_USERNAME,
    WindowsCredentialStore,
)
from app.wiki.profile_knowledge_map import (
    ProfileKnowledgeMapError,
    ProfileKnowledgeMapService,
)


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Build a read-only incremental plan and return its JSON payload."""

    key = WindowsCredentialStore().get(EMBEDDING_API_KEY_USERNAME)
    if not key:
        raise RuntimeError("embedding API key must be configured")
    plan = await ProfileKnowledgeMapService(args.data_root).plan_incremental_map_update(
        base_map_id=args.base_map_id,
        preview_run_ids=args.preview_run_id,
        embedding_provider=DashScopeEmbeddingProvider(key),
    )
    return plan.model_dump(mode="json")


def main() -> None:
    """Parse arguments and print the immutable plan result."""

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--base-map-id")
    parser.add_argument("--preview-run-id", action="append", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        payload = asyncio.run(run(args))
    except (ProfileKnowledgeMapError, RuntimeError) as error:
        payload = {
            "error": {
                "code": getattr(error, "code", "incremental_plan_failed"),
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
