from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel

from app.indexing.models import IndexBundle, IndexFailureEvidence
from app.indexing.runner import EmbeddingRunFailure
from app.indexing.service import IndexBuildService
from app.ingestion.blocks import build_block_set
from app.ingestion.errors import IngestFailure
from app.ingestion.extract import extract_html_document
from app.ingestion.models import (
    BlockSet,
    BuildIndexRequest,
    DocumentVersionRecord,
    ExtractedDocument,
    IngestDocumentResponse,
    IngestPreviewRequest,
    IngestPreviewResponse,
    PersistIngestRequest,
    PreviewDocument,
    SectionTree,
    SourceArtifact,
)
from app.ingestion.sections import build_section_tree
from app.ingestion.source import (
    ArtifactStore,
    HttpUrlFetcher,
    UrlFetcher,
    acquire_local_source,
    acquire_url_source,
)
from app.providers.embeddings import (
    DashScopeEmbeddingProvider,
    EmbeddingProvider,
    EmbeddingProviderError,
)
from app.storage.credentials import EMBEDDING_API_KEY_USERNAME, WindowsCredentialStore
from app.storage.database import StorageDatabase
from app.storage.document_registry import DocumentRegistry

ModelT = TypeVar("ModelT", bound=BaseModel)
_LOGGER = logging.getLogger(__name__)


