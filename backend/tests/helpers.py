from __future__ import annotations

import hashlib
from pathlib import Path

from app.ingestion.models import SourceArtifact

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def source_artifact_for_bytes(name: str, raw: bytes) -> SourceArtifact:
    digest = hashlib.sha256(raw).hexdigest()
    return SourceArtifact(
        source_kind="file",
        source_key=f"file:///{name}",
        source_uri=str(FIXTURES / name),
        media_type="text/html",
        raw_artifact_ref=f"jobs/test/source-{name}",
        content_hash=digest,
        raw_byte_length=len(raw),
        fetched_at="2026-10-20T00:00:00+00:00",
        final_url=None,
        http_status=None,
        acquisition_mode="file",
        charset="utf-8",
    )
