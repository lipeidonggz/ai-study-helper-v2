from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import lancedb
import pytest

from app.collections.models import CandidateSnapshotManifest
from app.collections.registry import CollectionRegistry
from app.collections.service import CollectionSnapshotService
from app.ingestion.errors import IngestFailure
from app.main import create_app
from app.providers.embeddings import FakeEmbeddingProvider
from app.storage.database import StorageDatabase

TOKEN = "collection-test-token"


def _assert_utc_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        f"<h2>正文</h2><p>{body}</p>"
        "<p>补充正文用于确保提取、章节、切块和候选索引流程具有足够内容。</p>"
        "</article></body></html>"
    ).encode()


async def _run_job(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    html_path: Path,
    *,
    index: bool = True,
) -> tuple[dict, dict | None]:
    preview = await client.post(
        "/api/ingest/preview",
        headers=headers,
        json={"source_kind": "file", "source_value": str(html_path)},
    )
    preview.raise_for_status()
    persisted = await client.post(
        "/api/ingest/persist",
        headers=headers,
        json={"job_id": preview.json()["job_id"]},
    )
    persisted.raise_for_status()
    bundle = None
    if index:
        indexed = await client.post(
            "/api/ingest/index",
            headers=headers,
            json={"job_id": preview.json()["job_id"]},
        )
        indexed.raise_for_status()
        bundle = indexed.json()
    return persisted.json(), bundle


async def _publish(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    job_ids: list[str],
) -> tuple[int, dict]:
    response = await client.post(
        "/api/collections/publish",
        headers=headers,
        json={"job_ids": job_ids},
    )
    return response.status_code, response.json()


