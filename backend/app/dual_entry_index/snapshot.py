"""直接读取 collection 快照产物的 5.1 输入适配器。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from hmac import compare_digest
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.dual_entry_index.models import DocumentVersionIdentity, SnapshotData, SourceBlock
from app.dual_entry_index.normalization import canonical_hash


class SnapshotLoadError(RuntimeError):
    """快照清单、产物身份或原文块内容不一致时抛出的失败。"""


def select_snapshot_documents(snapshot: SnapshotData, doc_ids: list[str]) -> SnapshotData:
    """按文档筛选快照块；显式筛选必须覆盖整份清单，避免局部块冒充整份快照。"""

    if not doc_ids:
        return snapshot
    snapshot_doc_ids = {document.doc_id for document in snapshot.documents}
    selected_doc_ids = set(doc_ids)
    if selected_doc_ids != snapshot_doc_ids:
        raise SnapshotLoadError("document selection must cover the entire snapshot")
    return snapshot.model_copy(
        update={
            "blocks": tuple(block for block in snapshot.blocks if block.doc_id in selected_doc_ids)
        }
    )


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SnapshotLoadError(f"cannot read JSON object: {path}") from error
    if not isinstance(payload, dict):
        raise SnapshotLoadError(f"JSON root must be an object: {path}")
    return payload


def _resolve_snapshot_relative_path(root: Path, relative_path: str) -> Path:
    root_resolved = root.resolve()
    candidate = (root_resolved / relative_path).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as error:
        raise SnapshotLoadError("snapshot artifact path escapes snapshot root") from error
    return candidate


def _extract_document_artifact(document: dict[str, Any], kind: str) -> dict[str, Any]:
    artifact_refs = document.get("artifact_refs")
    if not isinstance(artifact_refs, list):
        raise SnapshotLoadError("document artifact_refs must be a list")
    matching_artifacts = [
        artifact
        for artifact in artifact_refs
        if isinstance(artifact, dict) and artifact.get("kind") == kind
    ]
    if len(matching_artifacts) != 1:
        raise SnapshotLoadError(f"document must declare exactly one {kind} artifact")
    artifact = matching_artifacts[0]
    snapshot_ref = artifact.get("snapshot_ref")
    if not isinstance(snapshot_ref, str) or not snapshot_ref:
        raise SnapshotLoadError(f"{kind} artifact snapshot_ref is invalid")
    return artifact


def _validate_artifact_bytes(
    path: Path,
    artifact: dict[str, Any],
    *,
    kind: str,
) -> bytes:
    expected_hash = artifact.get("sha256")
    expected_length = artifact.get("byte_length")
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise SnapshotLoadError(f"{kind} artifact sha256 is invalid")
    if not isinstance(expected_length, int) or expected_length < 0:
        raise SnapshotLoadError(f"{kind} artifact byte_length is invalid")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise SnapshotLoadError(f"cannot read {kind} artifact: {path}") from error
    if len(content) != expected_length:
        raise SnapshotLoadError(f"{kind} artifact byte_length does not match manifest")
    if not compare_digest(hashlib.sha256(content).hexdigest(), expected_hash):
        raise SnapshotLoadError(f"{kind} artifact sha256 does not match manifest")
    return content


def _load_json_object_bytes(path: Path, content: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SnapshotLoadError(f"cannot read JSON object: {path}") from error
    if not isinstance(payload, dict):
        raise SnapshotLoadError(f"JSON root must be an object: {path}")
    return payload


def _validate_manifest_hash(manifest: dict[str, Any], recorded_hash: str) -> None:
    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if not compare_digest(canonical_hash(payload), recorded_hash):
        raise SnapshotLoadError("manifest manifest_hash does not match content")


def _extract_document_artifact_ref(document: dict[str, Any], kind: str) -> str:
    artifact = _extract_document_artifact(document, kind)
    snapshot_ref = artifact["snapshot_ref"]
    assert isinstance(snapshot_ref, str)
    return snapshot_ref


def _table_context_fields(block: dict[str, Any]) -> tuple[str | None, list[str]]:
    context = block.get("table_context")
    if context is None:
        return None, []
    if not isinstance(context, dict):
        raise SnapshotLoadError("block table_context must be an object or null")
    caption = context.get("caption")
    raw_headers = context.get("headers")
    if caption is not None and not isinstance(caption, str):
        raise SnapshotLoadError("block table_context caption must be a string or null")
    if not isinstance(raw_headers, list) or any(
        not isinstance(header, str) for header in raw_headers
    ):
        raise SnapshotLoadError("block table_context headers must be a string list")
    return caption, raw_headers


def _source_node_types(
    block: dict[str, Any],
    nodes_by_ordinal: dict[int, str],
) -> tuple[str, ...]:
    raw_ordinals = block.get("source_node_ordinals")
    if not isinstance(raw_ordinals, list) or not raw_ordinals:
        raise SnapshotLoadError("block source_node_ordinals must be a non-empty list")
    if any(not isinstance(ordinal, int) or ordinal < 0 for ordinal in raw_ordinals):
        raise SnapshotLoadError("block source_node_ordinals must contain non-negative integers")
    try:
        return tuple(sorted({nodes_by_ordinal[ordinal] for ordinal in raw_ordinals}))
    except KeyError as error:
        raise SnapshotLoadError("block references an unknown source node ordinal") from error


def _nodes_by_ordinal(extracted: dict[str, Any]) -> dict[int, str]:
    raw_nodes = extracted.get("nodes")
    if not isinstance(raw_nodes, list):
        raise SnapshotLoadError("extracted artifact nodes must be a list")
    nodes: dict[int, str] = {}
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise SnapshotLoadError("extracted node entry must be an object")
        ordinal = raw_node.get("ordinal")
        kind = raw_node.get("kind")
        if not isinstance(ordinal, int) or ordinal < 0:
            raise SnapshotLoadError("extracted node ordinal is invalid")
        if not isinstance(kind, str) or not kind:
            raise SnapshotLoadError("extracted node kind is invalid")
        if ordinal in nodes:
            raise SnapshotLoadError("extracted node ordinals must be unique")
        nodes[ordinal] = kind
    return nodes


class SnapshotLoader:
    """从 manifest 和 canonical block artifacts 构造独立编译输入。"""

    def load(self, snapshot_root: Path) -> SnapshotData:
        """加载并校验一个不可变 collection 快照。"""

        root = snapshot_root.resolve()
        manifest_path = root / "manifest.json"
        manifest = _load_json_object(manifest_path)
        snapshot_id = manifest.get("snapshot_id")
        manifest_hash = manifest.get("manifest_hash")
        documents = manifest.get("documents")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise SnapshotLoadError("manifest snapshot_id is missing")
        if not isinstance(manifest_hash, str) or len(manifest_hash) != 64:
            raise SnapshotLoadError("manifest manifest_hash is invalid")
        if not isinstance(documents, list):
            raise SnapshotLoadError("manifest documents must be a list")
        _validate_manifest_hash(manifest, manifest_hash)

        document_versions: list[DocumentVersionIdentity] = []
        blocks: list[SourceBlock] = []
        block_ordinal = 0
        for document in documents:
            if not isinstance(document, dict):
                raise SnapshotLoadError("manifest document entry must be an object")
            doc_id = document.get("doc_id")
            doc_version = document.get("doc_version")
            title = document.get("title")
            declared_block_count = document.get("block_count")
            declared_table_block_count = document.get("table_block_count")
            if not isinstance(doc_id, str) or not doc_id:
                raise SnapshotLoadError("manifest document doc_id is missing")
            if not isinstance(doc_version, int) or doc_version < 1:
                raise SnapshotLoadError("manifest document doc_version is invalid")
            if not isinstance(title, str):
                raise SnapshotLoadError("manifest document title is invalid")
            if not isinstance(declared_block_count, int) or declared_block_count < 0:
                raise SnapshotLoadError("manifest document block_count is invalid")
            if not isinstance(declared_table_block_count, int) or declared_table_block_count < 0:
                raise SnapshotLoadError("manifest document table_block_count is invalid")
            document_versions.append(
                DocumentVersionIdentity(doc_id=doc_id, doc_version=doc_version)
            )
            extracted_artifact = _extract_document_artifact(document, "extracted")
            extracted_path = _resolve_snapshot_relative_path(
                root,
                _extract_document_artifact_ref(document, "extracted"),
            )
            extracted_bytes = _validate_artifact_bytes(
                extracted_path,
                extracted_artifact,
                kind="extracted",
            )
            extracted = _load_json_object_bytes(extracted_path, extracted_bytes)
            nodes_by_ordinal = _nodes_by_ordinal(extracted)

            block_set_artifact = _extract_document_artifact(document, "block_set")
            block_set_path = _resolve_snapshot_relative_path(
                root,
                _extract_document_artifact_ref(document, "block_set"),
            )
            block_set_bytes = _validate_artifact_bytes(
                block_set_path,
                block_set_artifact,
                kind="block_set",
            )
            block_set = _load_json_object_bytes(block_set_path, block_set_bytes)
            if block_set.get("doc_id") != doc_id:
                raise SnapshotLoadError("block_set doc_id does not match manifest")
            if block_set.get("doc_version") != doc_version:
                raise SnapshotLoadError("block_set doc_version does not match manifest")
            raw_blocks = block_set.get("blocks")
            if not isinstance(raw_blocks, list):
                raise SnapshotLoadError("block_set blocks must be a list")
            if len(raw_blocks) != declared_block_count:
                raise SnapshotLoadError("block_set block count does not match manifest")
            actual_table_block_count = sum(
                1
                for raw_block in raw_blocks
                if isinstance(raw_block, dict) and raw_block.get("table_context") is not None
            )
            if actual_table_block_count != declared_table_block_count:
                raise SnapshotLoadError("block_set table block count does not match manifest")
            previous_block_index = 0
            for raw_block in raw_blocks:
                if not isinstance(raw_block, dict):
                    raise SnapshotLoadError("block entry must be an object")
                block_id = raw_block.get("block_id")
                block_index = raw_block.get("block_index")
                text = raw_block.get("text")
                content_hash = raw_block.get("content_hash")
                section_path = raw_block.get("section_path", [])
                if not isinstance(block_id, str) or not block_id:
                    raise SnapshotLoadError("block_id is missing")
                if not isinstance(block_index, int) or block_index < 1:
                    raise SnapshotLoadError("block_index is invalid")
                if block_index <= previous_block_index:
                    raise SnapshotLoadError("block indexes are not strictly increasing")
                previous_block_index = block_index
                if not isinstance(text, str):
                    raise SnapshotLoadError("block text must be a string")
                if not isinstance(content_hash, str):
                    raise SnapshotLoadError("block content_hash is missing")
                actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if actual_hash != content_hash:
                    raise SnapshotLoadError("block content hash does not match text")
                if not isinstance(section_path, list) or any(
                    not isinstance(item, str) for item in section_path
                ):
                    raise SnapshotLoadError("block section_path must be a string list")
                table_caption, table_headers = _table_context_fields(raw_block)
                source_node_types = _source_node_types(raw_block, nodes_by_ordinal)
                try:
                    source_block = SourceBlock(
                        snapshot_id=snapshot_id,
                        doc_id=doc_id,
                        doc_version=doc_version,
                        doc_title=title,
                        block_id=block_id,
                        block_index=block_index,
                        block_ordinal=block_ordinal,
                        text=text,
                        content_hash=content_hash,
                        section_path=section_path,
                        source_node_types=source_node_types,
                        table_caption=table_caption,
                        table_headers=table_headers,
                    )
                except ValidationError as error:
                    raise SnapshotLoadError(f"block metadata is invalid: {block_id}") from error
                blocks.append(source_block)
                block_ordinal += 1
        try:
            return SnapshotData(
                snapshot_id=snapshot_id,
                manifest_hash=manifest_hash,
                root=str(root),
                documents=tuple(document_versions),
                blocks=tuple(blocks),
            )
        except ValidationError as error:
            raise SnapshotLoadError("snapshot input is internally inconsistent") from error


def find_active_snapshot(data_root: Path) -> Path:
    """通过活动指针数据库定位当前 collection 快照目录。"""

    resolved_root = data_root.resolve()
    database_path = resolved_root / "db" / "application.db"
    if not database_path.is_file():
        raise SnapshotLoadError(f"application database is missing: {database_path}")
    try:
        with sqlite3.connect(database_path) as connection:
            row = connection.execute(
                "select snapshot_id from active_collection_pointer where pointer_id = 1"
            ).fetchone()
    except sqlite3.Error as error:
        raise SnapshotLoadError("cannot read active collection pointer") from error
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise SnapshotLoadError("active collection pointer is empty")
    snapshot_root = resolved_root / "collections" / row[0]
    if not snapshot_root.is_dir():
        raise SnapshotLoadError(f"active collection snapshot is missing: {snapshot_root}")
    return snapshot_root
