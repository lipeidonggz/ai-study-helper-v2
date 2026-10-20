from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from app.ingestion.blocks import build_block_set
from app.ingestion.extract import extract_html_document
from app.ingestion.models import (
    IngestPreviewRequest,
    IngestPreviewResponse,
    PreviewDocument,
)
from app.ingestion.sections import build_section_tree
from app.ingestion.source import (
    ArtifactStore,
    HttpUrlFetcher,
    UrlFetcher,
    acquire_local_source,
    acquire_url_source,
)


class IngestionPreviewService:
    """Runs the preview-only S0-S4 pipeline inside one isolated job workspace."""

    def __init__(
        self,
        data_root: Path,
        *,
        url_fetcher: UrlFetcher | None = None,
    ) -> None:
        self._store = ArtifactStore(data_root)
        self._url_fetcher = url_fetcher or HttpUrlFetcher()

    async def preview(self, request: IngestPreviewRequest) -> IngestPreviewResponse:
        if request.source_kind == "file":
            job_id, acquired = await acquire_local_source(
                request.source_value,
                store=self._store,
            )
        else:
            job_id, acquired = await acquire_url_source(
                request.source_value,
                store=self._store,
                fetcher=self._url_fetcher,
            )

        source = acquired.artifact
        self._store.write_json(
            job_id,
            "source.json",
            source.model_dump_json(indent=2),
        )

        extracted = extract_html_document(source, acquired.decoded_html)
        self._store.write_json(
            job_id,
            "extracted.json",
            extracted.model_dump_json(indent=2),
        )

        section_tree = build_section_tree(extracted)
        self._store.write_json(
            job_id,
            "section-tree.json",
            section_tree.model_dump_json(indent=2),
        )

        preview_doc_id = (
            "preview_" + hashlib.sha256(source.source_key.encode("utf-8")).hexdigest()[:16]
        )
        block_set = build_block_set(
            preview_doc_id=preview_doc_id,
            extracted=extracted,
            section_tree=section_tree,
        )
        self._store.write_json(
            job_id,
            "blocks.json",
            block_set.model_dump_json(indent=2),
        )

        source_label = _source_label(
            source.source_kind, source.final_url, extracted.site_name_candidate
        )
        aliases = _build_aliases(source.source_key, extracted.title_candidate, source_label)
        document = PreviewDocument(
            preview_doc_id=preview_doc_id,
            title=extracted.title_candidate or "Untitled",
            source_label=source_label,
            language=extracted.language,
            aliases=aliases,
        )
        warnings = list(
            dict.fromkeys(
                [
                    *source.warnings,
                    *extracted.warnings,
                    *section_tree.warnings,
                    *block_set.warnings,
                ]
            )
        )
        response = IngestPreviewResponse(
            job_id=job_id,
            source=source,
            document=document,
            extracted=extracted,
            section_tree=section_tree,
            block_set=block_set,
            warnings=warnings,
        )
        self._store.write_json(
            job_id,
            "preview.json",
            response.model_dump_json(indent=2),
        )
        return response


def _source_label(
    source_kind: str,
    final_url: str | None,
    site_name: str | None,
) -> str:
    if source_kind == "file":
        return "本地文件"
    if site_name:
        return site_name
    if final_url:
        return urlsplit(final_url).hostname or "网页"
    return "网页"


def _build_aliases(source_key: str, title: str, source_label: str) -> list[str]:
    candidates: list[str] = []
    if source_key.startswith(("http://", "https://")):
        hostname = urlsplit(source_key).hostname
        if hostname:
            candidates.append(hostname)
    else:
        filename = Path(source_key).name
        if filename:
            candidates.append(filename)
    if source_label and title:
        candidates.append(f"{source_label} {title}")
    return list(dict.fromkeys(candidate for candidate in candidates if candidate != title))