def _run_client_flow(
    tmp_path: Path,
    html_paths: list[Path],
    *,
    index_flags: list[bool] | None = None,
) -> tuple[list[dict], list[dict | None], tuple[int, dict]]:
    async def run() -> tuple[list[dict], list[dict | None], tuple[int, dict]]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        headers = {"x-launch-token": TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            documents: list[dict] = []
            bundles: list[dict | None] = []
            flags = index_flags or [True] * len(html_paths)
            for html_path, should_index in zip(html_paths, flags, strict=True):
                persisted, bundle = await _run_job(
                    client,
                    headers,
                    html_path,
                    index=should_index,
                )
                documents.append(persisted)
                bundles.append(bundle)
            result = await _publish(
                client,
                headers,
                [document["job_id"] for document in documents],
            )
            return documents, bundles, result

    return asyncio.run(run())


def test_publish_builds_self_contained_snapshot_and_pointer(tmp_path: Path) -> None:
    html_path = tmp_path / "first.html"
    html_path.write_bytes(_html("第一篇", "沙箱和网络边界是本文的核心内容。"))

    documents, _, (status_code, response) = _run_client_flow(
        tmp_path,
        [html_path],
    )

    assert status_code == 200
    manifest = response["manifest"]
    pointer = response["pointer"]
    record = response["publish_record"]
    assert manifest["status"] == "ready"
    assert manifest["completeness"] == "complete"
    assert manifest["totals"]["documents"] == 1
    assert "rrf_k" not in manifest["versions"]
    assert "top_k" not in manifest["versions"]
    assert len(manifest["manifest_hash"]) == 64
    manifest_payload = dict(manifest)
    recorded_hash = manifest_payload.pop("manifest_hash")
    assert (
        recorded_hash
        == hashlib.sha256(
            json.dumps(
                manifest_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert pointer["snapshot_id"] == manifest["snapshot_id"]
    assert pointer["generation"] == 1
    assert record["result"] == "committed"
    _assert_utc_timestamp(pointer["published_at"])
    _assert_utc_timestamp(record["started_at"])
    _assert_utc_timestamp(record["finished_at"])

    snapshot_root = tmp_path / "collections" / manifest["snapshot_id"]
    assert (snapshot_root / "manifest.json").is_file()
    assert (snapshot_root / "index" / "lance").is_dir()
    document_id = documents[0]["document"]["doc_id"]
    assert (snapshot_root / "documents" / document_id / "v0001" / "blocks.json").is_file()
    table = lancedb.connect(str(snapshot_root / "index" / "lance")).open_table("blocks")
    assert table.count_rows() == manifest["totals"]["blocks"]
    stored_manifest = json.loads((snapshot_root / "manifest.json").read_text(encoding="utf-8"))
    assert stored_manifest == manifest
    for artifact in manifest["documents"][0]["artifact_refs"]:
        content = (snapshot_root / artifact["snapshot_ref"]).read_bytes()
        assert artifact["byte_length"] == len(content)
        assert artifact["sha256"] == hashlib.sha256(content).hexdigest()

    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute(
            "SELECT snapshot_id, generation FROM active_collection_pointer"
        ).fetchone() == (manifest["snapshot_id"], 1)
        assert connection.execute("SELECT result FROM collection_publish_records").fetchone() == (
            "committed",
        )

    async def read_current() -> tuple[int, dict]:
        app = create_app(TOKEN, data_root=tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                "/api/collections/current",
                headers={"x-launch-token": TOKEN},
            )
            return response.status_code, response.json()

    current_status, current = asyncio.run(read_current())
    assert current_status == 200
    assert current["pointer"]["snapshot_id"] == pointer["snapshot_id"]
    assert current["manifest"]["manifest_hash"] == manifest["manifest_hash"]
    _assert_utc_timestamp(current["pointer"]["published_at"])


def test_second_publish_increments_generation_and_keeps_previous(tmp_path: Path) -> None:
    first_path = tmp_path / "first.html"
    second_path = tmp_path / "second.html"
    first_path.write_bytes(_html("第一篇", "第一篇正文用于第一次 collection 发布。"))
    second_path.write_bytes(_html("第二篇", "第二篇正文用于第二次 collection 发布。"))

    first_docs, _, (_, first_response) = _run_client_flow(tmp_path, [first_path])
    # A second isolated app instance uses the same data root and existing pointer.
    second_docs, _, (_, second_response) = _run_client_flow(tmp_path, [second_path])

    first_pointer = first_response["pointer"]
    second_pointer = second_response["pointer"]
    assert second_pointer["generation"] == first_pointer["generation"] + 1
    assert second_pointer["previous_snapshot_id"] == first_pointer["snapshot_id"]
    assert second_pointer["snapshot_id"] != first_pointer["snapshot_id"]
    assert second_docs[0]["document"]["doc_id"] != first_docs[0]["document"]["doc_id"]


def test_second_publish_merges_new_documents_into_active_collection(tmp_path: Path) -> None:
    first_path = tmp_path / "first.html"
    second_path = tmp_path / "second.html"
    third_path = tmp_path / "third.html"
    first_path.write_bytes(_html("First document", "First active collection document."))
    second_path.write_bytes(_html("Second document", "Second incremental document."))
    third_path.write_bytes(_html("Third document", "Third incremental document."))

    first_docs, _, (_, first_response) = _run_client_flow(tmp_path, [first_path])
    second_docs, _, (_, second_response) = _run_client_flow(
        tmp_path,
        [second_path, third_path],
    )

    first_pointer = first_response["pointer"]
    second_pointer = second_response["pointer"]
    expected_doc_ids = {
        *(document["document"]["doc_id"] for document in first_docs),
        *(document["document"]["doc_id"] for document in second_docs),
    }
    assert second_pointer["generation"] == first_pointer["generation"] + 1
    assert second_pointer["previous_snapshot_id"] == first_pointer["snapshot_id"]
    assert second_pointer["snapshot_id"] != first_pointer["snapshot_id"]
    assert second_response["manifest"]["totals"]["documents"] == len(expected_doc_ids)
    assert {
        document["doc_id"] for document in second_response["manifest"]["documents"]
    } == expected_doc_ids
    snapshot_root = tmp_path / "collections" / second_pointer["snapshot_id"]
    table = lancedb.connect(str(snapshot_root / "index" / "lance")).open_table("blocks")
    assert {row["doc_id"] for row in table.search().limit(100).to_list()} == expected_doc_ids


def test_reimport_replaces_previous_version_without_duplicating_document(
    tmp_path: Path,
) -> None:
    html_path = tmp_path / "versioned.html"
    html_path.write_bytes(
        _html("Version one", "First content for incremental version replacement.")
    )
    first_docs, _, (_, first_response) = _run_client_flow(tmp_path, [html_path])

    html_path.write_bytes(
        _html("Version two", "Changed content for incremental version replacement.")
    )
    second_docs, _, (_, second_response) = _run_client_flow(tmp_path, [html_path])

    first_doc = first_docs[0]["document"]
    second_doc = second_docs[0]["document"]
    assert second_doc["doc_id"] == first_doc["doc_id"]
    assert second_doc["current_doc_version"] == 2
    manifest = second_response["manifest"]
    assert manifest["totals"]["documents"] == 1
    assert manifest["documents"][0]["doc_id"] == first_doc["doc_id"]
    assert manifest["documents"][0]["doc_version"] == 2
    assert (
        second_response["pointer"]["previous_snapshot_id"]
        == first_response["pointer"]["snapshot_id"]
    )


def test_rollback_switches_pointer_to_previous_snapshot(tmp_path: Path) -> None:
    first_path = tmp_path / "first.html"
    second_path = tmp_path / "second.html"
    first_path.write_bytes(_html("回滚一", "第一篇正文用于回滚验证。"))
    second_path.write_bytes(_html("回滚二", "第二篇正文用于回滚验证。"))
    _run_client_flow(tmp_path, [first_path])
    _, _, (_, second_response) = _run_client_flow(tmp_path, [second_path])
    first_pointer = second_response["pointer"]["previous_snapshot_id"]

    async def run() -> tuple[int, dict]:
        app = create_app(TOKEN, data_root=tmp_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/collections/rollback",
                headers={"x-launch-token": TOKEN},
                json={"expected_generation": second_response["pointer"]["generation"]},
            )
            return response.status_code, response.json()

    status_code, response = asyncio.run(run())

    assert status_code == 200
    assert response["pointer"]["snapshot_id"] == first_pointer
    assert response["pointer"]["previous_snapshot_id"] == second_response["pointer"]["snapshot_id"]
    assert response["publish_record"]["reason"] == "rollback"
    assert response["publish_record"]["result"] == "committed"


def test_multi_document_snapshot_merges_collection_index(tmp_path: Path) -> None:
    first_path = tmp_path / "first.html"
    second_path = tmp_path / "second.html"
    first_path.write_bytes(_html("多文档一", "第一篇正文用于集合索引合并验证。"))
    second_path.write_bytes(_html("多文档二", "第二篇正文用于集合索引合并验证。"))

    documents, _, (status_code, response) = _run_client_flow(
        tmp_path,
        [first_path, second_path],
    )

    assert status_code == 200
    manifest = response["manifest"]
    assert manifest["status"] == "ready"
    assert manifest["totals"]["documents"] == 2
    assert len(manifest["documents"]) == 2
    snapshot_root = tmp_path / "collections" / manifest["snapshot_id"]
    table = lancedb.connect(str(snapshot_root / "index" / "lance")).open_table("blocks")
    rows = table.search().limit(100).to_list()
    expected_doc_ids = {document["document"]["doc_id"] for document in documents}
    assert {row["doc_id"] for row in rows} == expected_doc_ids
    assert len({row["block_id"] for row in rows}) == len(rows)


def test_partial_snapshot_publishes_valid_documents_and_records_failures(
    tmp_path: Path,
) -> None:
    html_path = tmp_path / "valid.html"
    html_path.write_bytes(_html("有效文档", "有效文档正文用于 partial snapshot 验证。"))

    async def run() -> tuple[int, dict]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        headers = {"x-launch-token": TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            persisted, _ = await _run_job(client, headers, html_path)
            return await _publish(
                client,
                headers,
                [persisted["job_id"], "f" * 32],
            )

    status_code, response = asyncio.run(run())

    assert status_code == 200
    assert response["manifest"]["status"] == "ready_partial"
    assert response["manifest"]["completeness"] == "partial"
    assert len(response["manifest"]["failed_documents"]) == 1
    assert response["pointer"] is not None
    assert response["publish_record"]["result"] == "committed"


def test_failed_snapshot_does_not_publish_or_replace_pointer(tmp_path: Path) -> None:
    html_path = tmp_path / "unindexed.html"
    html_path.write_bytes(_html("未建索引", "文档没有 S5 候选索引，因此不能发布。"))

    async def run() -> tuple[int, dict]:
        app = create_app(
            TOKEN,
            data_root=tmp_path,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        headers = {"x-launch-token": TOKEN}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            persisted, _ = await _run_job(client, headers, html_path, index=False)
            return await _publish(client, headers, [persisted["job_id"]])

    status_code, response = asyncio.run(run())

    assert status_code == 200
    assert response["manifest"]["status"] == "failed"
    assert response["pointer"] is None
    assert response["publish_record"]["result"] == "aborted"
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute("SELECT COUNT(*) FROM active_collection_pointer").fetchone() == (
            0,
        )


def test_collection_registry_rejects_stale_cas_generation(tmp_path: Path) -> None:
    html_path = tmp_path / "cas.html"
    html_path.write_bytes(_html("CAS", "用于验证活动指针 generation CAS。"))
    documents, _, (_, first_response) = _run_client_flow(tmp_path, [html_path])

    service = CollectionSnapshotService(tmp_path)
    second_manifest = service.build([documents[0]["job_id"]])
    registry = CollectionRegistry(StorageDatabase(tmp_path))

    with pytest.raises(IngestFailure) as error:
        registry.publish(
            second_manifest,
            snapshot_path=f"collections/{second_manifest.snapshot_id}",
            expected_generation=0,
        )

    assert error.value.code == "publish_generation_conflict"
    current = registry.current()
    assert current is not None
    assert current.snapshot_id == first_response["pointer"]["snapshot_id"]
    assert current.generation == 1
    with sqlite3.connect(tmp_path / "db" / "application.db") as connection:
        assert connection.execute(
            "SELECT result, reason FROM collection_publish_records ORDER BY started_at DESC LIMIT 1"
        ).fetchone() == ("aborted", "publish_generation_conflict")


def test_same_snapshot_publish_is_idempotent(tmp_path: Path) -> None:
    html_path = tmp_path / "idempotent.html"
    html_path.write_bytes(_html("幂等", "用于验证相同 snapshot 重复发布不递增 generation。"))
    documents, _, (_, first_response) = _run_client_flow(tmp_path, [html_path])
    manifest = first_response["manifest"]
    registry = CollectionRegistry(StorageDatabase(tmp_path))

    parsed = CandidateSnapshotManifest.model_validate(manifest)
    pointer, record = registry.publish(
        parsed,
        snapshot_path=f"collections/{parsed.snapshot_id}",
    )

    assert pointer.generation == 1
    assert record.reason == "idempotent"


def test_snapshot_build_failure_cleans_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    html_path = tmp_path / "cleanup.html"
    html_path.write_bytes(_html("清理", "用于验证 snapshot staging 失败清理。"))
    documents, _, _ = _run_client_flow(tmp_path, [html_path])

    def fail_index(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated index write failure")

    monkeypatch.setattr("app.collections.service.write_candidate_lancedb", fail_index)
    service = CollectionSnapshotService(tmp_path)

    with pytest.raises(IngestFailure, match="could not finalize"):
        service.build([documents[0]["job_id"]])

    assert list((tmp_path / "collections").glob(".b-*")) == []


def test_publish_verification_rejects_tampered_artifact(tmp_path: Path) -> None:
    html_path = tmp_path / "tamper.html"
    html_path.write_bytes(_html("校验", "用于验证发布前的 artifact checksum 对账。"))
    documents, _, _ = _run_client_flow(tmp_path, [html_path])
    service = CollectionSnapshotService(tmp_path)
    manifest = service.build([documents[0]["job_id"]])
    snapshot_root = tmp_path / "collections" / manifest.snapshot_id
    block_artifact = next(
        artifact for artifact in manifest.documents[0].artifact_refs if artifact.kind == "block_set"
    )
    with (snapshot_root / block_artifact.snapshot_ref).open("ab") as file:
        file.write(b"\n")

    with pytest.raises(IngestFailure) as error:
        service._verify_snapshot(manifest)

    assert error.value.code == "snapshot_verification_failed"