class IngestionService:
    """执行 S0-S4 预览，并把预览结果登记为正式文档版本。"""

    def __init__(
        self,
        data_root: Path,
        *,
        url_fetcher: UrlFetcher | None = None,
        embedding_provider_factory: Callable[[], EmbeddingProvider] | None = None,
    ) -> None:
        self._data_root = data_root.resolve()
        self._store = ArtifactStore(data_root)
        self._url_fetcher = url_fetcher or HttpUrlFetcher()
        self._database = StorageDatabase(data_root)
        self._registry = DocumentRegistry(self._database)
        self._index_build_service = IndexBuildService(self._data_root)
        self._embedding_provider_factory = embedding_provider_factory or _default_embedding_provider

    async def preview(self, request: IngestPreviewRequest) -> IngestPreviewResponse:
        """获取并解析一个来源，在独立 job 中生成预览使用的不变 artifact。"""

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

        try:
            extracted = extract_html_document(source, acquired.decoded_html)
        except IngestFailure:
            raise
        except Exception as error:
            # 提取器缺陷必须留下可诊断日志，但对用户暴露稳定错误码，不能把 Python 异常当业务结果。
            _LOGGER.exception("unexpected HTML extraction failure for job %s", job_id)
            raise IngestFailure(
                "extractor_internal_error",
                "HTML extraction failed because of an unexpected internal error",
                500,
            ) from error
        self._store.write_json(
            job_id,
            "extracted.json",
            extracted.model_dump_json(indent=2),
        )

        preview_doc_id = (
            "preview_" + hashlib.sha256(source.source_key.encode("utf-8")).hexdigest()[:16]
        )
        section_tree = build_section_tree(
            extracted,
            doc_id=preview_doc_id,
            doc_version=0,
        )
        self._store.write_json(
            job_id,
            "section-tree.json",
            section_tree.model_dump_json(indent=2),
        )

        block_set = build_block_set(
            doc_id=preview_doc_id,
            doc_version=0,
            extracted=extracted,
            section_tree=section_tree,
            use_preview_block_ids=True,
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

    def preview_source(self, job_id: str) -> SourceArtifact:
        """读取已完成预览的来源，供后台任务在不重新抓取的情况下继续入库。"""

        preview = IngestPreviewResponse.model_validate_json(
            self._store.read_text(job_id, "preview.json")
        )
        if preview.job_id != job_id:
            raise IngestFailure(
                "preview_job_invalid",
                "preview artifact job identity does not match the requested job",
                409,
            )
        return preview.source

    def persist(self, request: PersistIngestRequest) -> IngestDocumentResponse:
        """复用预览 artifact 登记文档版本，并在一致性校验后保存正式派生物。"""

        source = SourceArtifact.model_validate_json(
            self._store.read_text(request.job_id, "source.json")
        )
        extracted = ExtractedDocument.model_validate_json(
            self._store.read_text(request.job_id, "extracted.json")
        )
        title = extracted.title_candidate or "Untitled"
        source_label = _source_label(
            source.source_kind,
            source.final_url,
            extracted.site_name_candidate,
        )
        aliases = _build_aliases(source.source_key, title, source_label)
        self._registry.start_job(request.job_id, source.source_kind, source.source_uri)

        try:
            preview_section_tree = SectionTree.model_validate_json(
                self._store.read_text(request.job_id, "section-tree.json")
            )
            preview_block_set = BlockSet.model_validate_json(
                self._store.read_text(request.job_id, "blocks.json")
            )
            registration = self._registry.register(
                source=source,
                extracted=extracted,
                extracted_document_ref=self._artifact_ref(
                    request.job_id,
                    "extracted.json",
                ),
                title=title,
                source_label=source_label,
                aliases=aliases,
            )
            self._registry.advance_job(request.job_id, "S4")
            section_tree = build_section_tree(
                extracted,
                doc_id=registration.document.doc_id,
                doc_version=registration.version.doc_version,
            )
            block_set = build_block_set(
                doc_id=registration.document.doc_id,
                doc_version=registration.version.doc_version,
                extracted=extracted,
                section_tree=section_tree,
            )
            _assert_preview_consistency(
                preview_section_tree=preview_section_tree,
                preview_block_set=preview_block_set,
                section_tree=section_tree,
                block_set=block_set,
            )
            self._validate_existing_artifacts(
                version=registration.version,
                expected_section_tree=section_tree,
                expected_block_set=block_set,
            )
            section_tree_ref = self._store.write_json(
                request.job_id,
                "document-section-tree.json",
                section_tree.model_dump_json(indent=2),
            )
            block_set_ref = self._store.write_json(
                request.job_id,
                "document-blocks.json",
                block_set.model_dump_json(indent=2),
            )
            version, derived_artifacts_repaired = self._registry.attach_derived_artifacts(
                doc_id=registration.document.doc_id,
                doc_version=registration.version.doc_version,
                section_tree_ref=section_tree_ref,
                block_set_ref=block_set_ref,
            )
            show_repair_warning = derived_artifacts_repaired and not registration.is_new_version
            repair_warnings: list[str] = (
                ["derived_artifacts_repaired"] if show_repair_warning else []
            )
            warnings = list(
                dict.fromkeys(
                    [
                        *source.warnings,
                        *extracted.warnings,
                        *section_tree.warnings,
                        *block_set.warnings,
                        *repair_warnings,
                    ]
                )
            )
            response = IngestDocumentResponse(
                job_id=request.job_id,
                source=source,
                document=registration.document,
                version=version,
                extracted=extracted,
                section_tree=section_tree,
                block_set=block_set,
                warnings=warnings,
                is_new_document=registration.is_new_document,
                is_new_version=registration.is_new_version,
            )
            self._store.write_json(
                request.job_id,
                "persisted-document.json",
                response.model_dump_json(indent=2),
            )
            self._registry.complete_job(
                request.job_id,
                registration.document.doc_id,
                registration.version.doc_version,
            )
            return response
        except IngestFailure as error:
            self._registry.fail_job(request.job_id, error.code)
            raise
        except Exception as error:
            self._registry.fail_job(request.job_id, "document_persistence_failed")
            raise IngestFailure(
                "document_persistence_failed",
                f"could not persist the document: {error}",
                500,
            ) from error

    async def build_index(self, request: BuildIndexRequest) -> IndexBundle:
        """为已保存 job 构建 S5 候选索引，并记录独立的 job 阶段状态。"""

        self._registry.start_index_job(request.job_id)
        try:
            persisted = IngestDocumentResponse.model_validate_json(
                self._store.read_text(request.job_id, "persisted-document.json")
            )
            provider = self._embedding_provider_factory()
            bundle = await self._index_build_service.build_candidate(
                job_id=request.job_id,
                document_title=persisted.document.title,
                section_tree=persisted.section_tree,
                block_set=persisted.block_set,
                provider=provider,
            )
            self._registry.complete_index_job(request.job_id)
            return bundle
        except EmbeddingRunFailure as error:
            doc_id, doc_version = self._persisted_identity(request.job_id)
            self._store.write_json(
                request.job_id,
                "index-failure.json",
                IndexFailureEvidence(
                    status="failed",
                    job_id=request.job_id,
                    doc_id=doc_id,
                    doc_version=doc_version,
                    stage="S5",
                    code=error.code,
                    message=error.message,
                    retryable=error.retryable,
                    usage_preview=error.usage_preview,
                    usage=error.usage,
                    created_at=datetime.now(UTC).isoformat(),
                ).model_dump_json(indent=2),
            )
            self._registry.fail_job(request.job_id, error.code)
            raise IngestFailure(
                error.code,
                error.message,
                error.http_status,
                retryable=error.retryable,
            ) from error
        except EmbeddingProviderError as error:
            self._registry.fail_job(request.job_id, error.code)
            raise IngestFailure(
                error.code,
                error.message,
                502 if error.retryable else 409,
                retryable=error.retryable,
            ) from error
        except IngestFailure as error:
            self._registry.fail_job(request.job_id, error.code)
            raise
        except Exception as error:
            self._registry.fail_job(request.job_id, "candidate_index_failed")
            raise IngestFailure(
                "candidate_index_failed",
                f"could not build the candidate index: {error}",
                500,
            ) from error

    def _artifact_ref(self, job_id: str, relative_name: str) -> str:
        return f"jobs/{job_id}/{relative_name}"

    def _persisted_identity(self, job_id: str) -> tuple[str, int]:
        """从已保存 artifact 读取失败证据需要的文档身份与版本。"""

        persisted = IngestDocumentResponse.model_validate_json(
            self._store.read_text(job_id, "persisted-document.json")
        )
        return persisted.document.doc_id, persisted.version.doc_version

    def _validate_existing_artifacts(
        self,
        *,
        version: DocumentVersionRecord,
        expected_section_tree: SectionTree,
        expected_block_set: BlockSet,
    ) -> None:
        """验证已有版本引用；只允许补齐空引用，不允许覆盖历史派生物。"""

        if version.section_tree_ref is not None:
            existing_tree = self._read_artifact_model(
                version.section_tree_ref,
                SectionTree,
            )
            if (
                existing_tree.doc_id != version.doc_id
                or existing_tree.doc_version != version.doc_version
                or existing_tree.builder != expected_section_tree.builder
                or existing_tree.tree_hash != expected_section_tree.tree_hash
            ):
                self._raise_invalid_version_artifact()

        if version.block_set_ref is not None:
            existing_blocks = self._read_artifact_model(
                version.block_set_ref,
                BlockSet,
            )
            if (
                existing_blocks.doc_id != version.doc_id
                or existing_blocks.doc_version != version.doc_version
                or existing_blocks.chunker != expected_block_set.chunker
                or existing_blocks.chunk_sizing != expected_block_set.chunk_sizing
                or existing_blocks.block_set_hash != expected_block_set.block_set_hash
            ):
                self._raise_invalid_version_artifact()

    def _read_artifact_model(
        self,
        artifact_ref: str,
        model_type: type[ModelT],
    ) -> ModelT:
        """读取并校验 data root 内的 artifact ref，损坏统一映射为稳定错误码。"""

        try:
            return model_type.model_validate_json(
                self._store.read_ref_text(
                    artifact_ref,
                    missing_code="document_version_artifact_invalid",
                )
            )
        except IngestFailure as error:
            if error.code in {
                "document_version_artifact_invalid",
                "artifact_unreadable",
                "invalid_artifact_ref",
            }:
                self._raise_invalid_version_artifact()
            raise
        except ValueError as error:
            raise IngestFailure(
                "document_version_artifact_invalid",
                f"persisted document artifact is invalid: {error}",
                409,
            ) from error

    @staticmethod
    def _raise_invalid_version_artifact() -> None:
        """抛出已有文档版本 artifact 无效的统一失败。"""

        raise IngestFailure(
            "document_version_artifact_invalid",
            "persisted document version references a missing or inconsistent artifact",
            409,
        )


def _assert_preview_consistency(
    *,
    preview_section_tree: SectionTree,
    preview_block_set: BlockSet,
    section_tree: SectionTree,
    block_set: BlockSet,
) -> None:
    """确认正式重建结果与用户刚才看到的预览具有相同语义。"""

    checks = [
        preview_section_tree.builder == section_tree.builder,
        preview_section_tree.tree_hash == section_tree.tree_hash,
        preview_block_set.chunker == block_set.chunker,
        preview_block_set.chunk_sizing == block_set.chunk_sizing,
        preview_block_set.block_set_hash == block_set.block_set_hash,
    ]
    if not all(checks):
        raise IngestFailure(
            "preview_consistency_failed",
            "the persisted structure no longer matches the preview; parse the document again",
            409,
        )


def _source_label(
    source_kind: str,
    final_url: str | None,
    site_name: str | None,
) -> str:
    """把来源类型转换为稳定、用户可读的显示名。"""

    if source_kind == "file":
        return "本地文件"
    if site_name:
        return site_name
    if final_url:
        return urlsplit(final_url).hostname or "网页"
    return "网页"


def _build_aliases(source_key: str, title: str, source_label: str) -> list[str]:
    """只根据来源和标题生成可解释别名，不调用模型扩展同义词。"""

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


def _default_embedding_provider() -> EmbeddingProvider:
    """读取 embedding Key 配置，但允许已有候选在无 Key 时直接复用。"""

    api_key = WindowsCredentialStore().get(EMBEDDING_API_KEY_USERNAME)
    return DashScopeEmbeddingProvider(api_key)
