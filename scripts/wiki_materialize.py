"""Materialize selected narrative Wiki pages for the active snapshot build."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.providers.chat import DeepSeekChatProvider  # noqa: E402
from app.storage.credentials import (  # noqa: E402
    CHAT_API_KEY_USERNAME,
    WindowsCredentialStore,
)
from app.wiki.narrative_service import WikiNarrativeService  # noqa: E402


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Generate selected pages and return their identifiers and evidence counts."""

    chat_key = WindowsCredentialStore().get(CHAT_API_KEY_USERNAME)
    if not chat_key:
        raise RuntimeError("chat API key is required")
    result = await WikiNarrativeService(args.data_root).materialize(
        args.snapshot_id,
        provider=DeepSeekChatProvider(chat_key),
        topic_ids=args.topic_ids,
    )
    return {
        "snapshot_id": result.snapshot_id,
        "build_id": result.build_id,
        "pages": [
            {
                "page_id": page.page_id,
                "topic_id": page.topic_id,
                "title": page.title,
                "summary": page.summary,
                "section_count": len(page.sections),
                "paragraph_count": sum(len(section.paragraphs) for section in page.sections),
                "source_block_ids": sorted(
                    {
                        block_id
                        for section in page.sections
                        for paragraph in section.paragraphs
                        for block_id in paragraph.source_block_ids
                    }
                ),
            }
            for page in result.pages
        ],
        "catalog_only_topic_ids": result.catalog_only_topic_ids,
    }


def main() -> None:
    """Parse CLI arguments and print materialized page metadata."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--topic-id", dest="topic_ids", action="append", required=True)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
