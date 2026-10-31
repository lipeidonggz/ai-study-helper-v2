"""Query the active Wiki build for a fixed collection snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.embeddings import DashScopeEmbeddingProvider  # noqa: E402
from app.storage.credentials import (  # noqa: E402
    EMBEDDING_API_KEY_USERNAME,
    WindowsCredentialStore,
)
from app.wiki.build_service import WikiBuildService  # noqa: E402


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Search the active Wiki build and return a compact audit payload."""

    embedding_key = WindowsCredentialStore().get(EMBEDDING_API_KEY_USERNAME)
    if not embedding_key:
        raise RuntimeError("embedding API key is required")
    result = await WikiBuildService(args.data_root).search_topics(
        args.snapshot_id,
        args.query,
        semantic_tag=args.semantic_tag,
        embedding_provider=DashScopeEmbeddingProvider(embedding_key),
    )
    topics = result.topics[: args.limit] if args.limit else result.topics
    return {
        "snapshot_id": result.snapshot_id,
        "build_id": result.build_id,
        "total": result.total,
        "returned": len(topics),
        "topics": [
            {
                "topic_id": topic.topic_id,
                "title": topic.title,
                "summary": topic.summary,
                "content_level": topic.content_level,
                "source_doc_count": topic.source_doc_count,
                "source_doc_titles": topic.source_doc_titles,
                "evidence_count": topic.evidence_count,
                "coverage_slot_labels": topic.coverage_slot_labels,
                "coverage_slot_count": topic.coverage_slot_count,
                "match_sources": topic.match_sources,
                "dense_score": topic.dense_score,
            }
            for topic in topics
        ],
    }


def main() -> None:
    """Parse CLI arguments and print the Wiki search audit payload."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--semantic-tag")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be zero or positive")
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
