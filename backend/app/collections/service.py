from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.collections.models import (
    CandidateSnapshotManifest,
    CurrentCollectionResponse,
    PublishCollectionResponse,
    RollbackCollectionRequest,
    SnapshotArtifact,
    SnapshotDocument,
    SnapshotFailure,
    SnapshotTotals,
    SnapshotVersions,
)
from app.collections.registry import CollectionRegistry
from app.indexing.lancedb_index import (
    read_candidate_lancedb_rows,
    write_candidate_lancedb,
)
from app.indexing.models import IndexBundle
from app.ingestion.errors import IngestFailure
from app.ingestion.models import BlockSet, IngestDocumentResponse
from app.ingestion.source import ArtifactStore
from app.storage.database import StorageDatabase

INDEX_SCHEMA = "lancedb-blocks-v1"


@dataclass(slots=True)
class _CandidateDocument:
    """一个成功读取且可进入候选 snapshot 的已保存文档。"""

    job_id: str
    persisted: IngestDocumentResponse
    bundle: IndexBundle
    index_path: Path
    rows: list[dict[str, Any]]
    artifacts: list[SnapshotArtifact]


class CollectionSnapshotService:
    """构建自包含 collection 候选快照并执行 S6 发布。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._store = ArtifactStore(self._data_root)
        self._database = StorageDatabase(self._data_root)
        self._registry = CollectionRegistry(self._database)
        self._collections_root = self._data_root / "collections"

    def publish(self, job_ids: list[str]) -> PublishCollectionResponse:
        """构建快照并尝试发布；成功或部分成功才切换活动指针。"""

        manifest = self.build(job_ids)
        snapshot_path = f"collections/{manifest.snapshot_id}"
        if manifest.status == "failed":
            record = self._registry.record_aborted(
                manifest,
                snapshot_path=snapshot_path,
                reason="snapshot_failed",
            )
            return PublishCollectionResponse(
                manifest=manifest,
                pointer=None,
                publish_record=record,
            )
        self._verify_snapshot(manifest)
        pointer, record = self._registry.publish(
            manifest,
            snapshot_path=snapshot_path,
        )
        return PublishCollectionResponse(
            manifest=manifest,
            pointer=pointer,
            publish_record=record,
        )

    def current(self) -> CurrentCollectionResponse | None:
        """读取活动指针和对应 manifest；没有活动快照时返回 None。"""

        pointer = self._registry.current()
        if pointer is None:
            return None
        manifest_path = self._collections_root / pointer.snapshot_id / "manifest.json"
        try:
            manifest = CandidateSnapshotManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise IngestFailure(
                "collection_unavailable",
                f"active collection manifest is unreadable: {error}",
                503,
            ) from error
        if (
            manifest.manifest_hash != pointer.manifest_hash
            or manifest.snapshot_id != pointer.snapshot_id
        ):
            raise IngestFailure(
                "collection_unavailable",
                "active collection pointer does not match its manifest",
                503,
            )
        return CurrentCollectionResponse(pointer=pointer, manifest=manifest)

    def rollback(
        self,
        request: RollbackCollectionRequest,
    ) -> PublishCollectionResponse:
        """通过新的 CAS generation 切回 pointer 记录的 previous snapshot。"""

        current = self._registry.current()
        if current is None or current.previous_snapshot_id is None:
            raise IngestFailure(
                "rollback_unavailable",
                "there is no previous collection snapshot to restore",
                409,
            )
        target_id = current.previous_snapshot_id
        manifest_path = self._collections_root / target_id / "manifest.json"
        try:
            manifest = CandidateSnapshotManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise IngestFailure(
                "rollback_unavailable",
                f"previous collection snapshot is unreadable: {error}",
                503,
            ) from error
        self._verify_snapshot(manifest)
        pointer, record = self._registry.publish(
            manifest,
            snapshot_path=f"collections/{target_id}",
            expected_generation=request.expected_generation,
            reason="rollback",
        )
        return PublishCollectionResponse(
            manifest=manifest,
            pointer=pointer,
            publish_record=record,
        )

    def _verify_snapshot(self, manifest: CandidateSnapshotManifest) -> None:
        """发布前从最终目录重新验证 manifest、artifact 和 collection 索引。"""

        snapshot_root = self._collections_root / manifest.snapshot_id
        manifest_path = snapshot_root / "manifest.json"
        try:
            persisted_manifest = CandidateSnapshotManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise IngestFailure(
                "snapshot_verification_failed",
                f"snapshot manifest cannot be read: {error}",
                409,
            ) from error
        expected_hash = _manifest_hash(persisted_manifest)
        if (
            persisted_manifest.snapshot_id != manifest.snapshot_id
            or persisted_manifest.manifest_hash != expected_hash
            or persisted_manifest.manifest_hash != manifest.manifest_hash
        ):
            raise IngestFailure(
                "snapshot_verification_failed",
                "snapshot manifest hash or identity does not match",
                409,
            )
        if persisted_manifest.index_ref is None:
            raise IngestFailure(
                "snapshot_verification_failed",
                "publishable snapshot has no collection index reference",
                409,
            )
        if persisted_manifest.versions is None:
            raise IngestFailure(
                "snapshot_verification_failed",
                "publishable snapshot has no version profile",
                409,
            )
        index_path = self._store.ref_path(
            persisted_manifest.index_ref,
            must_be_file=False,
        )
        expected_blocks = {
            block_id: content_hash
            for document in persisted_manifest.documents
            for block_id, content_hash in _expected_blocks_for_document(
                snapshot_root,
                document,
            ).items()
        }
        if not expected_blocks:
            raise IngestFailure(
                "snapshot_verification_failed",
                "publishable snapshot contains no blocks",
                409,
            )
        index_rows = read_candidate_lancedb_rows(
            index_path,
            expected_block_ids=set(expected_blocks),
            dimension=persisted_manifest.versions.embedding_profile.dimension,
        )
        if len(index_rows) != persisted_manifest.totals.blocks:
            raise IngestFailure(
                "snapshot_verification_failed",
                "collection index row count does not match manifest totals",
                409,
            )
        actual_blocks = {
            str(row.get("block_id", "")): str(row.get("content_hash", "")) for row in index_rows
        }
        if actual_blocks != expected_blocks:
            raise IngestFailure(
                "snapshot_verification_failed",
                "collection index block hashes do not match copied block sets",
                409,
            )
        for document in persisted_manifest.documents:
            for artifact in document.artifact_refs:
                artifact_path = snapshot_root / artifact.snapshot_ref
                if not artifact_path.is_file():
                    raise IngestFailure(
                        "snapshot_verification_failed",
                        f"snapshot artifact is missing: {artifact.snapshot_ref}",
                        409,
                    )
                content = artifact_path.read_bytes()
                if (
                    len(content) != artifact.byte_length
                    or hashlib.sha256(content).hexdigest() != artifact.sha256
                ):
                    raise IngestFailure(
                        "snapshot_verification_failed",
                        f"snapshot artifact checksum mismatch: {artifact.snapshot_ref}",
                        409,
                    )

    def build(self, job_ids: list[str]) -> CandidateSnapshotManifest:
        """从已有 S5 候选索引构建不可变 S6 快照。"""

        if not job_ids:
            raise IngestFailure(
                "snapshot_no_documents",
                "at least one job_id is required to build a collection snapshot",
                422,
            )
        self._collections_root.mkdir(parents=True, exist_ok=True)
        snapshot_id = f"s_{os.urandom(8).hex()}"
        staging_directory = Path(tempfile.mkdtemp(prefix=".b-", dir=self._collections_root))
        failures: list[SnapshotFailure] = []
        candidates: list[_CandidateDocument] = []
        seen_jobs: set[str] = set()
        seen_documents: set[tuple[str, int]] = set()

        for job_id in job_ids:
            if job_id in seen_jobs:
                continue
            seen_jobs.add(job_id)
            try:
                candidate = self._load_candidate(job_id)
                document_key = (
                    candidate.persisted.document.doc_id,
                    candidate.persisted.version.doc_version,
                )
                if document_key in seen_documents:
                    raise IngestFailure(
                        "snapshot_duplicate_document",
                        "the same document version was supplied more than once",
                        409,
                    )
                seen_documents.add(document_key)
                candidates.append(candidate)
            except Exception as error:
                failures.append(self._failure(job_id, error))

        successful: list[_CandidateDocument] = []
        versions: SnapshotVersions | None = None
        for candidate in candidates:
            try:
                candidate_versions = _versions_for(candidate)
                if versions is None:
                    versions = candidate_versions
                elif versions != candidate_versions:
                    raise IngestFailure(
                        "snapshot_version_mismatch",
                        "successful documents use different extraction, "
                        "chunking, or embedding profiles",
                        409,
                    )
                try:
                    self._copy_document_artifacts(staging_directory, candidate)
                except Exception:
                    shutil.rmtree(
                        staging_directory / "documents" / candidate.persisted.document.doc_id,
                        ignore_errors=True,
                    )
                    raise
                successful.append(candidate)
            except Exception as error:
                failures.append(self._failure(candidate.job_id, error))

        try:
            manifest = self._finalize_staging(
                staging_directory=staging_directory,
                snapshot_id=snapshot_id,
                successful=successful,
                failures=failures,
                versions=versions,
            )
            final_directory = self._collections_root / snapshot_id
            os.replace(staging_directory, final_directory)
        except Exception as error:
            shutil.rmtree(staging_directory, ignore_errors=True)
            if isinstance(error, IngestFailure):
                raise
            raise IngestFailure(
                "snapshot_finalize_failed",
                f"could not finalize collection snapshot: {error}",
                500,
            ) from error
        return manifest

    def _finalize_staging(
        self,
        *,
        staging_directory: Path,
        snapshot_id: str,
        successful: list[_CandidateDocument],
        failures: list[SnapshotFailure],
        versions: SnapshotVersions | None,
    ) -> CandidateSnapshotManifest:
        """在 staging 中写索引、汇总 stats 和 manifest；失败由调用方清理。"""

        all_rows = [row for candidate in successful for row in candidate.rows]
        if successful:
            self._write_collection_index(
                staging_directory / "index" / "lance",
                all_rows,
                dimension=successful[0].bundle.embedding_profile.dimension,
            )

        status = "failed" if not successful else "ready_partial" if failures else "ready"
        completeness = "partial" if failures else "complete"
        documents = [_snapshot_document(candidate) for candidate in successful]
        totals = SnapshotTotals(
            documents=len(documents),
            sections=sum(document.section_count for document in documents),
            blocks=sum(document.block_count for document in documents),
            table_blocks=sum(document.table_block_count for document in documents),
            embedding_requests=sum(
                candidate.bundle.usage.request_count for candidate in successful
            ),
            embedding_input_tokens=sum(
                candidate.bundle.usage.input_tokens for candidate in successful
            ),
        )
        current = self._registry.current()
        manifest = CandidateSnapshotManifest(
            snapshot_id=snapshot_id,
            parent_snapshot_id=current.snapshot_id if current is not None else None,
            status=status,
            completeness=completeness,
            created_at=datetime.now(UTC).isoformat(),
            versions=versions,
            documents=documents,
            failed_documents=failures,
            totals=totals,
            index_ref=f"collections/{snapshot_id}/index/lance" if successful else None,
            keyword_index_ref=(
                f"collections/{snapshot_id}/index/lance#blocks.search_terms" if successful else None
            ),
        )
        manifest = _with_manifest_hash(manifest)
        (staging_directory / "manifest.json").write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return manifest

    def _load_candidate(self, job_id: str) -> _CandidateDocument:
        persisted = IngestDocumentResponse.model_validate_json(
            self._store.read_text(job_id, "persisted-document.json")
        )
        indexes = sorted(
            (self._store.job_dir(job_id) / "idx").glob("*/index.json"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        if not indexes:
            raise IngestFailure(
                "candidate_index_missing",
                "no S5 candidate index exists for this job",
                409,
            )
        index_path = indexes[-1]
        bundle = IndexBundle.model_validate_json(index_path.read_text(encoding="utf-8"))
        if (
            bundle.status != "candidate"
            or bundle.doc_id != persisted.document.doc_id
            or bundle.doc_version != persisted.version.doc_version
            or bundle.block_set_hash != persisted.block_set.block_set_hash
        ):
            raise IngestFailure(
                "candidate_index_invalid",
                "candidate IndexBundle does not match the persisted document version",
                409,
            )
        expected_ids = {block.block_id for block in persisted.block_set.blocks}
        rows = read_candidate_lancedb_rows(
            index_path.parent / "lance",
            expected_block_ids=expected_ids,
            dimension=bundle.embedding_profile.dimension,
        )
        rows_by_id = {str(row.get("block_id", "")): row for row in rows}
        if set(rows_by_id) != expected_ids:
            raise IngestFailure(
                "index_integrity_failed",
                "candidate rows do not match the persisted block set",
                500,
            )
        block_by_id = {block.block_id: block for block in persisted.block_set.blocks}
        bundle_by_id = {record.block_id: record for record in bundle.block_indexes}
        if len(bundle_by_id) != len(bundle.block_indexes):
            raise IngestFailure(
                "index_integrity_failed",
                "candidate bundle contains duplicate block records",
                500,
            )
        for block_id, block in block_by_id.items():
            row = rows_by_id[block_id]
            index_record = bundle_by_id.get(block_id)
            if (
                index_record is None
                or str(row.get("doc_id", "")) != persisted.document.doc_id
                or int(row.get("doc_version", 0)) != persisted.version.doc_version
                or str(row.get("content_hash", "")) != block.content_hash
                or str(row.get("content_hash", "")) != index_record.content_hash
                or str(row.get("embedding_input_hash", "")) != index_record.embedding_input_hash
            ):
                raise IngestFailure(
                    "index_integrity_failed",
                    f"candidate row metadata does not match block {block_id}",
                    500,
                )
        return _CandidateDocument(
            job_id=job_id,
            persisted=persisted,
            bundle=bundle,
            index_path=index_path,
            rows=rows,
            artifacts=[],
        )

    def _copy_document_artifacts(
        self,
        staging_directory: Path,
        candidate: _CandidateDocument,
    ) -> None:
        persisted = candidate.persisted
        doc_base = (
            Path("documents") / persisted.document.doc_id / f"v{persisted.version.doc_version:04d}"
        )
        artifacts: list[SnapshotArtifact] = []
        artifacts.append(
            _copy_artifact(
                self._store,
                staging_directory,
                persisted.source.raw_artifact_ref,
                doc_base / "source.html",
                "source",
            )
        )
        artifacts.append(
            _copy_artifact(
                self._store,
                staging_directory,
                f"jobs/{candidate.job_id}/source.json",
                doc_base / "source.json",
                "source_metadata",
            )
        )
        artifacts.append(
            _copy_artifact(
                self._store,
                staging_directory,
                persisted.version.extracted_document_ref,
                doc_base / "extracted.json",
                "extracted",
            )
        )
        if persisted.version.section_tree_ref is None or persisted.version.block_set_ref is None:
            raise IngestFailure(
                "snapshot_artifact_missing",
                "persisted document version has no section tree or block set reference",
                409,
            )
        artifacts.append(
            _copy_artifact(
                self._store,
                staging_directory,
                persisted.version.section_tree_ref,
                doc_base / "section-tree.json",
                "section_tree",
            )
        )
        artifacts.append(
            _copy_artifact(
                self._store,
                staging_directory,
                persisted.version.block_set_ref,
                doc_base / "blocks.json",
                "block_set",
            )
        )
        artifacts.append(
            _copy_artifact(
                self._store,
                staging_directory,
                candidate.bundle.artifact_ref,
                doc_base / "index.json",
                "index_bundle",
            )
        )
        artifacts.append(
            _copy_artifact(
                self._store,
                staging_directory,
                f"jobs/{candidate.job_id}/persisted-document.json",
                doc_base / "document.json",
                "document",
            )
        )
        candidate.artifacts = artifacts

    @staticmethod
    def _write_collection_index(
        directory: Path,
        rows: list[dict[str, Any]],
        *,
        dimension: int,
    ) -> None:
        if not rows:
            raise IngestFailure(
                "snapshot_empty_index",
                "cannot write a collection index without rows",
                500,
            )
        block_ids = {str(row.get("block_id", "")) for row in rows}
        if len(block_ids) != len(rows):
            raise IngestFailure(
                "snapshot_duplicate_block",
                "collection index contains duplicate block IDs",
                500,
            )
        write_candidate_lancedb(directory, rows, dimension=dimension)

    @staticmethod
    def _failure(job_id: str, error: Exception) -> SnapshotFailure:
        if isinstance(error, IngestFailure):
            return SnapshotFailure(
                job_id=job_id,
                stage="S6",
                code=error.code,
                message=error.message,
                retryable=error.retryable,
            )
        return SnapshotFailure(
            job_id=job_id,
            stage="S6",
            code="snapshot_document_failed",
            message=f"document snapshot failed: {error}",
        )


def _versions_for(candidate: _CandidateDocument) -> SnapshotVersions:
    persisted = candidate.persisted
    sizing = persisted.block_set.chunk_sizing.get("estimator_version")
    if not isinstance(sizing, str):
        raise IngestFailure(
            "snapshot_version_mismatch",
            "block set does not contain a string sizing estimator version",
            409,
        )
    return SnapshotVersions(
        extractor=persisted.extracted.extractor,
        section_builder=persisted.section_tree.builder,
        chunker=persisted.block_set.chunker,
        sizing_estimator=sizing,
        embedding_profile=candidate.bundle.embedding_profile,
        embedding_input_policy=candidate.bundle.embedding_input_policy,
        keyword_profile=candidate.bundle.keyword_profile,
        index_schema=INDEX_SCHEMA,
    )


def _snapshot_document(candidate: _CandidateDocument) -> SnapshotDocument:
    persisted = candidate.persisted
    return SnapshotDocument(
        doc_id=persisted.document.doc_id,
        doc_version=persisted.version.doc_version,
        title=persisted.document.title,
        source_label=persisted.document.source_label,
        section_count=len(persisted.section_tree.sections),
        block_count=len(persisted.block_set.blocks),
        table_block_count=sum(
            1 for block in persisted.block_set.blocks if block.table_context is not None
        ),
        embedded_block_count=len(candidate.bundle.block_indexes),
        keyword_indexed_block_count=len(candidate.rows),
        artifact_refs=candidate.artifacts,
    )


def _copy_artifact(
    store: ArtifactStore,
    staging_directory: Path,
    source_ref: str,
    target_ref: Path,
    kind: Literal[
        "source",
        "source_metadata",
        "extracted",
        "section_tree",
        "block_set",
        "index_bundle",
        "document",
    ],
) -> SnapshotArtifact:
    source = store.ref_path(source_ref, missing_code="snapshot_artifact_missing")
    target = staging_directory / target_ref
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return _artifact_for_target(staging_directory, kind, target_ref, source_ref)


def _artifact_for_target(
    staging_directory: Path,
    kind: Literal[
        "source",
        "source_metadata",
        "extracted",
        "section_tree",
        "block_set",
        "index_bundle",
        "document",
    ],
    target_ref: Path,
    source_ref: str,
) -> SnapshotArtifact:
    target = staging_directory / target_ref
    content = target.read_bytes()
    return SnapshotArtifact(
        kind=kind,
        snapshot_ref=target_ref.as_posix(),
        source_ref=source_ref,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
    )


def _with_manifest_hash(manifest: CandidateSnapshotManifest) -> CandidateSnapshotManifest:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    return manifest.model_copy(update={"manifest_hash": _hash_payload(payload)})


def _manifest_hash(manifest: CandidateSnapshotManifest) -> str:
    """重新计算 manifest hash，不信任文件中的自报值。"""

    return _hash_payload(manifest.model_dump(mode="json", exclude={"manifest_hash"}))


def _hash_payload(payload: dict[str, object]) -> str:
    """对规范化 JSON 计算 SHA-256。"""

    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return digest


def _expected_blocks_for_document(
    snapshot_root: Path,
    document: SnapshotDocument,
) -> dict[str, str]:
    """从快照内的 block set 读取用于对账的 block ID。"""

    block_artifact = next(
        (artifact for artifact in document.artifact_refs if artifact.kind == "block_set"),
        None,
    )
    if block_artifact is None:
        raise IngestFailure(
            "snapshot_verification_failed",
            f"document {document.doc_id} has no block set artifact",
            409,
        )
    try:
        block_set = BlockSet.model_validate_json(
            (snapshot_root / block_artifact.snapshot_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise IngestFailure(
            "snapshot_verification_failed",
            f"document {document.doc_id} block set is unreadable: {error}",
            409,
        ) from error
    return {block.block_id: block.content_hash for block in block_set.blocks}
