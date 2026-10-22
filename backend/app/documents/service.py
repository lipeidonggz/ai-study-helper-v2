from __future__ import annotations

from pathlib import Path
from typing import cast

from sqlalchemy import select

from app.collections.service import CollectionSnapshotService
from app.documents.models import (
    DocumentDetailResponse,
    DocumentSourceDetail,
    DocumentTechnicalDetail,
    DocumentVersionDetail,
)
from app.ingestion.errors import IngestFailure
from app.ingestion.models import DocumentLanguage
from app.storage.database import StorageDatabase
from app.storage.models import DocumentVersionRow, utc_iso
from app.tasks.service import BackgroundTaskService


class DocumentDetailService:
    """组合活动快照正文、登记版本和关联入库任务，返回只读文档详情。"""

    def __init__(
        self,
        data_root: Path,
        collections: CollectionSnapshotService,
        tasks: BackgroundTaskService,
    ) -> None:
        self._database = StorageDatabase(data_root)
        self._collections = collections
        self._tasks = tasks

    def get(self, doc_id: str) -> DocumentDetailResponse:
        """读取当前 active snapshot 中的指定文档详情。

        正文和章节只从当前 active snapshot 的 ``document.json`` 读取；登记库中的
        其他版本仅用于版本时间线，不能混入当前正文。文档不在活动快照时返回 404。
        """

        current, persisted = self._collections.active_document(doc_id)
        if current.manifest.versions is None:
            raise IngestFailure(
                "collection_unavailable",
                "active collection has no version profile",
                503,
            )
        with self._database.session() as session:
            version_rows = session.scalars(
                select(DocumentVersionRow)
                .where(DocumentVersionRow.doc_id == doc_id)
                .order_by(DocumentVersionRow.doc_version.desc())
            ).all()
        if not version_rows:
            raise IngestFailure(
                "collection_document_not_found",
                f"document is not registered: {doc_id}",
                404,
            )

        versions = [
            DocumentVersionDetail(
                doc_version=row.doc_version,
                title=row.title,
                language=cast(DocumentLanguage, row.language),
                extractor_name=row.extractor_name,
                extractor_version=row.extractor_version,
                created_at=utc_iso(row.created_at),
                is_active=row.doc_version == persisted.version.doc_version,
            )
            for row in version_rows
        ]
        job_ids = {
            persisted.job_id,
            *(
                job_id
                for row in version_rows
                if (job_id := _job_id_from_ref(row.extracted_document_ref)) is not None
            ),
        }
        activities = self._tasks.list_related(
            job_ids=job_ids,
            source_values={persisted.source.source_uri},
        )
        profile = current.manifest.versions.embedding_profile
        return DocumentDetailResponse(
            snapshot_id=current.pointer.snapshot_id,
            published_at=current.pointer.published_at,
            doc_id=persisted.document.doc_id,
            doc_version=persisted.version.doc_version,
            title=persisted.document.title,
            source_label=persisted.document.source_label,
            language=persisted.document.language,
            source=DocumentSourceDetail(
                source_kind=persisted.source.source_kind,
                source_uri=persisted.source.source_uri,
                final_url=persisted.source.final_url,
                content_hash=persisted.source.content_hash,
                acquisition_mode=persisted.source.acquisition_mode,
                charset=persisted.source.charset,
            ),
            sections=persisted.section_tree.sections,
            blocks=persisted.block_set.blocks,
            versions=versions,
            activities=activities,
            technical=DocumentTechnicalDetail(
                extractor_name=persisted.version.extractor_name,
                extractor_version=persisted.version.extractor_version,
                section_builder_name=persisted.section_tree.builder.name,
                section_builder_version=persisted.section_tree.builder.version,
                chunker_name=persisted.block_set.chunker.name,
                chunker_version=persisted.block_set.chunker.version,
                embedding_model_id=profile.model_id,
                embedding_dimension=profile.dimension,
                extraction_hash=persisted.version.extraction_hash,
            ),
            warnings=persisted.warnings,
        )


def _job_id_from_ref(artifact_ref: str) -> str | None:
    """从 ``jobs/{job_id}/...`` 引用恢复历史任务 ID。"""

    parts = Path(artifact_ref).parts
    if len(parts) >= 2 and parts[0] == "jobs":
        return parts[1]
    return None
