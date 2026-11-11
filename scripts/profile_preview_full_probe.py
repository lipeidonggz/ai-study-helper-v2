"""Run one frozen Profile against every document in a snapshot."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.profile_extraction.models import PreviewRunRequest  # noqa: E402
from app.profile_extraction.preview_service import ProfilePreviewService  # noqa: E402
from app.profile_extraction.profile_service import ProfileService  # noqa: E402
from app.retrieval.service import RetrievalReadService  # noqa: E402
from app.storage.credentials import WindowsCredentialStore  # noqa: E402
from app.storage.database import StorageDatabase  # noqa: E402
from app.storage.profile_extraction_models import ProfilePreviewRunRow  # noqa: E402


async def run(args: argparse.Namespace) -> dict[str, object]:
    """Create, wait for, and summarize one full-corpus Preview Run."""

    data_root = args.data_root.resolve()
    credentials = WindowsCredentialStore()
    database = StorageDatabase(data_root)
    with database.session() as session:
        active_runs = list(
            session.scalars(
                select(ProfilePreviewRunRow.run_id)
                .where(ProfilePreviewRunRow.status.in_(("queued", "running")))
                .order_by(ProfilePreviewRunRow.created_at)
            )
        )
    if active_runs and not args.recover_active:
        raise RuntimeError(
            "Preview Run(s) are already active; use --recover-active only after confirming "
            f"the previous process is gone: {', '.join(active_runs)}"
        )
    profiles = ProfileService(data_root, credentials, database=database)
    previews = ProfilePreviewService(
        data_root,
        profiles,
        credentials,
        database=database,
    )
    reader = RetrievalReadService(data_root).bind_snapshot(args.snapshot_id)
    available_document_ids = {document.doc_id for document in reader.list_documents().documents}
    document_ids = args.document_ids or sorted(available_document_ids)
    unknown_document_ids = sorted(set(document_ids) - available_document_ids)
    if unknown_document_ids:
        raise ValueError(
            f"snapshot does not contain requested documents: {', '.join(unknown_document_ids)}"
        )
    await previews.start()
    try:
        created = await previews.create(
            PreviewRunRequest(
                profile_id=args.profile_id,
                snapshot_id=args.snapshot_id,
                document_ids=document_ids,
                execution_mode="force_new",
                semantic_audit_enabled=args.semantic_audit,
            )
        )
        completed = await previews.wait_for_terminal(created.run_id, timeout_seconds=7_200)
    finally:
        await previews.stop()
    return {
        "run_id": completed.run_id,
        "snapshot_id": completed.snapshot_id,
        "profile_id": completed.profile_id,
        "profile_hash": completed.profile_hash,
        "status": completed.status,
        "error_code": completed.error_code,
        "error_message": completed.error_message,
        "documents": len(completed.document_scope),
        "candidates": completed.candidates_total,
        "document_scope": completed.document_scope,
        "usage": [
            {
                "channel_id": item.get("channel_id"),
                "operation": item.get("operation"),
                "input_tokens": item.get("input_tokens"),
                "output_tokens": item.get("output_tokens"),
            }
            for item in completed.usage
        ],
        "diagnostics": completed.diagnostics,
    }


def main() -> None:
    """Parse CLI arguments and print the completed run summary."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--document-id", action="append", default=[], dest="document_ids")
    parser.add_argument("--semantic-audit", action="store_true")
    parser.add_argument("--recover-active", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = asyncio.run(run(args))
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
