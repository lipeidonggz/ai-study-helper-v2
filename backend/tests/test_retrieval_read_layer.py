from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.main import create_app
from app.providers.embeddings import FakeEmbeddingProvider
from app.retrieval.service import RetrievalFailure, RetrievalReadService

TOKEN = "retrieval-read-test-token"


def _html(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='zh'><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body><article><h1>{title}</h1>"
        "<h2>Alpha</h2><p>Alpha 根章节包含 MCP 和沙箱边界说明。</p>"
        "<h3>Alpha child</h3><p>Alpha 子章节包含需要完整枚举的补充证据。</p>"
        "<h2>Beta</h2>"
        f"<p>{body}</p>"
        "</article></body></html>"
    ).encode()


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    task_id: str,
    *,
    timeout_seconds: float = 15,
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(
            f"/api/tasks/{task_id}",
            headers={"x-launch-token": TOKEN},
        )
        response.raise_for_status()
        task = response.json()
        if task["status"] in {"succeeded", "failed", "canceled", "interrupted"}:
            return task
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _ingest(data_root: Path, paths: list[Path]) -> str:
    async def run() -> str:
        app = create_app(
            TOKEN,
            data_root=data_root,
            embedding_provider_factory=FakeEmbeddingProvider,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            created = await client.post(
                "/api/tasks/ingest",
                headers={"x-launch-token": TOKEN},
                json={
                    "sources": [
                        {"source_kind": "file", "source_value": str(path)} for path in paths
                    ]
                },
            )
            created.raise_for_status()
            task = await _wait_for_terminal(client, created.json()["task_id"])
            assert task["status"] == "succeeded"
            assert task["snapshot_id"] is not None
            return task["snapshot_id"]

    return asyncio.run(run())


def test_reader_keeps_its_snapshot_after_a_new_collection_is_published(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "alpha-doc.html"
    second_path = tmp_path / "beta-doc.html"
    first_path.write_bytes(_html("Alpha document", "第一篇文档的第二章节正文。"))
    second_path.write_bytes(_html("Beta document", "第二篇文档的第二章节正文。"))

    first_snapshot = _ingest(tmp_path, [first_path])
    service = RetrievalReadService(tmp_path)
    bound_reader = service.bind_current()

    second_snapshot = _ingest(tmp_path, [second_path])
    fresh_reader = service.bind_current()

    assert bound_reader.snapshot_id == first_snapshot
    assert second_snapshot != first_snapshot
    assert [document.title for document in bound_reader.list_documents().documents] == [
        "Alpha document"
    ]
    assert {document.title for document in fresh_reader.list_documents().documents} == {
        "Alpha document",
        "Beta document",
    }


def test_explicit_snapshot_binding_does_not_follow_active_pointer(tmp_path: Path) -> None:
    first_path = tmp_path / "alpha-doc.html"
    second_path = tmp_path / "beta-doc.html"
    first_path.write_bytes(_html("Alpha document", "第一篇文档的第二章节正文。"))
    second_path.write_bytes(_html("Beta document", "第二篇文档的第二章节正文。"))

    first_snapshot = _ingest(tmp_path, [first_path])
    second_snapshot = _ingest(tmp_path, [second_path])
    reader = RetrievalReadService(tmp_path).bind_snapshot(first_snapshot)

    assert second_snapshot != first_snapshot
    assert RetrievalReadService(tmp_path).bind_current().snapshot_id == second_snapshot
    assert [document.title for document in reader.list_documents().documents] == ["Alpha document"]


def test_list_documents_applies_filters_and_stable_sorting(tmp_path: Path) -> None:
    paths = [tmp_path / f"document-{index}.html" for index in range(3)]
    for index, path in enumerate(paths):
        path.write_bytes(_html(f"Document {index}", f"第 {index} 篇正文。"))
    _ingest(tmp_path, paths)
    reader = RetrievalReadService(tmp_path).bind_current()

    all_documents = reader.list_documents()
    filtered = reader.list_documents(title_contains="Document 1")
    source_filtered = reader.list_documents(
        source=all_documents.documents[0].source_label,
        lang="zh",
    )

    assert all_documents.total == 3
    assert [document.title for document in all_documents.documents] == [
        "Document 0",
        "Document 1",
        "Document 2",
    ]
    assert [document.title for document in filtered.documents] == ["Document 1"]
    assert filtered.total == 1
    assert source_filtered.total == 3


def test_outline_and_section_enumeration_include_descendant_blocks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "outline.html"
    path.write_bytes(_html("Outline document", "Beta 章节正文。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    outline = reader.get_outline([document.doc_id]).documents[0].document
    assert outline is not None
    alpha = next(section for section in outline.sections if section.title == "Alpha")
    alpha_blocks = reader.list_blocks(document.doc_id, [alpha.path])
    beta = next(section for section in outline.sections if section.title == "Beta")
    combined_blocks = reader.list_blocks(
        document.doc_id,
        [alpha.path, beta.path, alpha.path],
    )
    all_blocks = reader.list_blocks(document.doc_id)

    assert alpha.subtree_blocks >= 2
    assert alpha.subtree_context_tokens >= alpha.own_context_tokens
    assert any("MCP" in block.text for block in alpha_blocks.blocks)
    assert any("子章节" in block.text for block in alpha_blocks.blocks)
    assert len({block.block_id for block in combined_blocks.blocks}) == len(combined_blocks.blocks)
    assert combined_blocks.total > alpha_blocks.total
    assert [block.block_id for block in all_blocks.blocks] == sorted(
        (block.block_id for block in all_blocks.blocks),
    )


def test_get_outline_returns_per_document_scope_errors(tmp_path: Path) -> None:
    path = tmp_path / "outline-error.html"
    path.write_bytes(_html("Outline error document", "用于验证同一请求中的部分文档错误。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    outline = reader.get_outline([document.doc_id, "doc_missing"])

    assert outline.documents[0].document is not None
    assert outline.documents[1].error is not None
    assert outline.documents[1].error.code == "scope_not_found"


def test_scan_term_distinguishes_zero_matches_from_missing_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "scan.html"
    path.write_bytes(_html("Scan document", "只有普通正文，没有那个缩写。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    matches = reader.scan_term("mcp", [document.doc_id])
    no_matches = reader.scan_term("SENTINEL_ABSENT", [document.doc_id])

    assert matches.count == 1
    assert "MCP" in matches.matches[0].snippet
    assert no_matches.count == 0
    assert no_matches.matches == []
    with pytest.raises(RetrievalFailure) as missing_scope:
        reader.scan_term("MCP", ["doc_missing"])
    assert missing_scope.value.code == "scope_not_found"


def test_empty_arguments_and_missing_sections_return_stable_errors(
    tmp_path: Path,
) -> None:
    path = tmp_path / "errors.html"
    path.write_bytes(_html("Error document", "用于验证参数和范围错误。"))
    _ingest(tmp_path, [path])
    reader = RetrievalReadService(tmp_path).bind_current()
    document = reader.list_documents().documents[0]

    with pytest.raises(RetrievalFailure) as empty_term:
        reader.scan_term("   ")
    assert empty_term.value.code == "invalid_argument"

    with pytest.raises(RetrievalFailure) as invalid_scope:
        reader.list_blocks(document.doc_id, [["Missing section"]])
    assert invalid_scope.value.code == "scope_not_found"


def test_missing_active_collection_is_not_an_empty_library(tmp_path: Path) -> None:
    with pytest.raises(RetrievalFailure) as unavailable:
        RetrievalReadService(tmp_path).bind_current()

    assert unavailable.value.code == "collection_unavailable"


def test_tampered_snapshot_document_checksum_is_not_silently_ignored(
    tmp_path: Path,
) -> None:
    path = tmp_path / "corrupt.html"
    path.write_bytes(_html("Corrupt document", "用于验证快照文档损坏。"))
    snapshot_id = _ingest(tmp_path, [path])
    manifest_path = tmp_path / "collections" / snapshot_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact = next(
        item for item in manifest["documents"][0]["artifact_refs"] if item["kind"] == "document"
    )
    artifact_path = tmp_path / "collections" / snapshot_id / artifact["snapshot_ref"]
    persisted = json.loads(artifact_path.read_text(encoding="utf-8"))
    persisted["document"]["title"] = "Tampered title"
    artifact_path.write_text(
        json.dumps(persisted, ensure_ascii=False),
        encoding="utf-8",
    )

    reader = RetrievalReadService(tmp_path).bind_current()
    with pytest.raises(RetrievalFailure) as corrupted:
        reader.list_documents()

    assert corrupted.value.code == "snapshot_integrity_error"
