from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from sqlalchemy import select

from app.ingestion.errors import IngestFailure
from app.ingestion.models import (
    DocumentLanguage,
    DocumentRecord,
    DocumentVersionRecord,
    ExtractedDocument,
    SourceArtifact,
)
from app.storage.database import StorageDatabase
from app.storage.models import (
    DocumentRow,
    DocumentVersionRow,
    IngestionJobRow,
    SourceArtifactRow,
    utc_now,
)


@dataclass(slots=True)
class RegistrationResult:
    """一次建档或幂等复用后的文档与版本结果。"""

    document: DocumentRecord
    version: DocumentVersionRecord
    is_new_document: bool
    is_new_version: bool


SourceKind = Literal["file", "url"]


def _encode_strings(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _decode_strings(value: str) -> list[str]:
    decoded = json.loads(value)
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise ValueError("stored string list is invalid")
    return decoded


def _document_record(row: DocumentRow) -> DocumentRecord:
    return DocumentRecord(
        doc_id=row.doc_id,
        source_key=row.source_key,
        source_kind=cast(SourceKind, row.source_kind),
        current_doc_version=row.current_doc_version,
        title=row.title,
        source_label=row.source_label,
        aliases=_decode_strings(row.aliases_json),
        language=cast(DocumentLanguage, row.language),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _version_record(row: DocumentVersionRow) -> DocumentVersionRecord:
    return DocumentVersionRecord(
        doc_id=row.doc_id,
        doc_version=row.doc_version,
        extraction_hash=row.extraction_hash,
        source_artifact_ref=row.source_artifact_ref,
        extracted_document_ref=row.extracted_document_ref,
        section_tree_ref=row.section_tree_ref,
        block_set_ref=row.block_set_ref,
        title=row.title,
        source_label=row.source_label,
        aliases=_decode_strings(row.aliases_json),
        language=cast(DocumentLanguage, row.language),
        extractor_name=row.extractor_name,
        extractor_version=row.extractor_version,
        created_at=row.created_at.isoformat(),
    )


class DocumentRegistry:
    """登记稳定文档身份和不可覆盖的内容版本。"""

    def __init__(self, database: StorageDatabase) -> None:
        self._database = database

    def start_job(self, job_id: str, source_kind: str, source_value: str) -> None:
        """创建或重置一个保存 job，并把它置于 S2 运行状态。"""

        with self._database.session() as session, session.begin():
            row = session.get(IngestionJobRow, job_id)
            if row is None:
                row = IngestionJobRow(
                    job_id=job_id,
                    source_kind=source_kind,
                    source_value=source_value,
                    status="running",
                    stage="S2",
                )
                session.add(row)
            else:
                row.status = "running"
                row.stage = "S2"
                row.error_code = None

    def register(
        self,
        *,
        source: SourceArtifact,
        extracted: ExtractedDocument,
        extracted_document_ref: str,
        title: str,
        source_label: str,
        aliases: list[str],
    ) -> RegistrationResult:
        """按 source key 复用文档、按 extraction hash 幂等登记版本。

        相同来源与最新提取内容相同时不新增版本；内容变化时保持 ``doc_id``
        并追加递增 ``doc_version``。身份查询、版本写入和 artifact 关联在
        单机 SQLite 事务中完成。
        """

        now = utc_now()
        with self._database.session() as session, session.begin():
            source_row = session.get(SourceArtifactRow, source.raw_artifact_ref)
            if source_row is None:
                source_row = SourceArtifactRow(
                    artifact_ref=source.raw_artifact_ref,
                    source_kind=source.source_kind,
                    source_key=source.source_key,
                    source_uri=source.source_uri,
                    media_type=source.media_type,
                    content_hash=source.content_hash,
                    raw_byte_length=source.raw_byte_length,
                    fetched_at=source.fetched_at,
                    final_url=source.final_url,
                    http_status=source.http_status,
                    acquisition_mode=source.acquisition_mode,
                    charset=source.charset,
                    warnings_json=_encode_strings(source.warnings),
                    created_at=now,
                )
                session.add(source_row)

            document_row = session.scalar(
                select(DocumentRow).where(DocumentRow.source_key == source.source_key)
            )
            is_new_document = document_row is None
            if document_row is None:
                document_row = DocumentRow(
                    doc_id=f"doc_{uuid4().hex}",
                    source_key=source.source_key,
                    source_kind=source.source_kind,
                    current_doc_version=0,
                    title=title,
                    source_label=source_label,
                    aliases_json=_encode_strings(aliases),
                    language=extracted.language,
                    created_at=now,
                    updated_at=now,
                )
                session.add(document_row)
                session.flush()

            latest_version = session.scalar(
                select(DocumentVersionRow)
                .where(DocumentVersionRow.doc_id == document_row.doc_id)
                .order_by(DocumentVersionRow.doc_version.desc())
                .limit(1)
            )
            if (
                latest_version is not None
                and latest_version.extraction_hash == extracted.extraction_hash
            ):
                return RegistrationResult(
                    document=_document_record(document_row),
                    version=_version_record(latest_version),
                    is_new_document=False,
                    is_new_version=False,
                )

            version_number = (latest_version.doc_version if latest_version else 0) + 1
            version_row = DocumentVersionRow(
                doc_id=document_row.doc_id,
                doc_version=version_number,
                extraction_hash=extracted.extraction_hash,
                source_artifact_ref=source.raw_artifact_ref,
                extracted_document_ref=extracted_document_ref,
                title=title,
                source_label=source_label,
                aliases_json=_encode_strings(aliases),
                language=extracted.language,
                extractor_name=extracted.extractor.name,
                extractor_version=extracted.extractor.version,
                created_at=now,
            )
            session.add(version_row)
            document_row.current_doc_version = version_number
            document_row.title = title
            document_row.source_label = source_label
            document_row.aliases_json = _encode_strings(aliases)
            document_row.language = extracted.language
            document_row.updated_at = now
            session.flush()
            return RegistrationResult(
                document=_document_record(document_row),
                version=_version_record(version_row),
                is_new_document=is_new_document,
                is_new_version=True,
            )

    def attach_derived_artifacts(
        self,
        *,
        doc_id: str,
        doc_version: int,
        section_tree_ref: str,
        block_set_ref: str,
    ) -> tuple[DocumentVersionRecord, bool]:
        """只补齐为空的派生 ref，并返回本次是否发生恢复写入。"""

        with self._database.session() as session, session.begin():
            row = session.get(DocumentVersionRow, (doc_id, doc_version))
            if row is None:
                raise IngestFailure(
                    "document_version_missing",
                    "persisted document version disappeared before artifact attachment",
                    500,
                )
            repaired = False
            if row.section_tree_ref is None:
                row.section_tree_ref = section_tree_ref
                repaired = True
            if row.block_set_ref is None:
                row.block_set_ref = block_set_ref
                repaired = True
            session.flush()
            return _version_record(row), repaired

    def advance_job(self, job_id: str, stage: str) -> None:
        """推进 job 的阶段，但不清除已有错误或其他状态字段。"""

        with self._database.session() as session, session.begin():
            row = session.get(IngestionJobRow, job_id)
            if row is None:
                raise IngestFailure(
                    "job_missing",
                    "ingestion job disappeared before stage advancement",
                    500,
                )
            row.stage = stage

    def complete_job(self, job_id: str, doc_id: str, doc_version: int) -> None:
        """把 job 标记为成功，并关联最终文档版本。"""

        with self._database.session() as session, session.begin():
            row = session.get(IngestionJobRow, job_id)
            if row is None:
                raise IngestFailure(
                    "job_missing",
                    "ingestion job disappeared before completion",
                    500,
                )
            row.status = "succeeded"
            row.stage = "S4"
            row.doc_id = doc_id
            row.doc_version = doc_version
            row.error_code = None

    def fail_job(self, job_id: str, error_code: str) -> None:
        """把 job 标记为失败，同时保留最后已进入的阶段。"""

        with self._database.session() as session, session.begin():
            row = session.get(IngestionJobRow, job_id)
            if row is None:
                return
            row.status = "failed"
            row.error_code = error_code
