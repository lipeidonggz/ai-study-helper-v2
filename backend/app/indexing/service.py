from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from app.indexing.embedding_inputs import (
    EMBEDDING_INPUT_POLICY,
    build_embedding_inputs,
)
from app.indexing.keywords import (
    KEYWORD_PROFILE,
    build_keyword_search_text,
    build_keyword_terms,
)
from app.indexing.lancedb_index import (
    BLOCKS_TABLE_NAME,
    FTS_FIELD_NAME,
    validate_candidate_lancedb,
    write_candidate_lancedb,
)
from app.indexing.models import BlockIndexRecord, IndexBundle
from app.indexing.runner import EmbeddingRunner
from app.ingestion.errors import IngestFailure
from app.ingestion.models import BlockSet, SectionTree
from app.ingestion.source import ArtifactStore
from app.providers.embeddings import EmbeddingProvider


class IndexBuildService:
    """在 job workspace 构建并校验不可发布的 S5 候选索引。"""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.resolve()
        self._store = ArtifactStore(self._data_root)

    async def build_candidate(
        self,
        *,
        job_id: str,
        document_title: str,
        section_tree: SectionTree,
        block_set: BlockSet,
        provider: EmbeddingProvider,
    ) -> IndexBundle:
        """生成候选 bundle；相同身份和历史候选完整时直接复用。"""

        self._validate_inputs(section_tree, block_set)
        policy, inputs = build_embedding_inputs(document_title, block_set.blocks)
        index_id = self._index_id(
            block_set=block_set,
            provider=provider,
        )
        job_directory = self._store.job_dir(job_id)
        indexes_directory = job_directory / "idx"
        final_directory = indexes_directory / index_id
        artifact_ref = (Path("jobs") / job_id / "idx" / index_id / "index.json").as_posix()
        lancedb_ref = (Path("jobs") / job_id / "idx" / index_id / "lance").as_posix()
        expected_block_ids = {block.block_id for block in block_set.blocks}

        existing = final_directory / "index.json"
        if existing.is_file():
            bundle = self._read_existing_bundle(
                existing,
                expected_index_id=index_id,
                expected_block_ids=expected_block_ids,
                dimension=provider.profile.dimension,
            )
            return bundle
        if final_directory.exists():
            raise IngestFailure(
                "candidate_index_invalid",
                "candidate directory exists without a readable index bundle",
                409,
            )

        indexes_directory.mkdir(parents=True, exist_ok=True)
        run_result = await EmbeddingRunner(provider).embed(inputs)
        rows = []
        block_indexes: list[BlockIndexRecord] = []
        for block, embedding_input in zip(block_set.blocks, inputs, strict=True):
            vector = run_result.vectors_by_block_id[block.block_id]
            search_text = build_keyword_search_text(document_title, block)
            search_terms = build_keyword_terms(search_text)
            if not search_terms:
                raise IngestFailure(
                    "keyword_terms_empty",
                    f"block {block.block_id} produced no keyword terms",
                    422,
                )
            rows.append(
                {
                    "block_id": block.block_id,
                    "doc_id": block_set.doc_id,
                    "doc_version": block_set.doc_version,
                    "block_index": block.block_index,
                    "content_hash": block.content_hash,
                    "embedding_input_hash": embedding_input.input_hash,
                    "search_text": search_text,
                    "search_terms": search_terms,
                    "vector_dimension": provider.profile.dimension,
                    "dense_and_keyword_present": True,
                    "vector": vector,
                }
            )
            block_indexes.append(
                BlockIndexRecord(
                    block_id=block.block_id,
                    block_index=block.block_index,
                    content_hash=block.content_hash,
                    embedding_input_hash=embedding_input.input_hash,
                    dense_vector_ref=f"{lancedb_ref}/{BLOCKS_TABLE_NAME}#{block.block_id}",
                )
            )

        bundle = IndexBundle(
            index_id=index_id,
            status="candidate",
            doc_id=block_set.doc_id,
            doc_version=block_set.doc_version,
            block_set_hash=block_set.block_set_hash,
            embedding_profile=provider.profile,
            embedding_input_policy=policy,
            keyword_profile=KEYWORD_PROFILE,
            block_indexes=block_indexes,
            lancedb_ref=lancedb_ref,
            keyword_index_ref=f"{lancedb_ref}/{BLOCKS_TABLE_NAME}#{FTS_FIELD_NAME}",
            artifact_ref=artifact_ref,
            usage_preview=run_result.usage_preview,
            usage=run_result.usage,
            created_at=datetime.now(UTC).isoformat(),
            errors=[],
        )

        staging_directory = Path(
            tempfile.mkdtemp(
                prefix=".t-",
                dir=job_directory,
            )
        )
        try:
            write_candidate_lancedb(
                staging_directory / "lance",
                rows,
                dimension=provider.profile.dimension,
            )
            validate_candidate_lancedb(
                staging_directory / "lance",
                expected_block_ids=expected_block_ids,
                dimension=provider.profile.dimension,
            )
            (staging_directory / "index.json").write_text(
                bundle.model_dump_json(indent=2),
                encoding="utf-8",
            )
            os.replace(staging_directory, final_directory)
        except Exception as error:
            if staging_directory.exists():
                shutil.rmtree(staging_directory)
            if isinstance(error, IngestFailure):
                raise
            raise IngestFailure(
                "candidate_index_write_failed",
                f"could not finalize candidate index: {error}",
                500,
            ) from error

        return bundle

    @staticmethod
    def _validate_inputs(section_tree: SectionTree, block_set: BlockSet) -> None:
        """在调用 embedding API 前验证身份、hash、顺序和章节引用。"""

        if not block_set.blocks:
            raise IngestFailure(
                "index_empty_block_set",
                "cannot build a candidate index for an empty block set",
                422,
            )
        if (
            section_tree.doc_id != block_set.doc_id
            or section_tree.doc_version != block_set.doc_version
        ):
            raise IngestFailure(
                "index_document_mismatch",
                "section tree and block set do not describe the same document version",
                409,
            )
        block_ids = [block.block_id for block in block_set.blocks]
        if len(block_ids) != len(set(block_ids)):
            raise IngestFailure(
                "index_integrity_failed",
                "block set contains duplicate block IDs",
                500,
            )
        expected_indices = list(range(1, len(block_set.blocks) + 1))
        if [block.block_index for block in block_set.blocks] != expected_indices:
            raise IngestFailure(
                "index_integrity_failed",
                "block indexes are not contiguous",
                500,
            )
        section_paths = {tuple(section.path) for section in section_tree.sections}
        for block in block_set.blocks:
            actual_hash = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
            if actual_hash != block.content_hash:
                raise IngestFailure(
                    "index_block_integrity_failed",
                    f"block {block.block_id} text no longer matches its content hash",
                    409,
                )
            if tuple(block.section_path) not in section_paths:
                raise IngestFailure(
                    "index_integrity_failed",
                    f"block {block.block_id} references an unknown section path",
                    500,
                )

    @staticmethod
    def _index_id(*, block_set: BlockSet, provider: EmbeddingProvider) -> str:
        payload = {
            "doc_id": block_set.doc_id,
            "doc_version": block_set.doc_version,
            "block_set_hash": block_set.block_set_hash,
            "embedding_profile": provider.profile.model_dump(mode="json"),
            "embedding_input_policy": EMBEDDING_INPUT_POLICY.model_dump(mode="json"),
            "keyword_profile": KEYWORD_PROFILE.model_dump(mode="json"),
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return f"idx_{digest[:16]}"

    @staticmethod
    def _read_existing_bundle(
        path: Path,
        *,
        expected_index_id: str,
        expected_block_ids: set[str],
        dimension: int,
    ) -> IndexBundle:
        try:
            bundle = IndexBundle.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise IngestFailure(
                "candidate_index_invalid",
                f"existing candidate bundle is invalid: {error}",
                409,
            ) from error
        if bundle.index_id != expected_index_id or bundle.status != "candidate":
            raise IngestFailure(
                "candidate_index_invalid",
                "existing candidate bundle does not match the requested index identity",
                409,
            )
        lancedb_directory = path.parent / "lance"
        validate_candidate_lancedb(
            lancedb_directory,
            expected_block_ids=expected_block_ids,
            dimension=dimension,
        )
        return bundle
